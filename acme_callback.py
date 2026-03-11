"""
Volga <-> Cloudflare ACME dns-01 callback

Listens on a Volga topic for JSON messages instructing it to add/remove
TXT records for ACME dns-01 challenges on Cloudflare, then publishes
acknowledgements/results to an output Volga topic.

Message schema (incoming, JSON in Volga message payload):
{
  "id": "<optional correlation id>",
  "action": "add" | "remove",
  "domain": "example.com",                     # the certificate domain (informational)
  "name": "_acme-challenge.example.com",       # FQDN for the TXT record
  "value": "<the ACME token>",                 # TXT record content
  "ttl": 120                                    # optional per‑record TTL
}

Outgoing ack schema (JSON payload):
{
  "id": "<echoed correlation id if provided>",
  "action": "add" | "remove",
  "name": "_acme-challenge.example.com",
  "value": "<token>",
  "status": "ok" | "error",
  "record_id": "<Cloudflare DNS record id if applicable>",
  "zone_id": "<Cloudflare zone id>",
  "error": {"type": "...", "message": "..."}   # only on error
}

Configuration (environment variables):
  # Cloudflare
  CF_API_TOKEN        : required – Cloudflare API Token with Zone DNS:Edit for relevant zones
  CF_API_BASE         : optional – default https://api.cloudflare.com/client/v4
  CF_DEFAULT_TTL      : optional – default 120

  # Avassa / Volga
  VOLGA_ROLE_ID       : required – approle role-id (string)
  APPROLE_SECRET_ID   : required – injected by Avassa: ${SYS_APPROLE_SECRET_ID}
  API_CA_CERT         : injected by Avassa: ${SYS_API_CA_CERT}
  AVASSA_API_HOST     : optional – default https://api.internal:4646
  MANAGED_DOMAINS     : optional – comma-separated domains this instance manages (if not set, handles all)

  # Debugging
  ACME_DEBUG_DNS_VERIFICATION : optional – enable DNS verification checks for debugging (true/false)

Notes:
- Zone discovery: we look up the Cloudflare zone by repeatedly stripping the left-most
  label from the provided record name until a matching zone is found (e.g.,
  _acme-challenge.www.example.co.uk → try www.example.co.uk → example.co.uk, ...).
- Idempotency: duplicate "add" messages for the same name+value are safe – we check
  whether a TXT record with identical content already exists and reuse its id.
- Deletion: we only delete the TXT record whose content equals the provided value, leaving
  other TXT records at the same name intact.
- Token management: automatically refreshes Avassa API tokens before expiration using the
  /v1/state/strongbox/token/refresh endpoint. Tokens are refreshed 5 minutes before expiry.
- Continuous processing: waits for and processes Volga messages one by one in an endless loop.
- Domain filtering: supports multi-instance deployments where each instance manages specific domains.
- Volga configuration: uses hardcoded topic names (acme:requests/acme:events) addressed as parent topics
  (volga.Topic.parent) so the container can run on a site while the topics live on the parent Control Tower.
- Consumer mode: shared mode is used when handling all domains; exclusive mode is forced when
  MANAGED_DOMAINS is configured to prevent filtered messages from being dropped.
"""

import asyncio
import json
import os
import socket
import signal
import logging
import time
from typing import Optional, Tuple

from cloudflare import AsyncCloudflare
from cloudflare._exceptions import CloudflareError
import avassa_client
import avassa_client.volga as volga

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def _redact_value(value: Optional[str]) -> str:
    """Redact sensitive values in logs while keeping enough context for debugging."""
    if not value:
        return "<empty>"
    if len(value) <= 10:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:6]}...{value[-4:]}"

# --------------------------- Cloudflare client ---------------------------
class CloudflareClient:
    def __init__(self, api_token: str, base_url: str = "https://api.cloudflare.com/client/v4"):
        self.client = AsyncCloudflare(api_token=api_token, base_url=base_url)

    async def close(self):
        await self.client.close()

    async def find_zone_id(self, fqdn: str) -> Tuple[str, str]:
        """Return (zone_id, zone_name) for the longest-matching zone for fqdn.
        Tries progressively shorter candidate names until a match is found.
        """
        labels = fqdn.strip('.').split('.')
        for i in range(len(labels) - 1):  # require at least a.tld
            candidate = '.'.join(labels[i:])
            zones = await self.client.zones.list(name=candidate)
            if zones.result:
                zone = zones.result[0]
                return zone.id, zone.name
        raise ValueError(f"No Cloudflare zone found for {fqdn}")

    async def list_txt_records(self, zone_id: str, name: str) -> list:
        records = await self.client.dns.records.list(zone_id=zone_id, type="TXT", name=name)
        return [record.model_dump() for record in records.result]

    async def create_txt(self, zone_id: str, name: str, value: str, ttl: int) -> str:
        logging.info(f"Creating TXT record: {name} = '{_redact_value(value)}' (TTL: {ttl})")
        
        record = await self.client.dns.records.create(
            zone_id=zone_id,
            type="TXT",
            name=name,
            content=value,
            ttl=ttl
        )
        
        record_id = record.id
        logging.info(f"Successfully created TXT record {record_id} for {name}")
        
        # Add brief diagnostic info
        try:
            # Check the record was created correctly
            await asyncio.sleep(0.5)  # Brief pause
            records = await self.list_txt_records(zone_id, name)
            matching_records = [r for r in records if r.get("content") == value]
            if matching_records:
                logging.info(f"Verified: TXT record for {name} is now active in Cloudflare DNS")
            else:
                logging.warning(f"Warning: Could not immediately verify TXT record for {name}")
        except Exception as e:
            logging.warning(f"Could not verify TXT record creation: {e}")
            
        return record_id

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        logging.info(f"Deleting DNS record {record_id} from zone {zone_id}")
        await self.client.dns.records.delete(record_id, zone_id=zone_id)
        logging.info(f"Successfully deleted DNS record {record_id}")
        
    async def verify_dns_propagation(self, name: str, expected_value: str,
                                       max_attempts: int = 10, retry_delay: float = 3.0,
                                       max_retry_delay: float = 15.0) -> dict:
        """
        Verify DNS propagation for debugging ACME validation failures.
        Retries up to max_attempts times with increasing delays to account for
        wildcard CNAME overrides and Cloudflare edge propagation delays.
        """
        try:
            import dns.resolver
        except ImportError:
            return {
                "fqdn": name,
                "expected_value": expected_value,
                "validation_issues": ["DNS verification requires dnspython package"]
            }

        result = {
            "fqdn": name,
            "expected_value": expected_value,
            "cloudflare_records": [],
            "dns_resolution": {},
            "validation_issues": [],
            "attempts": 0,
        }

        # Public DNS servers to check (System DNS excluded from retry success
        # criteria since local overrides may hide the record)
        public_dns_servers = [
            ("Cloudflare", "1.1.1.1"),
            ("Google", "8.8.8.8"),
        ]
        all_dns_servers = public_dns_servers + [("System", None)]

        def _query_txt(server_ip: str | None) -> dict:
            resolver = dns.resolver.Resolver()
            if server_ip:
                resolver.nameservers = [server_ip]
            try:
                answers = resolver.resolve(name, "TXT")
                txt_values = [str(rdata).strip('"') for rdata in answers]
                return {
                    "success": True,
                    "values": txt_values,
                    "has_expected": expected_value in txt_values,
                    "server": server_ip or "system",
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "server": server_ip or "system",
                }

        try:
            zone_id, zone_name = await self.find_zone_id(name)

            # Verify the record exists in Cloudflare API
            records = await self.list_txt_records(zone_id, name)
            result["cloudflare_records"] = [
                {"id": r.get("id"), "content": r.get("content"), "ttl": r.get("ttl")}
                for r in records
            ]
            cf_values = [r["content"] for r in result["cloudflare_records"]]

            if not cf_values:
                result["validation_issues"].append("No TXT records found in Cloudflare")
            elif expected_value not in cf_values:
                result["validation_issues"].append("Expected value not found in Cloudflare records")

            # Check for multiple conflicting records
            if len(cf_values) > 1:
                unique_values = set(cf_values)
                if len(unique_values) > 1:
                    result["validation_issues"].append(
                        f"Multiple conflicting TXT records found: {unique_values}"
                    )

            # Retry loop for DNS propagation
            for attempt in range(1, max_attempts + 1):
                result["attempts"] = attempt

                dns_resolution = {}
                for server_name, server_ip in all_dns_servers:
                    dns_resolution[server_name] = await asyncio.get_event_loop().run_in_executor(
                        None, _query_txt, server_ip
                    )

                public_ok = all(
                    dns_resolution[s]["has_expected"]
                    for s, _ in public_dns_servers
                    if dns_resolution[s].get("success")
                )
                public_resolved = all(
                    dns_resolution[s].get("success")
                    for s, _ in public_dns_servers
                )

                if public_resolved and public_ok:
                    result["dns_resolution"] = dns_resolution
                    break

                if attempt < max_attempts:
                    wait = min(retry_delay * attempt, max_retry_delay)
                    logging.info(
                        f"   DNS check attempt {attempt}/{max_attempts}: "
                        f"records not yet visible, retrying in {wait:.0f}s..."
                    )
                    await asyncio.sleep(wait)
                else:
                    result["dns_resolution"] = dns_resolution

            # Analyze final resolution result
            dns_servers_with_expected = [
                sname for sname, info in result["dns_resolution"].items()
                if info.get("success") and info.get("has_expected")
            ]
            successful_servers = [
                sname for sname, info in result["dns_resolution"].items()
                if info.get("success")
            ]

            if len(dns_servers_with_expected) == 0:
                result["validation_issues"].append(
                    "Challenge value not resolved by any tested DNS servers"
                )
            elif len(dns_servers_with_expected) < len(successful_servers):
                result["validation_issues"].append(
                    "Challenge value not consistently resolved across DNS servers"
                )

        except Exception as e:
            result["validation_issues"].append(f"DNS verification failed: {str(e)}")

        return result


# CloudflareError is now provided by the official cloudflare library


# ------------------------------ Worker ------------------------------
class AcmeWorker:
    def __init__(self):
        # Cloudflare
        self.cf_token = os.environ["CF_API_TOKEN"]
        self.cf_base = os.getenv("CF_API_BASE", "https://api.cloudflare.com/client/v4")
        self.default_ttl = int(os.getenv("CF_DEFAULT_TTL", "120"))
        # Avassa / Volga
        self.role_id = os.environ["VOLGA_ROLE_ID"]
        self.approle_secret = os.environ["APPROLE_SECRET_ID"]
        self.api_host = os.getenv("AVASSA_API_HOST", "https://api.internal:4646")
        self.api_ca_cert = os.getenv("API_CA_CERT")
        
        # Hardcoded Volga settings - these rarely need to change
        self.topic_in = "acme:requests"
        self.topic_out = "acme:events"
        self.consumer_mode = "shared"
        self.position_pref = "latest"
        
        # Domain filtering for multi-instance deployment
        managed_domains_str = os.getenv("MANAGED_DOMAINS", "")
        self.managed_domains = set()
        if managed_domains_str:
            self.managed_domains = {domain.strip().lower() for domain in managed_domains_str.split(",") if domain.strip()}
            logging.info(f"This instance manages domains: {', '.join(sorted(self.managed_domains))}")
            # With domain filtering, shared consumers can drop messages that another
            # instance should handle. Use exclusive mode so all instances see messages.
            self.consumer_mode = "exclusive"
            logging.info("Domain filtering enabled: forcing consumer mode to 'exclusive'")
        else:
            logging.info("This instance manages ALL domains (no domain filtering)")
        self.hostname = socket.gethostname()
        self._shutdown = asyncio.Event()

        # Token management
        self.session: Optional[avassa_client.Session] = None
        self.token_expires_at: Optional[float] = None
        self.refresh_margin_seconds = 300  # Proactive reconnect 5 minutes before expiry
        
        self.cf = CloudflareClient(self.cf_token, self.cf_base)

    def _should_handle_domain(self, domain: str) -> bool:
        """Check if this instance should handle the given domain."""
        if not self.managed_domains:
            # No domain filtering configured, handle all domains
            return True
            
        if not domain:
            return False
            
        domain_lower = domain.strip().lower()
        
        # Direct match
        if domain_lower in self.managed_domains:
            return True
            
        # Check if any managed domain is a parent of this domain
        # e.g., if we manage "example.com", we should handle "sub.example.com"
        for managed_domain in self.managed_domains:
            if domain_lower.endswith(f".{managed_domain}"):
                return True
                
        return False

    async def _make_session(self) -> avassa_client.Session:
        """Create a new Avassa session and record token lifetime if available."""
        logging.info("Creating new Avassa session...")
        session = avassa_client.approle_login(
            host=self.api_host,
            role_id=self.role_id,
            secret_id=self.approle_secret,
            user_agent="volga-acme-cloudflare/1.0",
        )

        if hasattr(session, 'expires_in'):
            self.token_expires_at = time.time() + session.expires_in
            logging.info(f"Token expires in {session.expires_in}s "
                         f"(proactive reconnect at -{self.refresh_margin_seconds}s)")
        else:
            self.token_expires_at = None
            logging.info("Token lifetime unknown — relying on 4200 reconnect handler")

        return session

    def _needs_token_refresh(self) -> bool:
        """Return True when we should proactively reconnect before token expiry."""
        if not self.token_expires_at:
            return False
        return time.time() >= (self.token_expires_at - self.refresh_margin_seconds)

    async def _token_refresh_task(self, reconnect_event: asyncio.Event) -> None:
        """
        Background task that triggers a graceful reconnect when the token is
        approaching expiry.  Relies on the caller's reconnect_event so that the
        Consumer/Producer are rebuilt together with the new session.
        """
        while not self._shutdown.is_set() and not reconnect_event.is_set():
            try:
                if self._needs_token_refresh():
                    logging.info("Token approaching expiry — triggering proactive reconnect")
                    reconnect_event.set()
                    break
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in token refresh task: {e}")
                await asyncio.sleep(60)

    def _position(self):
        # Only process messages that arrive after this container starts.
        # Never fall back to beginning() — that would replay old challenges.
        if not hasattr(volga.Position, 'latest'):
            raise RuntimeError(
                "volga.Position.latest() is not available in this client version. "
                "Refusing to start to avoid replaying old ACME challenges."
            )
        logging.info("Consumer position: latest (skipping historical messages)")
        return volga.Position.latest()

    def _is_token_expired_error(self, e: Exception) -> bool:
        err = str(e)
        return "4200" in err or "Token expired" in err or "token expired" in err

    async def _process_message(
        self,
        payload: dict,
        producer,
        producer_lock: asyncio.Lock,
        reconnect_event: asyncio.Event,
    ) -> None:
        """Handle one ACME challenge message in a background task."""
        try:
            ack = await self.handle_message(payload)
            async with producer_lock:
                await producer.produce(ack)
            logging.info(f"Sent acknowledgment: {ack.get('status', 'unknown')}")
        except Exception as e:
            if self._is_token_expired_error(e):
                logging.warning(f"Session token expired in task, triggering reconnect: {e}")
                reconnect_event.set()
            else:
                logging.error(f"Error processing message: {e}")

    async def _cancel_tasks(self, tasks: set) -> None:
        """Cancel and await a set of asyncio tasks."""
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        max_concurrent = int(os.getenv("ACME_MAX_CONCURRENT", "20"))
        refresh_task = None

        try:
            while not self._shutdown.is_set():
                try:
                    self.session = await self._make_session()

                    # Create reconnect_event first so the refresh task can signal it
                    reconnect_event = asyncio.Event()

                    # (Re)start the token refresh background task
                    if refresh_task and not refresh_task.done():
                        refresh_task.cancel()
                        try:
                            await refresh_task
                        except asyncio.CancelledError:
                            pass
                    refresh_task = asyncio.create_task(
                        self._token_refresh_task(reconnect_event)
                    )

                    create_wait = volga.CreateOptions.wait()
                    create_json = volga.CreateOptions.create(fmt='json')
                    in_topic = volga.Topic.parent(self.topic_in)
                    out_topic = volga.Topic.parent(self.topic_out)

                    logging.info(
                        f"Starting ACME callback service, listening on topic: {self.topic_in} "
                        f"(max {max_concurrent} concurrent requests)"
                    )

                    active_tasks: set[asyncio.Task] = set()
                    producer_lock = asyncio.Lock()
                    semaphore = asyncio.Semaphore(max_concurrent)

                    async with volga.Consumer(
                        session=self.session,
                        consumer_name=f"acme-cf-{self.hostname}",
                        mode=self.consumer_mode,
                        position=self._position(),
                        topic=in_topic,
                        on_no_exists=create_wait,
                    ) as consumer, volga.Producer(
                        session=self.session,
                        producer_name=f"acme-cf-{self.hostname}",
                        topic=out_topic,
                        on_no_exists=create_json,
                    ) as producer:
                        await consumer.more(max_concurrent)
                        logging.info("Ready to process ACME challenge requests...")

                        while not self._shutdown.is_set() and not reconnect_event.is_set():
                            try:
                                msg = await consumer.recv()
                                if not msg:
                                    await asyncio.sleep(0.2)
                                    # Prune completed tasks
                                    active_tasks = {t for t in active_tasks if not t.done()}
                                    continue

                                payload = msg.get("payload", {})
                                domain = payload.get('domain', '')
                                action = payload.get('action', 'unknown')
                                name = payload.get('name', 'unknown')

                                logging.info(f"Received message: {action} for {name} (domain: {domain})")

                                if not self._should_handle_domain(domain):
                                    logging.info(f"Skipping message for domain '{domain}' - not managed by this instance")
                                    await consumer.more(1)
                                    continue

                                logging.info(f"Processing message for managed domain: {domain}")

                                async def _run(p=payload):
                                    async with semaphore:
                                        await self._process_message(
                                            p, producer, producer_lock, reconnect_event
                                        )

                                task = asyncio.create_task(_run())
                                active_tasks.add(task)
                                task.add_done_callback(active_tasks.discard)

                                # Replenish one consumer credit per accepted task
                                await consumer.more(1)

                            except Exception as e:
                                if self._is_token_expired_error(e):
                                    logging.warning(f"Session token expired, reconnecting: {e}")
                                    reconnect_event.set()
                                    break
                                logging.error(f"Error receiving message: {e}")
                                continue

                        # Wait for in-flight tasks before tearing down
                        if active_tasks:
                            logging.info(f"Waiting for {len(active_tasks)} in-flight task(s)...")
                            await asyncio.gather(*active_tasks, return_exceptions=True)

                    if reconnect_event.is_set():
                        logging.info("Reconnecting with a fresh session...")
                        await asyncio.sleep(2)
                        continue

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    if self._shutdown.is_set():
                        break
                    logging.critical(f"Fatal error, exiting so the container can restart: {e}")
                    raise

        finally:
            if refresh_task and not refresh_task.done():
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass
            await self.cf.close()
            logging.info("ACME callback service stopped")

    async def handle_message(self, payload: dict) -> dict:
        """Process one instruction and return an acknowledgement dict."""
        correlation_id = payload.get("id")
        action = (payload.get("action") or "").lower()
        name = payload.get("name")
        value = payload.get("value")
        domain = payload.get("domain")
        ttl = int(payload.get("ttl") or self.default_ttl)

        base_ack = {
            "id": correlation_id,
            "action": action,
            "name": name,
            "value": value,
        }

        try:
            if action not in {"add", "remove"}:
                raise ValueError(f"Unsupported action: {action}")
            if not name or not value:
                raise ValueError("Both 'name' and 'value' are required")

            zone_id, zone_name = await self.cf.find_zone_id(name)

            if action == "add":
                logging.info(f"ADD challenge: {name} in zone {zone_name} (ID: {zone_id})")
                
                # Check for existing records (idempotency)
                existing = await self.cf.list_txt_records(zone_id, name)
                if existing:
                    logging.info(f"Found {len(existing)} existing TXT record(s) for {name}:")
                    for i, rec in enumerate(existing):
                        content = rec.get("content", "")
                        record_id = rec.get("id", "unknown")
                        is_match = "✓ MATCH" if content == value else "✗ different"
                        logging.info(
                            f"  [{i+1}] {record_id}: '{_redact_value(content)}' {is_match}"
                        )
                        
                        if content == value:
                            logging.info(f"Reusing existing TXT record {record_id} for {name}")
                            return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": record_id}
                else:
                    logging.info(f"No existing TXT records found for {name}")
                
                # Create new record
                logging.info(f"Creating new TXT record for {name} with challenge value")
                record_id = await self.cf.create_txt(zone_id, name, value, ttl)
                
                # Log success with diagnostic info
                logging.info(f"✅ ACME challenge record created successfully:")
                logging.info(f"   Domain: {domain}")
                logging.info(f"   FQDN: {name}")
                logging.info(f"   Zone: {zone_name} ({zone_id})")
                logging.info(f"   Record ID: {record_id}")
                logging.info(f"   TTL: {ttl}s")
                logging.info(f"   Challenge Value: {_redact_value(value)}")
                
                # Optional DNS verification for debugging (if environment variable is set)
                if os.getenv("ACME_DEBUG_DNS_VERIFICATION", "").lower() == "true":
                    try:
                        logging.info("🔍 Running DNS verification check (debug mode)...")
                        dns_check = await self.cf.verify_dns_propagation(name, value)
                        attempts = dns_check.get("attempts", 1)

                        if dns_check["validation_issues"]:
                            logging.warning(
                                f"⚠️ DNS verification found potential issues "
                                f"(after {attempts} attempt(s)):"
                            )
                            for issue in dns_check["validation_issues"]:
                                logging.warning(f"   • {issue}")
                        else:
                            logging.info(
                                f"✅ DNS verification passed - no issues detected "
                                f"(attempt {attempts})"
                            )

                        # Log DNS resolution status
                        for server, info in dns_check["dns_resolution"].items():
                            if info.get("success"):
                                status = "✅ Found" if info["has_expected"] else "❌ Missing"
                                logging.info(f"   {server} DNS: {status} challenge value")
                            else:
                                logging.warning(
                                    f"   {server} DNS: ❌ Resolution failed - "
                                    f"{info.get('error', 'unknown')}"
                                )

                    except Exception as e:
                        logging.warning(f"DNS verification check failed: {e}")
                
                return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": record_id}

            else:  # remove
                logging.info(f"REMOVE challenge: {name} in zone {zone_name} (ID: {zone_id})")
                
                existing = await self.cf.list_txt_records(zone_id, name)
                if existing:
                    logging.info(f"Found {len(existing)} existing TXT record(s) for {name}:")
                    for i, rec in enumerate(existing):
                        content = rec.get("content", "")
                        record_id = rec.get("id", "unknown")
                        is_target = "✓ TARGET" if content == value else "✗ different"
                        logging.info(
                            f"  [{i+1}] {record_id}: '{_redact_value(content)}' {is_target}"
                        )
                else:
                    logging.info(f"No existing TXT records found for {name}")
                
                target = next((r for r in existing if r.get("content") == value), None)
                if not target:
                    logging.info(f"Target TXT record with value '{value}' not found - considering removal successful (idempotent)")
                    return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": None}
                
                target_id = target["id"]
                logging.info(f"Removing TXT record {target_id} from {name}")
                await self.cf.delete_record(zone_id, target_id)
                
                logging.info(f"✅ ACME challenge record removed successfully:")
                logging.info(f"   Domain: {domain}")
                logging.info(f"   FQDN: {name}")
                logging.info(f"   Zone: {zone_name} ({zone_id})")
                logging.info(f"   Removed Record ID: {target_id}")
                
                return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": target_id}

        except CloudflareError as e:
            return {**base_ack, "status": "error", "error": {"type": "cloudflare", "message": str(e)}, "zone_id": None, "record_id": None}
        except Exception as e:
            return {**base_ack, "status": "error", "error": {"type": "internal", "message": str(e)}, "zone_id": None, "record_id": None}


# ------------------------------ Entrypoint ------------------------------
async def main():
    worker = AcmeWorker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker._shutdown.set)

    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

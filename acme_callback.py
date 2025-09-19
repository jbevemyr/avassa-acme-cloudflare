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
- Volga configuration: uses hardcoded topic names (acme:requests/acme:events) and shared consumer mode
  for reliable multi-instance operation.
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
            try:
                zones = await self.client.zones.list(name=candidate)
                if zones.result:
                    zone = zones.result[0]
                    return zone.id, zone.name
            except Exception:
                pass  # try next shorter candidate
        raise ValueError(f"No Cloudflare zone found for {fqdn}")

    async def list_txt_records(self, zone_id: str, name: str) -> list:
        records = await self.client.dns.records.list(zone_id=zone_id, type="TXT", name=name)
        return [record.model_dump() for record in records.result]

    async def create_txt(self, zone_id: str, name: str, value: str, ttl: int) -> str:
        logging.info(f"Creating TXT record: {name} = '{value}' (TTL: {ttl})")
        
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
        
    async def verify_dns_propagation(self, name: str, expected_value: str) -> dict:
        """
        Verify DNS propagation for debugging ACME validation failures.
        This helps diagnose common issues like DNS propagation delays,
        multiple conflicting records, or incorrect values.
        """
        try:
            import socket
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
            "validation_issues": []
        }
        
        try:
            # Get zone for this name
            zone_id, zone_name = await self.find_zone_id(name)
            
            # Check Cloudflare records
            records = await self.list_txt_records(zone_id, name)
            result["cloudflare_records"] = [
                {"id": r.get("id"), "content": r.get("content"), "ttl": r.get("ttl")} 
                for r in records
            ]
            
            # Check DNS resolution from different perspectives
            dns_servers = [
                ("Cloudflare", "1.1.1.1"),
                ("Google", "8.8.8.8"),
                ("System", None)  # Use system resolver
            ]
            
            for server_name, server_ip in dns_servers:
                try:
                    resolver = dns.resolver.Resolver()
                    if server_ip:
                        resolver.nameservers = [server_ip]
                    
                    answers = resolver.resolve(name, 'TXT')
                    txt_values = [str(rdata).strip('"') for rdata in answers]
                    
                    result["dns_resolution"][server_name] = {
                        "success": True,
                        "values": txt_values,
                        "has_expected": expected_value in txt_values,
                        "server": server_ip or "system"
                    }
                    
                except Exception as e:
                    result["dns_resolution"][server_name] = {
                        "success": False,
                        "error": str(e),
                        "server": server_ip or "system"
                    }
            
            # Analyze for common issues
            cf_values = [r["content"] for r in result["cloudflare_records"]]
            
            if not cf_values:
                result["validation_issues"].append("No TXT records found in Cloudflare")
            elif expected_value not in cf_values:
                result["validation_issues"].append(f"Expected value not found in Cloudflare records")
            
            # Check if DNS resolution is consistent
            dns_servers_with_expected = [
                name for name, info in result["dns_resolution"].items() 
                if info.get("success") and info.get("has_expected")
            ]
            
            if len(dns_servers_with_expected) == 0:
                result["validation_issues"].append("Challenge value not resolved by any tested DNS servers")
            elif len(dns_servers_with_expected) < len([s for s in result["dns_resolution"].values() if s.get("success")]):
                result["validation_issues"].append("Challenge value not consistently resolved across DNS servers")
            
            # Check for multiple conflicting records
            if len(cf_values) > 1:
                unique_values = set(cf_values)
                if len(unique_values) > 1:
                    result["validation_issues"].append(f"Multiple conflicting TXT records found: {unique_values}")
                    
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
        self.consumer_mode = "shared"  # Shared mode for multi-instance deployment
        self.position_pref = "latest"
        
        # Domain filtering for multi-instance deployment
        managed_domains_str = os.getenv("MANAGED_DOMAINS", "")
        self.managed_domains = set()
        if managed_domains_str:
            self.managed_domains = {domain.strip().lower() for domain in managed_domains_str.split(",") if domain.strip()}
            logging.info(f"This instance manages domains: {', '.join(sorted(self.managed_domains))}")
        else:
            logging.info("This instance manages ALL domains (no domain filtering)")
        self.hostname = socket.gethostname()
        self._shutdown = asyncio.Event()

        # Token management
        self.session: Optional[avassa_client.Session] = None
        self.token: Optional[str] = None
        self.token_expires_at: Optional[float] = None
        self.refresh_margin_seconds = 300  # Refresh 5 minutes before expiry
        
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
        """Create a new session and track token expiration."""
        logging.info("Creating new Avassa session...")
        session = avassa_client.approle_login(
            host=self.api_host,
            role_id=self.role_id,
            secret_id=self.approle_secret,
            user_agent="volga-acme-cloudflare/1.0",
        )
        
        # Extract token information for refresh tracking
        # Note: The avassa_client might store this internally, 
        # but we need to track it for refresh logic
        if hasattr(session, 'token'):
            self.token = session.token
        if hasattr(session, 'expires_in'):
            self.token_expires_at = time.time() + session.expires_in
            logging.info(f"Token expires in {session.expires_in} seconds")
        
        return session

    def _needs_token_refresh(self) -> bool:
        """Check if token needs to be refreshed."""
        if not self.token_expires_at:
            return False
        return time.time() >= (self.token_expires_at - self.refresh_margin_seconds)

    async def _refresh_token(self) -> bool:
        """Refresh the Avassa API token using the refresh endpoint."""
        if not self.session or not self.token:
            logging.warning("Cannot refresh token: no active session or token")
            return False
            
        try:
            # Use the session's internal HTTP client to make the refresh request
            refresh_url = f"{self.api_host}/v1/state/strongbox/token/refresh"
            
            # Create headers for the request
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            
            # Use the avassa_client session's HTTP functionality for token refresh
            # The avassa_client should have built-in token refresh capabilities
            if hasattr(self.session, 'refresh_token'):
                # If the session has built-in refresh, use it
                new_session = await self.session.refresh_token()
                if new_session:
                    self.session = new_session
                    if hasattr(new_session, 'token'):
                        self.token = new_session.token
                    if hasattr(new_session, 'expires_in'):
                        self.token_expires_at = time.time() + new_session.expires_in
                    logging.info("Token refreshed successfully using session refresh")
                    return True
            
            # Fallback: create a new session (this will get a fresh token)
            logging.info("Refreshing token by creating new session...")
            old_session = self.session
            self.session = await self._make_session()
            
            # Clean up old session if possible
            if hasattr(old_session, 'close'):
                try:
                    await old_session.close()
                except:
                    pass
            
            logging.info("Token refreshed successfully with new session")
            return True
                        
        except Exception as e:
            logging.error(f"Error refreshing token: {e}")
            return False

    async def _token_refresh_task(self):
        """Background task to monitor and refresh tokens."""
        while not self._shutdown.is_set():
            try:
                if self._needs_token_refresh():
                    logging.info("Token needs refresh, attempting to refresh...")
                    success = await self._refresh_token()
                    if not success:
                        logging.error("Token refresh failed, service may become unavailable")
                
                # Check every 60 seconds
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in token refresh task: {e}")
                await asyncio.sleep(60)

    def _position(self):
        # Always start from latest messages for ACME challenges
        try:
            return volga.Position.latest()
        except AttributeError:
            # Fallback if older client lacks latest()
            return volga.Position.beginning()

    async def run(self):
        self.session = await self._make_session()
        
        # Start the token refresh background task
        refresh_task = asyncio.create_task(self._token_refresh_task())
        
        try:
            create_wait = volga.CreateOptions.wait()
            create_json = volga.CreateOptions.create(fmt='json')
            in_topic = volga.Topic.local(self.topic_in)
            out_topic = volga.Topic.local(self.topic_out)

            logging.info(f"Starting ACME callback service, listening on topic: {self.topic_in}")
            
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
                await consumer.more(10)
                logging.info("Ready to process ACME challenge requests...")
                
                while not self._shutdown.is_set():
                    try:
                        msg = await consumer.recv()
                        if not msg:
                            # No more messages, small delay and continue waiting
                            await asyncio.sleep(0.2)
                            continue
                            
                        payload = msg.get("payload", {})
                        domain = payload.get('domain', '')
                        action = payload.get('action', 'unknown')
                        name = payload.get('name', 'unknown')
                        
                        logging.info(f"Received message: {action} for {name} (domain: {domain})")
                        
                        # Check if this instance should handle this domain
                        if not self._should_handle_domain(domain):
                            logging.info(f"Skipping message for domain '{domain}' - not managed by this instance")
                            # Don't send acknowledgment, let other instances handle it
                            continue
                        
                        logging.info(f"Processing message for managed domain: {domain}")
                        
                        # Process one message and publish result
                        ack = await self.handle_message(payload)
                        await producer.produce(ack)
                        
                        logging.info(f"Sent acknowledgment: {ack.get('status', 'unknown')}")
                        
                    except Exception as e:
                        logging.error(f"Error processing message: {e}")
                        # Continue processing other messages
                        continue
                        
        finally:
            # Clean up
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
                        logging.info(f"  [{i+1}] {record_id}: '{content}' {is_match}")
                        
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
                logging.info(f"   Challenge Value: {value}")
                
                # Optional DNS verification for debugging (if environment variable is set)
                if os.getenv("ACME_DEBUG_DNS_VERIFICATION", "").lower() == "true":
                    try:
                        logging.info("🔍 Running DNS verification check (debug mode)...")
                        await asyncio.sleep(2)  # Allow some propagation time
                        dns_check = await self.cf.verify_dns_propagation(name, value)
                        
                        if dns_check["validation_issues"]:
                            logging.warning("⚠️ DNS verification found potential issues:")
                            for issue in dns_check["validation_issues"]:
                                logging.warning(f"   • {issue}")
                        else:
                            logging.info("✅ DNS verification passed - no issues detected")
                            
                        # Log DNS resolution status
                        for server, info in dns_check["dns_resolution"].items():
                            if info["success"]:
                                status = "✅ Found" if info["has_expected"] else "❌ Missing"
                                logging.info(f"   {server} DNS: {status} challenge value")
                            else:
                                logging.warning(f"   {server} DNS: ❌ Resolution failed - {info.get('error', 'unknown')}")
                                
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
                        logging.info(f"  [{i+1}] {record_id}: '{content}' {is_target}")
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

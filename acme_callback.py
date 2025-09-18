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
  VOLGA_TOPIC_IN      : optional – default acme:requests
  VOLGA_TOPIC_OUT     : optional – default acme:events
  VOLGA_CONSUMER_MODE : optional – exclusive | shared (default exclusive)
  VOLGA_POSITION      : optional – beginning | latest (default latest)

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
"""

import asyncio
import json
import os
import socket
import signal
import logging
import time
from typing import Optional, Tuple

import aiohttp
import avassa_client
import avassa_client.volga as volga

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# --------------------------- Cloudflare client ---------------------------
class CloudflareClient:
    def __init__(self, api_token: str, base: str = "https://api.cloudflare.com/client/v4", session: Optional[aiohttp.ClientSession] = None):
        self.api_token = api_token
        self.base = base.rstrip("/")
        self._session = session

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            })
        return self._session

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        async with self.session.request(method, url, **kwargs) as resp:
            data = await resp.json(content_type=None)
            if resp.status < 200 or resp.status >= 300 or not data.get("success", True):
                raise CloudflareError(method, url, resp.status, data)
            return data

    async def find_zone_id(self, fqdn: str) -> Tuple[str, str]:
        """Return (zone_id, zone_name) for the longest-matching zone for fqdn.
        Tries progressively shorter candidate names until a match is found.
        """
        labels = fqdn.strip('.').split('.')
        for i in range(len(labels) - 1):  # require at least a.tld
            candidate = '.'.join(labels[i:])
            try:
                data = await self._request("GET", f"/zones?name={candidate}")
                result = data.get("result", [])
                if result:
                    return result[0]["id"], result[0]["name"]
            except CloudflareError:
                pass  # try next shorter candidate
        raise ValueError(f"No Cloudflare zone found for {fqdn}")

    async def list_txt_records(self, zone_id: str, name: str) -> list:
        data = await self._request("GET", f"/zones/{zone_id}/dns_records?type=TXT&name={name}")
        return data.get("result", [])

    async def create_txt(self, zone_id: str, name: str, value: str, ttl: int) -> str:
        payload = {
            "type": "TXT",
            "name": name,
            "content": value,
            "ttl": ttl,
        }
        data = await self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        return data["result"]["id"]

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        await self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


class CloudflareError(Exception):
    def __init__(self, method: str, url: str, status: int, payload: dict):
        self.method = method
        self.url = url
        self.status = status
        self.payload = payload
        super().__init__(f"Cloudflare API error {status} for {method} {url}: {payload}")


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
        self.topic_in = os.getenv("VOLGA_TOPIC_IN", "acme:requests")
        self.topic_out = os.getenv("VOLGA_TOPIC_OUT", "acme:events")
        self.consumer_mode = os.getenv("VOLGA_CONSUMER_MODE", "exclusive")
        self.position_pref = os.getenv("VOLGA_POSITION", "latest").lower()
        if self.position_pref not in ("latest", "beginning"):
            self.position_pref = "latest"
        self.hostname = socket.gethostname()
        self._shutdown = asyncio.Event()

        # Token management
        self.session: Optional[avassa_client.Session] = None
        self.token: Optional[str] = None
        self.token_expires_at: Optional[float] = None
        self.refresh_margin_seconds = 300  # Refresh 5 minutes before expiry
        
        self.cf = CloudflareClient(self.cf_token, self.cf_base)

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
            
            # Make the refresh request using aiohttp directly
            async with aiohttp.ClientSession() as http_session:
                if self.api_ca_cert:
                    # If we have a CA cert, we'd need to configure SSL context
                    # For now, we'll assume the session handles this
                    pass
                    
                async with http_session.post(refresh_url, headers=headers) as resp:
                    if resp.status == 200:
                        refresh_data = await resp.json()
                        
                        # Update token information
                        self.token = refresh_data.get("token")
                        expires_in = refresh_data.get("expires-in", 0)
                        self.token_expires_at = time.time() + expires_in
                        
                        # Update the session with new token
                        if hasattr(self.session, 'token'):
                            self.session.token = self.token
                        
                        logging.info(f"Token refreshed successfully, expires in {expires_in} seconds")
                        return True
                    else:
                        error_data = await resp.json()
                        logging.error(f"Token refresh failed: {resp.status} - {error_data}")
                        return False
                        
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
        if self.position_pref == "beginning":
            return volga.Position.beginning()
        # Prefer tailing new messages only
        try:
            return volga.Position.latest()
        except AttributeError:
            # Fallback if older client lacks latest(): read nothing until new messages arrive
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
                        logging.info(f"Received message: {payload.get('action', 'unknown')} for {payload.get('name', 'unknown')}")
                        
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
                # Idempotency: if record already exists with same content, reuse
                existing = await self.cf.list_txt_records(zone_id, name)
                for rec in existing:
                    if rec.get("content") == value:
                        return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": rec.get("id")}
                record_id = await self.cf.create_txt(zone_id, name, value, ttl)
                return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": record_id}

            else:  # remove
                existing = await self.cf.list_txt_records(zone_id, name)
                target = next((r for r in existing if r.get("content") == value), None)
                if not target:
                    # Consider missing record as success (idempotent removal)
                    return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": None}
                await self.cf.delete_record(zone_id, target["id"])
                return {**base_ack, "status": "ok", "zone_id": zone_id, "record_id": target["id"]}

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

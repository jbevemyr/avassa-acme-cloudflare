"""Client for ICA's (unofficial) mobile app API.

Implements the OAuth2 flow used by the ICA app: a dynamic client registration
(DCR) followed by an authorization-code grant with PKCE, where the login step
posts the user's credentials to Curity's HTML-form authenticator. The flow was
reverse engineered by the LazyTarget/ha-ica-todo Home Assistant integration;
this is a standalone port with no Home Assistant dependencies.

Notes:
  * The API gateway (apimgw-pub.ica.se) only answers requests from Swedish
    IP addresses (HTTP 451 otherwise).
  * The account must support username/password login (personnummer + password).
    BankID-only accounts cannot be used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests

_LOGGER = logging.getLogger(__name__)

IMS_BASE = "https://ims.icagruppen.se"
API_BASE = "https://apimgw-pub.ica.se"

OAUTH2_AUTHORIZE_URL = f"{IMS_BASE}/oauth/v2/authorize"
OAUTH2_TOKEN_URL = f"{IMS_BASE}/oauth/v2/token"
LOGIN_URL = f"{IMS_BASE}/authn/authenticate/IcaCustomers"
APP_REGISTRATION_URL = f"{IMS_BASE}/register"

# Public credentials embedded in the ICA app, used only to register a
# per-installation OAuth client via dynamic client registration.
DCR_CLIENT_ID = "ica-app-dcr-registration"
DCR_CLIENT_SECRET = "uxLHTBvZ-Z2fV-SbrHl1E-tz7vB3jQFrwAdSLlbVMMu1rxDdvJU0s8KGu9d1wLS4"
DCR_SOFTWARE_ID = "dcr-ica-app-template"

REDIRECT_URI = "icacurity://app"
ACR = "urn:se:curity:authentication:html-form:IcaCustomers"

LISTS_URL = f"{API_BASE}/sverige/digx/mobile/shoppinglistservice/v1/shoppinglists"
LIST_URL = LISTS_URL + "/{}"
LIST_SYNC_URL = LISTS_URL + "/{}/sync"

DEFAULT_ARTICLE_GROUP_ID = 12  # "Övrigt" / unspecified

TOKEN_EXPIRY_MARGIN = timedelta(seconds=60)


class IcaError(Exception):
    """Base error for the ICA client."""


class IcaAuthError(IcaError):
    """Raised when authentication fails (e.g. bad credentials)."""


class IcaApiError(IcaError):
    """Raised when an API call fails."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class IcaClient:
    """Authenticates against ICA and manipulates shopping lists."""

    def __init__(self, username: str, password: str, state_file: str) -> None:
        self._username = username
        self._password = password
        self._state_file = state_file
        self._session = requests.Session()
        self._state: dict[str, Any] = self._load_state()

    # ------------------------------------------------------------------
    # Persisted auth state ({"client": {...}, "token": {...}})
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        directory = os.path.dirname(self._state_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fd = os.open(
            self._state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------

    def _register_app(self) -> dict[str, Any]:
        """Register a per-installation OAuth client (dynamic client registration)."""
        resp = self._session.post(
            OAUTH2_TOKEN_URL,
            data={
                "client_id": DCR_CLIENT_ID,
                "client_secret": DCR_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": "dcr",
                "response_type": "token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        dcr_token = resp.json()["access_token"]

        resp = self._session.post(
            APP_REGISTRATION_URL,
            json={"software_id": DCR_SOFTWARE_ID},
            headers={"Authorization": f"Bearer {dcr_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        verifier = re.sub(
            "[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode()
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return challenge, verifier

    def _full_login(self) -> None:
        """Run the complete login chain and store the resulting tokens."""
        client = self._state.get("client")
        if not client:
            client = self._register_app()
            self._state["client"] = client

        challenge, verifier = self._generate_pkce()

        # Step 1: start the authorization-code flow; the redirect carries
        # the transaction state used by the HTML-form authenticator.
        resp = self._session.get(
            OAUTH2_AUTHORIZE_URL,
            params={
                "client_id": client["client_id"],
                "scope": client["scope"],
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
                "acr": ACR,
            },
            allow_redirects=False,
            timeout=30,
        )
        resp.raise_for_status()
        location = resp.headers.get("Location")
        if not location:
            raise IcaAuthError("Expected a redirect from the authorize endpoint")
        state_match = re.search(r"&state=(\w*)", location)
        if not state_match:
            raise IcaAuthError(f"No state parameter in redirect: {location}")
        state = state_match[1]
        self._session.get(urljoin(IMS_BASE, location), timeout=30).raise_for_status()

        # Step 2: post the credentials to the HTML-form authenticator.
        resp = self._session.post(
            LOGIN_URL,
            data={"userName": self._username, "password": self._password},
            timeout=30,
        )
        if resp.status_code == 400:
            raise IcaAuthError("Login rejected — check username/password")
        resp.raise_for_status()
        try:
            api_state = re.search(
                r'<input type="hidden" name="state" value="(\w*)', resp.text
            )[1]
            form_token = re.search(
                r'<input type="hidden" name="token" value="(\w*)', resp.text
            )[1]
        except TypeError as err:
            raise IcaAuthError(
                "Login response did not contain the expected form. "
                "Wrong credentials, or the account requires BankID."
            ) from err
        if api_state != state:
            _LOGGER.warning("OAuth state mismatch: %s != %s", state, api_state)

        # Step 3: hand the form token back to the authorize endpoint to get
        # an authorization code, then exchange it for tokens.
        resp = self._session.post(
            OAUTH2_AUTHORIZE_URL,
            params={
                "client_id": client["client_id"],
                "forceAuthN": "true",
                "acr": ACR,
            },
            data={"token": form_token, "state": api_state},
            allow_redirects=False,
            timeout=30,
        )
        resp.raise_for_status()
        location = resp.headers.get("Location")
        if not location:
            raise IcaAuthError("Expected a redirect with the authorization code")
        code_match = re.search(r"&code=(\w*)", location)
        if not code_match:
            raise IcaAuthError(f"No authorization code in redirect: {location}")

        resp = self._session.post(
            OAUTH2_TOKEN_URL,
            data={
                "code": code_match[1],
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "grant_type": "authorization_code",
                "scope": client["scope"],
                "response_type": "token",
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._store_token(resp.json())

    def _refresh_token(self) -> None:
        client = self._state["client"]
        basic = base64.b64encode(
            f"{client['client_id']}:{client['client_secret']}".encode()
        ).decode()
        resp = self._session.post(
            OAUTH2_TOKEN_URL,
            headers={"Authorization": f"Basic {basic}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._state["token"]["refresh_token"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = dict(self._state["token"])
        token.update(resp.json())
        self._store_token(token)

    def _store_token(self, token: dict[str, Any]) -> None:
        expiry = datetime.now(timezone.utc) + timedelta(
            seconds=token.get("expires_in", 600)
        )
        token["expiry"] = expiry.isoformat()
        self._state["token"] = token
        self._save_state()

    def ensure_login(self) -> None:
        """Make sure we hold a non-expired access token."""
        token = self._state.get("token")
        if not token or not self._state.get("client"):
            self._full_login()
            return
        expiry = datetime.fromisoformat(token["expiry"])
        if expiry - TOKEN_EXPIRY_MARGIN > datetime.now(timezone.utc):
            return
        try:
            self._refresh_token()
        except requests.exceptions.HTTPError as err:
            status = err.response.status_code if err.response is not None else None
            _LOGGER.info("Token refresh failed (%s), doing a full login", status)
            self._state.pop("token", None)
            self._full_login()

    # ------------------------------------------------------------------
    # API requests
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        json_body: Any = None,
        _retry: bool = True,
    ) -> Any:
        self.ensure_login()
        headers = {
            "Authorization": f"Bearer {self._state['token']['access_token']}"
        }
        resp = self._session.request(
            method, url, json=json_body, headers=headers, timeout=30
        )
        if resp.status_code == 401 and _retry:
            self._state.pop("token", None)
            self._full_login()
            return self._request(method, url, json_body, _retry=False)
        if resp.status_code == 451:
            raise IcaApiError(
                "ICA's API gateway returned HTTP 451: it only accepts requests "
                "from Swedish IP addresses."
            )
        if not resp.ok:
            raise IcaApiError(
                f"{method} {url} failed with {resp.status_code}: {resp.text[:500]}"
            )
        if resp.content and "json" in resp.headers.get("Content-Type", ""):
            return resp.json()
        return None

    # ------------------------------------------------------------------
    # Shopping lists
    # ------------------------------------------------------------------

    def get_shopping_lists(self) -> list[dict[str, Any]]:
        data = self._request("GET", LISTS_URL)
        if isinstance(data, dict):
            data = data.get("shoppingLists", data.get("ShoppingLists", []))
        return data or []

    def get_shopping_list(self, offline_id: str) -> dict[str, Any]:
        return self._request("GET", LIST_URL.format(offline_id))

    def create_shopping_list(self, title: str, comment: str = "") -> dict[str, Any]:
        offline_id = str(uuid.uuid4())
        self._request(
            "POST",
            LISTS_URL,
            json_body={
                "offlineId": offline_id,
                "title": title,
                "commentText": comment,
                "sortingStore": 1,
                "rows": [],
                "latestChange": _utcnow_iso(),
            },
        )
        return self.get_shopping_list(offline_id)

    def sync_shopping_list(self, offline_id: str, sync_data: dict[str, Any]) -> Any:
        return self._request(
            "POST", LIST_SYNC_URL.format(offline_id), json_body=sync_data
        )

    def add_items(
        self, offline_id: str, items: list[dict[str, Any]]
    ) -> Any:
        """Add items to a list.

        Each item is a dict with "name" and optional "quantity"/"unit".
        """
        current = self.get_shopping_list(offline_id)
        order = (
            max(
                (row.get("internalOrder") or 0)
                for row in current.get("rows") or [{}]
            )
            if current.get("rows")
            else 0
        )
        rows = []
        for i, item in enumerate(items, start=1):
            row: dict[str, Any] = {
                "offlineId": str(uuid.uuid4()),
                "productName": item["name"],
                "isStrikedOver": False,
                "sourceId": -1,
                "internalOrder": order + i,
                "articleGroupId": DEFAULT_ARTICLE_GROUP_ID,
                "articleGroupIdExtended": DEFAULT_ARTICLE_GROUP_ID,
                "latestChange": _utcnow_iso(),
            }
            if item.get("quantity") is not None:
                row["quantity"] = item["quantity"]
            if item.get("unit"):
                row["unit"] = item["unit"]
            rows.append(row)
        return self.sync_shopping_list(offline_id, {"createdRows": rows})

    def delete_items(self, offline_id: str, row_offline_ids: list[str]) -> Any:
        return self.sync_shopping_list(
            offline_id, {"deletedRows": row_offline_ids}
        )

    def set_items_striked(
        self, offline_id: str, rows: list[dict[str, Any]], striked: bool
    ) -> Any:
        changed = []
        for row in rows:
            row = dict(row)
            row["isStrikedOver"] = striked
            row["latestChange"] = _utcnow_iso()
            changed.append(row)
        return self.sync_shopping_list(offline_id, {"changedRows": changed})

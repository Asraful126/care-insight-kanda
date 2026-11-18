"""Minimal Fabric REST client for the project scripts.

Auth: AzureCliCredential, i.e. whoever is signed in to the project-local az profile
(AZURE_CONFIG_DIR=<repo>/.azure). No secrets in code or .env. In CI this will become a
service principal / managed identity (Week 2).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
# Never fall back to the global az profile (it belongs to a different tenant).
os.environ.setdefault("AZURE_CONFIG_DIR", str(ROOT / ".azure"))

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"


def credential() -> AzureCliCredential:
    return AzureCliCredential(tenant_id=os.environ["AZURE_TENANT_ID"])


class Fabric:
    def __init__(self) -> None:
        self.cred = credential()
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        token = self.cred.get_token(FABRIC_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        resp = self.session.request(method, url, headers=self._headers(), timeout=60, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {resp.status_code}: {resp.text}")
        return resp

    def wait(self, resp: requests.Response, poll: int = 5, timeout: int = 900) -> dict | None:
        """Follow a long-running operation (202 + Location) until it finishes."""
        if resp.status_code != 202:
            return resp.json() if resp.content else None
        location = resp.headers["Location"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(int(resp.headers.get("Retry-After", poll)))
            resp = self.request("GET", location)
            body = resp.json() if resp.content else {}
            status = body.get("status")
            if status in ("Succeeded", "Completed"):
                if "/operations/" in location:
                    # Result of a create/update LRO lives at <operation>/result (may be empty).
                    try:
                        return self.request("GET", f"{location}/result").json()
                    except RuntimeError:
                        return body
                return body
            if status in ("Failed", "Cancelled", "Deduped"):
                raise RuntimeError(f"Operation {status}: {body}")
        raise TimeoutError(f"Operation did not finish in {timeout}s: {location}")

    # --- lookups -------------------------------------------------------------

    def workspace_id(self, name: str) -> str:
        for ws in self.request("GET", "/workspaces").json()["value"]:
            if ws["displayName"] == name:
                return ws["id"]
        raise LookupError(
            f"Workspace '{name}' not visible to the signed-in account. "
            "Sign in to the project az profile as the kanda@ user (see README)."
        )

    def item(self, ws_id: str, item_type: str, name: str) -> dict | None:
        items = self.request("GET", f"/workspaces/{ws_id}/items?type={item_type}").json()["value"]
        return next((i for i in items if i["displayName"] == name), None)

    def ensure_lakehouse(self, ws_id: str, name: str) -> str:
        found = self.item(ws_id, "Lakehouse", name)
        if found:
            return found["id"]
        # Schemas (kanda_bronze, kanda_silver, …) can only be enabled when the lakehouse is created.
        resp = self.request(
            "POST", f"/workspaces/{ws_id}/lakehouses",
            json={"displayName": name, "creationPayload": {"enableSchemas": True}},
        )
        self.wait(resp)
        return self.item(ws_id, "Lakehouse", name)["id"]


def names() -> tuple[str, str]:
    return os.environ["FABRIC_WORKSPACE"], os.environ["FABRIC_LAKEHOUSE"]

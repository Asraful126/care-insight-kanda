"""Deploy the Power BI semantic model from TMDL in the repo, reframe it, and check it.

Source of truth: fabric/semantic_model/sm_care_insights.SemanticModel/ (TMDL, the same folder layout
Fabric Git integration uses). The OneLake path in expressions.tmdl has {{WORKSPACE_ID}} and
{{LAKEHOUSE_ID}} placeholders, filled in here from the workspace and lakehouse names in .env,
so no IDs are committed.

Steps:
1. Create the semantic model, or replace its definition if it already exists.
2. Bind its OneLake data source to a shareable cloud connection that uses sp-care-insights-model
   (MODEL_CLIENT_ID / MODEL_CLIENT_SECRET in .env). Created on first run, credentials re-applied after.
3. Refresh it. For Direct Lake this is "framing": it points the model at the latest Delta versions;
   no data is copied.
4. Check: every table's row count in the model (DAX) equals COUNT(*) on the SQL endpoint.

Usage: .venv/bin/python scripts/deploy_semantic_model.py   (signed in to the project az profile as kanda@)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from assistant.db import connect  # noqa: E402
from fabric_client import Fabric, credential, names  # noqa: E402

MODEL_NAME = "sm_care_insights"
MODEL_DIR = ROOT / "fabric/semantic_model/sm_care_insights.SemanticModel"
PBI = "https://api.powerbi.com/v1.0/myorg"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
GOLD = "kanda_gold"
CONNECTION_NAME = "conn-onelake-sm-care-insights"


def definition(ws_id: str, lh_id: str) -> dict:
    parts = []
    for path in sorted(MODEL_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text().replace("{{WORKSPACE_ID}}", ws_id).replace("{{LAKEHOUSE_ID}}", lh_id)
        parts.append({
            "path": path.relative_to(MODEL_DIR).as_posix(),
            "payload": base64.b64encode(text.encode()).decode(),
            "payloadType": "InlineBase64",
        })
    return {"parts": parts}


def model_tables() -> list[str]:
    return sorted(p.stem for p in (MODEL_DIR / "definition/tables").glob("*.tmdl"))


def pbi(method: str, path: str, **kwargs) -> requests.Response:
    token = credential().get_token(PBI_SCOPE).token
    resp = requests.request(method, f"{PBI}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=120, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def bind_identity(fab: Fabric, ws_id: str, lh_id: str, model_id: str) -> None:
    """Fixed identity: the model reads OneLake as sp-care-insights-model through a shareable cloud
    connection, never as the viewer. Viewers then need only Read on the model, not OneLake, so they
    can't go around RLS, and refresh doesn't depend on anyone's MFA'd user token."""
    server, path = "onelake.dfs.fabric.microsoft.com", f"/{ws_id}/{lh_id}/"
    creds = {
        "credentialType": "ServicePrincipal",
        "tenantId": os.environ["AZURE_TENANT_ID"],
        "servicePrincipalClientId": os.environ["MODEL_CLIENT_ID"],
        "servicePrincipalSecret": os.environ["MODEL_CLIENT_SECRET"],
    }
    cred_details = {"singleSignOnType": "None", "connectionEncryption": "NotEncrypted",
                    "skipTestConnection": False, "credentials": creds}
    conn = next((c for c in fab.request("GET", "/connections").json()["value"]
                 if c["displayName"] == CONNECTION_NAME), None)
    if conn:  # re-apply credentials so a rotated secret in .env takes effect
        fab.request("PATCH", f"/connections/{conn['id']}", json={
            "connectivityType": "ShareableCloud", "privacyLevel": "Organizational", "credentialDetails": cred_details})
    else:
        conn = fab.request("POST", "/connections", json={
            "connectivityType": "ShareableCloud",
            "displayName": CONNECTION_NAME,
            "privacyLevel": "Organizational",
            "connectionDetails": {
                "type": "AzureDataLakeStorage", "creationMethod": "AzureDataLakeStorage",
                "parameters": [{"dataType": "Text", "name": "server", "value": server},
                               {"dataType": "Text", "name": "path", "value": path}],
            },
            "credentialDetails": cred_details,
        }).json()
    fab.request("POST", f"/workspaces/{ws_id}/semanticModels/{model_id}/bindConnection", json={
        "connectionBinding": {
            "id": conn["id"], "connectivityType": "ShareableCloud",
            "connectionDetails": {"type": "AzureDataLakeStorage", "path": f"https://{server}{path}"},
        }
    })


def refresh(ws_id: str, model_id: str, timeout: int = 600) -> None:
    pbi("POST", f"/groups/{ws_id}/datasets/{model_id}/refreshes", json={"type": "full"})
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        last = pbi("GET", f"/groups/{ws_id}/datasets/{model_id}/refreshes?$top=1").json()["value"][0]
        if last["status"] == "Completed":
            return
        if last["status"] in ("Failed", "Disabled", "Cancelled"):
            raise RuntimeError(f"Refresh {last['status']}: {last.get('serviceExceptionJson')}")
    raise TimeoutError("Refresh did not finish")


def dax_row_counts(ws_id: str, model_id: str, tables: list[str]) -> dict[str, int]:
    cols = ", ".join(f'"{t}", COUNTROWS({t})' for t in tables)
    body = {"queries": [{"query": f"EVALUATE ROW({cols})"}], "serializerSettings": {"includeNulls": True}}
    row = pbi("POST", f"/groups/{ws_id}/datasets/{model_id}/executeQueries", json=body).json()
    row = row["results"][0]["tables"][0]["rows"][0]
    return {k.strip("[]"): v for k, v in row.items()}


def sql_row_counts(tables: list[str]) -> dict[str, int]:
    cur = connect("admin").cursor()
    return {t: cur.execute(f"SELECT COUNT(*) FROM {GOLD}.{t}").fetchone()[0] for t in tables}


def main() -> None:
    fab = Fabric()
    ws_name, lh_name = names()
    ws_id = fab.workspace_id(ws_name)
    lh_id = fab.item(ws_id, "Lakehouse", lh_name)["id"]
    body = definition(ws_id, lh_id)

    existing = fab.item(ws_id, "SemanticModel", MODEL_NAME)
    if existing:
        print(f"1. update definition of {MODEL_NAME} ({len(body['parts'])} parts)")
        model_id = existing["id"]
        fab.wait(fab.request("POST", f"/workspaces/{ws_id}/semanticModels/{model_id}/updateDefinition",
                             json={"definition": body}))
    else:
        print(f"1. create {MODEL_NAME} ({len(body['parts'])} parts)")
        fab.wait(fab.request("POST", f"/workspaces/{ws_id}/semanticModels",
                             json={"displayName": MODEL_NAME, "definition": body}))
        model_id = fab.item(ws_id, "SemanticModel", MODEL_NAME)["id"]

    print(f"2. bind OneLake access to {CONNECTION_NAME} (sp-care-insights-model)")
    bind_identity(fab, ws_id, lh_id, model_id)

    print("3. refresh (Direct Lake framing)")
    refresh(ws_id, model_id)

    print("4. row counts: model vs SQL endpoint")
    tables = model_tables()
    model, sql = dax_row_counts(ws_id, model_id, tables), sql_row_counts(tables)
    bad = 0
    for t in tables:
        ok = model.get(t) == sql[t] and sql[t] > 0
        bad += not ok
        print(f"   {'OK ' if ok else 'BAD'} {t:28} model={model.get(t)} sql={sql[t]}")
    if bad:
        raise SystemExit(f"{bad} table(s) differ or are empty")
    print("all tables match")


if __name__ == "__main__":
    main()

"""Set up row-level security identities and the user → state mapping. Idempotent.

1. Entra security group `sg-care-insights-state-users`: the semantic model's "State access" role member.
   Being in the group lets you open the model; the mapping table decides which states you see.
2. Test users (disabled: they never sign in; they exist so RLS can be tested by impersonation and
   "View as"). Added to the group.
3. The group gets **Read on the semantic model only**: no workspace role, so no route to the lakehouse
   or the SQL endpoint around RLS.
4. Upload config/security_user_state.csv (git-ignored; see the .example file) to
   Files/security/user_state.csv. Then run fabric/notebooks/security_load.py and redeploy the model.

Usage: .venv/bin/python scripts/setup_rls.py   (signed in to the project az profile as kanda@)
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import requests
from azure.storage.filedatalake import DataLakeServiceClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from deploy_semantic_model import MODEL_NAME, pbi  # noqa: E402
from fabric_client import Fabric, credential, names  # noqa: E402

GROUP_NAME = "sg-care-insights-state-users"
TEST_USERS = ["rls.qld", "rls.nswvic", "rls.none"]  # rls.none has no mapping row: proves deny-by-default
MAPPING = ROOT / "config/security_user_state.csv"
GRAPH = "https://graph.microsoft.com/v1.0"


def graph(method: str, path: str, **kwargs) -> requests.Response:
    token = credential().get_token("https://graph.microsoft.com/.default").token
    resp = requests.request(method, f"{GRAPH}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    return resp


def group_id(create: bool = False) -> str:
    found = graph("GET", f"/groups?$filter=displayName eq '{GROUP_NAME}'").json()["value"]
    if found:
        return found[0]["id"]
    if not create:
        raise LookupError(f"Group {GROUP_NAME} not found; run scripts/setup_rls.py")
    return graph("POST", "/groups", json={
        "displayName": GROUP_NAME, "mailNickname": GROUP_NAME, "mailEnabled": False, "securityEnabled": True,
        "description": "Care Insights semantic model: State access role. States per user in kanda_gold.security_user_state.",
    }).json()["id"]


def tenant_domain() -> str:
    return graph("GET", "/me?$select=userPrincipalName").json()["userPrincipalName"].split("@")[1]


def ensure_user(alias: str, domain: str) -> str:
    upn = f"{alias}@{domain}"
    resp = requests.get(f"{GRAPH}/users/{upn}", timeout=60,
                        headers={"Authorization": f"Bearer {credential().get_token('https://graph.microsoft.com/.default').token}"})
    if resp.status_code == 200:
        return resp.json()["id"]
    return graph("POST", "/users", json={
        "accountEnabled": False,  # test identity only: can't sign in
        "displayName": f"RLS test {alias}",
        "mailNickname": alias.replace(".", "-"),
        "userPrincipalName": upn,
        # Random, never stored: the account is disabled and nobody signs in with it.
        "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": secrets.token_urlsafe(24) + "aA1!"},
    }).json()["id"]


def main() -> None:
    print(f"1. group {GROUP_NAME}")
    gid = group_id(create=True)

    print("2. test users (disabled) in the group")
    domain = tenant_domain()
    members = {m["id"] for m in graph("GET", f"/groups/{gid}/members?$select=id").json()["value"]}
    for alias in TEST_USERS:
        uid = ensure_user(alias, domain)
        if uid not in members:
            graph("POST", f"/groups/{gid}/members/$ref", json={"@odata.id": f"{GRAPH}/directoryObjects/{uid}"})
        print(f"   {alias}@<tenant>")

    print("3. group: Read on the semantic model only")
    fab = Fabric()
    ws_name, lh_name = names()
    ws = fab.workspace_id(ws_name)
    model = fab.item(ws, "SemanticModel", MODEL_NAME)["id"]
    users = pbi("GET", f"/groups/{ws}/datasets/{model}/users").json()["value"]
    if not any(u.get("identifier") == gid for u in users):
        pbi("POST", f"/groups/{ws}/datasets/{model}/users",
            json={"identifier": gid, "principalType": "Group", "datasetUserAccessRight": "Read"})
    roles = {r["role"] for r in fab.request("GET", f"/workspaces/{ws}/roleAssignments").json()["value"]
             if r["principal"]["id"] == gid}
    if roles:
        raise SystemExit(f"{GROUP_NAME} has workspace role(s) {roles}: remove them, they bypass RLS")

    print("4. upload mapping to Files/security/user_state.csv")
    if not MAPPING.exists():
        raise SystemExit(f"Create {MAPPING.relative_to(ROOT)} from the .example file first")
    lh = fab.item(ws, "Lakehouse", lh_name)["id"]
    fs = DataLakeServiceClient("https://onelake.dfs.fabric.microsoft.com", credential=credential()).get_file_system_client(ws)
    fs.get_file_client(f"{lh}/Files/security/user_state.csv").upload_data(MAPPING.read_bytes(), overwrite=True)
    print("next: scripts/fabric_notebook.py deploy-run fabric/notebooks/security_load.py, then deploy_semantic_model.py")


if __name__ == "__main__":
    main()

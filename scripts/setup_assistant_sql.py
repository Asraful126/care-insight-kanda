"""Set up the assistant's read-only access to gold. Idempotent; run after every gold rebuild.

1. Refresh the SQL analytics endpoint metadata (it lags behind new Delta tables).
2. Apply fabric/sql/assistant_views.sql (schema kanda_assistant, denormalised views).
3. Give each reader identity the workspace Viewer role (Fabric REST API): the service principal
   (local runs and eval) and, once deployed, the Container App's managed identity (D4: no secret).
4. T-SQL for each: GRANT SELECT on kanda_assistant, DENY SELECT on kanda_gold / kanda_silver / kanda_bronze / dbo.
   The views still work through ownership chaining (same owner as the tables), so the readers
   see gold only through the views.
5. Verify as the SP: every view is readable, every gold/silver/bronze table is denied, writes are denied.

Usage: python scripts/setup_assistant_sql.py   (signed in to the project az profile as the kanda@ user)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pyodbc  # noqa: E402

from assistant.db import connect  # noqa: E402
from fabric_client import Fabric  # noqa: E402

SP_NAME = "sp-care-insights-reader"
# D4: the deployed app reads with its system-assigned managed identity, whose name is the Container App's.
APP_NAME, APP_RG = "ca-care-insights", "rg-care-insights"
VIEW_SCHEMA = "kanda_assistant"
DENIED_SCHEMAS = ["kanda_gold", "kanda_silver", "kanda_bronze", "dbo"]


def sql_batches(path: Path) -> list[str]:
    return [b.strip() for b in re.split(r"^\s*GO\s*$", path.read_text(), flags=re.M) if b.strip()]


def main() -> None:
    fab = Fabric()
    ws = fab.workspace_id(os.environ["FABRIC_WORKSPACE"])
    lh_id = fab.item(ws, "Lakehouse", os.environ["FABRIC_LAKEHOUSE"])["id"]
    lh = fab.request("GET", f"/workspaces/{ws}/lakehouses/{lh_id}").json()
    endpoint_id = lh["properties"]["sqlEndpointProperties"]["id"]

    print("1. refresh SQL endpoint metadata")
    fab.wait(fab.request("POST", f"/workspaces/{ws}/sqlEndpoints/{endpoint_id}/refreshMetadata", json={}), timeout=600)

    print("2. apply views")
    admin = connect("admin")
    cur = admin.cursor()
    for batch in sql_batches(ROOT / "fabric/sql/assistant_views.sql"):
        cur.execute(batch)
    admin.commit()
    cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME", VIEW_SCHEMA
    )
    views = [r[0] for r in cur.fetchall()]
    print("   ", ", ".join(views))

    # Same access for every reader identity: the SP (local runs, eval) and the app's managed identity (Azure).
    readers = [(SP_NAME, os.environ.get("READER_OBJECT_ID") or _sp_object_id())]
    app_identity = _app_identity_object_id()
    if app_identity:
        readers.append((APP_NAME, app_identity))
    else:
        print(f"   (no Container App {APP_NAME} with a managed identity yet: SP only)")

    print("3. workspace Viewer role for each reader identity")
    for name, object_id in readers:
        existing = fab.request("GET", f"/workspaces/{ws}/roleAssignments").json()["value"]
        if not any(a["principal"]["id"] == object_id for a in existing):
            fab.request(
                "POST", f"/workspaces/{ws}/roleAssignments",
                json={"principal": {"id": object_id, "type": "ServicePrincipal"}, "role": "Viewer"},
            )
        role = next(a["role"] for a in fab.request("GET", f"/workspaces/{ws}/roleAssignments").json()["value"]
                    if a["principal"]["id"] == object_id)
        print(f"    {name}: {role}")
        if role != "Viewer":
            raise SystemExit(f"{name} has role {role}, expected Viewer only")

    print("4. GRANT / DENY")
    for name, _ in readers:
        for stmt in [f"GRANT SELECT ON SCHEMA::{VIEW_SCHEMA} TO [{name}]"] + [
            f"DENY SELECT ON SCHEMA::{s} TO [{name}]" for s in DENIED_SCHEMAS
        ]:
            cur.execute(stmt)
            print("   ", stmt)
    admin.commit()

    print("5. verify as the SP")
    reader = connect("reader").cursor()
    failures = []
    for v in views:
        try:
            reader.execute(f"SELECT TOP 1 * FROM {VIEW_SCHEMA}.{v}")
            reader.fetchall()
        except pyodbc.Error as e:
            failures.append(f"view {v} not readable: {e}")
    cur.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA IN ('kanda_gold', 'kanda_silver', 'kanda_bronze')"
    )
    tables = cur.fetchall()
    for schema, table in tables:
        try:
            reader.execute(f"SELECT TOP 1 * FROM {schema}.{table}")
            reader.fetchall()
            failures.append(f"table {schema}.{table} is readable by the SP")
        except pyodbc.Error:
            pass
    try:
        reader.execute(f"CREATE VIEW {VIEW_SCHEMA}.v_probe AS SELECT 1 AS x")
        failures.append("SP could create a view")
    except pyodbc.Error:
        pass
    if failures:
        raise SystemExit("FAILED:\n  " + "\n  ".join(failures))
    print(f"    OK: {len(views)} views readable, {len(tables)} tables denied, DDL denied")


def _app_identity_object_id() -> str | None:
    """principalId of the Container App's system-assigned managed identity, or None if there's no app yet."""
    import subprocess

    out = subprocess.run(
        ["az", "containerapp", "show", "-n", APP_NAME, "-g", APP_RG, "--query", "identity.principalId", "-o", "tsv"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or None


def _sp_object_id() -> str:
    import subprocess

    out = subprocess.run(
        ["az", "ad", "sp", "show", "--id", os.environ["READER_CLIENT_ID"], "--query", "id", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


if __name__ == "__main__":
    main()

"""Connections to the lakehouse SQL analytics endpoint (T-SQL over the Delta tables, read-only).

Two identities, on purpose:
- `admin`: whoever is signed in to the project az profile (the kanda@ user). Used only by
  scripts/setup_assistant_sql.py to create the views and grants.
- `reader`: the service principal `sp-care-insights-reader`. The assistant always uses this one.
  It can SELECT from the kanda_assistant views and nothing else (enforced in the database).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pyodbc
from azure.identity import AzureCliCredential, ClientSecretCredential
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
os.environ.setdefault("AZURE_CONFIG_DIR", str(ROOT / ".azure"))

SQL_SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql pre-connect attribute for an Entra access token


def _credential(identity: str):
    tenant = os.environ["AZURE_TENANT_ID"]
    if identity == "admin":
        return AzureCliCredential(tenant_id=tenant)
    if identity == "reader":
        return ClientSecretCredential(
            tenant_id=tenant,
            client_id=os.environ["READER_CLIENT_ID"],
            client_secret=os.environ["READER_CLIENT_SECRET"],
        )
    raise ValueError(f"unknown identity {identity!r}")


def connect(identity: str = "reader", timeout_s: int = 30) -> pyodbc.Connection:
    token = _credential(identity).get_token(SQL_SCOPE).token.encode("utf-16-le")
    attrs = struct.pack(f"<I{len(token)}s", len(token), token)
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={os.environ['FABRIC_SQL_ENDPOINT']},1433;"
        f"Database={os.environ.get('FABRIC_SQL_DATABASE', 'lh_care_insights')};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: attrs}, timeout=timeout_s)
    conn.timeout = timeout_s  # per-query timeout (seconds)
    return conn

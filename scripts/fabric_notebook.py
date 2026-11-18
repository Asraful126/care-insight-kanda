"""Deploy a notebook from fabric/notebooks/*.py to the workspace, and/or run it.

The .py file is the source of truth (readable diffs, reviewable in a PR). Cells are split on
"# %%" lines; "# %% [markdown]" cells become markdown with the leading "# " removed.
The workspace's lakehouse is attached as the notebook's default lakehouse at deploy time,
so no workspace or item IDs are committed.

Usage:
  .venv/bin/python scripts/fabric_notebook.py deploy fabric/notebooks/bronze_load.py
  .venv/bin/python scripts/fabric_notebook.py run    fabric/notebooks/bronze_load.py
  .venv/bin/python scripts/fabric_notebook.py deploy-run fabric/notebooks/bronze_load.py
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

from fabric_client import Fabric, names


def to_ipynb(source: str, ws_id: str, lh_id: str, lh_name: str) -> dict:
    cells = []
    for chunk in re.split(r"^# %%", source, flags=re.M)[1:]:
        header, _, body = chunk.partition("\n")
        body = body.strip("\n")
        if "[markdown]" in header:
            text = "\n".join(re.sub(r"^# ?", "", line) for line in body.splitlines())
            cells.append({"cell_type": "markdown", "metadata": {}, "source": text})
        else:
            cells.append(
                {"cell_type": "code", "metadata": {}, "source": body, "outputs": [], "execution_count": None}
            )
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "dependencies": {
                "lakehouse": {
                    "default_lakehouse": lh_id,
                    "default_lakehouse_name": lh_name,
                    "default_lakehouse_workspace_id": ws_id,
                }
            },
        },
    }


def deploy(fabric: Fabric, ws_id: str, lh_id: str, lh_name: str, path: Path) -> str:
    nb = to_ipynb(path.read_text(), ws_id, lh_id, lh_name)
    definition = {
        "format": "ipynb",
        "parts": [
            {
                "path": "notebook-content.ipynb",
                "payload": base64.b64encode(json.dumps(nb).encode()).decode(),
                "payloadType": "InlineBase64",
            }
        ],
    }
    name = path.stem
    existing = fabric.item(ws_id, "Notebook", name)
    if existing:
        resp = fabric.request(
            "POST", f"/workspaces/{ws_id}/notebooks/{existing['id']}/updateDefinition",
            json={"definition": definition},
        )
        fabric.wait(resp)
        print(f"updated notebook {name}")
        return existing["id"]
    resp = fabric.request(
        "POST", f"/workspaces/{ws_id}/notebooks", json={"displayName": name, "definition": definition}
    )
    fabric.wait(resp)
    print(f"created notebook {name}")
    return fabric.item(ws_id, "Notebook", name)["id"]


def run(fabric: Fabric, ws_id: str, lh_id: str, lh_name: str, nb_id: str) -> None:
    body = {
        "executionData": {
            "configuration": {"defaultLakehouse": {"name": lh_name, "id": lh_id, "workspaceId": ws_id}}
        }
    }
    resp = fabric.request(
        "POST", f"/workspaces/{ws_id}/items/{nb_id}/jobs/instances?jobType=RunNotebook", json=body
    )
    print("run started; waiting (Spark session start can take a few minutes)…")
    result = fabric.wait(resp, poll=15, timeout=1800)
    print(f"run finished: {result.get('status')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "run", "deploy-run"])
    parser.add_argument("notebook", type=Path)
    args = parser.parse_args()

    ws_name, lh_name = names()
    fabric = Fabric()
    ws_id = fabric.workspace_id(ws_name)
    lakehouse = fabric.item(ws_id, "Lakehouse", lh_name)
    if not lakehouse:
        raise SystemExit(f"Lakehouse {lh_name} not found; run scripts/upload_raw.py first.")
    lh_id = lakehouse["id"]

    if args.action in ("deploy", "deploy-run"):
        nb_id = deploy(fabric, ws_id, lh_id, lh_name, args.notebook)
    else:
        found = fabric.item(ws_id, "Notebook", args.notebook.stem)
        if not found:
            raise SystemExit(f"Notebook {args.notebook.stem} not deployed yet.")
        nb_id = found["id"]
    if args.action in ("run", "deploy-run"):
        run(fabric, ws_id, lh_id, lh_name, nb_id)


if __name__ == "__main__":
    main()

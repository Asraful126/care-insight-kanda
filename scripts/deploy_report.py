"""Deploy the Power BI report from PBIR in the repo, or pull edits made in the browser back into it.

Source of truth: fabric/reports/rpt_care_insights.Report/ (PBIR: one JSON file per page and per visual,
the same layout Fabric Git integration uses). definition.pbir binds the report to sm_care_insights by ID;
the ID is a {{SEMANTIC_MODEL_ID}} placeholder filled in at deploy time, so no IDs are committed.

Usage:
  .venv/bin/python scripts/deploy_report.py deploy [--pdf FILE]  create/update the report; optionally export
                                                                  all pages to a PDF for a visual check
  .venv/bin/python scripts/deploy_report.py pull                  write the service's current definition back
                                                                  into the repo (after editing in the browser)
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from deploy_semantic_model import MODEL_NAME, pbi  # noqa: E402
from fabric_client import Fabric, names  # noqa: E402

REPORT_NAME = "rpt_care_insights"
REPORT_DIR = ROOT / "fabric/reports/rpt_care_insights.Report"
PLACEHOLDER = "{{SEMANTIC_MODEL_ID}}"


def definition(model_id: str) -> dict:
    parts = []
    for path in sorted(REPORT_DIR.rglob("*")):
        if path.is_file():
            text = path.read_text().replace(PLACEHOLDER, model_id)
            parts.append({"path": path.relative_to(REPORT_DIR).as_posix(),
                          "payload": base64.b64encode(text.encode()).decode(), "payloadType": "InlineBase64"})
    return {"parts": parts}


def deploy(fab: Fabric, ws: str, model_id: str) -> str:
    body = definition(model_id)
    existing = fab.item(ws, "Report", REPORT_NAME)
    if existing:
        print(f"update definition of {REPORT_NAME} ({len(body['parts'])} parts)")
        fab.wait(fab.request("POST", f"/workspaces/{ws}/reports/{existing['id']}/updateDefinition",
                             json={"definition": body}))
        return existing["id"]
    print(f"create {REPORT_NAME} ({len(body['parts'])} parts)")
    fab.wait(fab.request("POST", f"/workspaces/{ws}/reports", json={"displayName": REPORT_NAME, "definition": body}))
    return fab.item(ws, "Report", REPORT_NAME)["id"]


def export_pdf(ws: str, report_id: str, out: Path) -> None:
    # PDF, not PNG: "Export reports as image files" is off by default at tenant level.
    job = pbi("POST", f"/groups/{ws}/reports/{report_id}/ExportTo", json={"format": "PDF"}).json()
    for _ in range(60):
        time.sleep(5)
        status = pbi("GET", f"/groups/{ws}/reports/{report_id}/exports/{job['id']}").json()
        if status["status"] == "Succeeded":
            break
        if status["status"] == "Failed":
            raise RuntimeError(f"export failed: {status}")
    else:
        raise TimeoutError("export did not finish")
    data = pbi("GET", f"/groups/{ws}/reports/{report_id}/exports/{job['id']}/file").content
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"exported {len(data):,} bytes to {out}")


def pull(fab: Fabric, ws: str, model_id: str) -> None:
    report = fab.item(ws, "Report", REPORT_NAME)
    result = fab.wait(fab.request("POST", f"/workspaces/{ws}/reports/{report['id']}/getDefinition"))
    for part in result["definition"]["parts"]:
        target = REPORT_DIR / part["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        text = base64.b64decode(part["payload"]).decode().replace(model_id, PLACEHOLDER)
        if part["path"].endswith(".json"):
            text = json.dumps(json.loads(text), indent=2) + "\n"
        target.write_text(text)
    print(f"pulled {len(result['definition']['parts'])} parts into {REPORT_DIR.relative_to(ROOT)}; review with git diff")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "pull"])
    parser.add_argument("--pdf", type=Path, help="export all pages to this PDF file after deploying")
    args = parser.parse_args()

    fab = Fabric()
    ws = fab.workspace_id(names()[0])
    model_id = fab.item(ws, "SemanticModel", MODEL_NAME)["id"]
    if args.action == "pull":
        pull(fab, ws, model_id)
        return
    report_id = deploy(fab, ws, model_id)
    if args.pdf:
        export_pdf(ws, report_id, args.pdf)


if __name__ == "__main__":
    main()

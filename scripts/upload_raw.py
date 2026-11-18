"""Land data/raw/ in the lakehouse as Files/raw/ (bronze landing zone).

1. Verify every local file against data/raw/MANIFEST.tsv (size + SHA-256), so we never land a
   file that differs from what the download script recorded.
2. Create the lakehouse if it does not exist.
3. Upload each file (overwrite) plus MANIFEST.tsv through the OneLake ADLS Gen2 endpoint.

Usage:  .venv/bin/python scripts/upload_raw.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from azure.storage.filedatalake import DataLakeServiceClient

from fabric_client import ROOT, Fabric, credential, names

RAW = ROOT / "data" / "raw"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"


def read_manifest() -> list[tuple[str, int, str]]:
    rows = []
    for line in (RAW / "MANIFEST.tsv").read_text().splitlines():
        path, size, sha = line.split("\t")
        rows.append((path, int(size), sha))
    return rows


def verify(rows: list[tuple[str, int, str]]) -> None:
    bad = []
    for path, size, sha in rows:
        f = RAW / path
        if not f.exists():
            bad.append(f"missing  {path}")
        elif f.stat().st_size != size or hashlib.sha256(f.read_bytes()).hexdigest() != sha:
            bad.append(f"changed  {path}")
    if bad:
        sys.exit("Local files do not match MANIFEST.tsv:\n  " + "\n  ".join(bad))
    print(f"manifest ok: {len(rows)} files")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="verify only, upload nothing")
    args = parser.parse_args()

    rows = read_manifest()
    verify(rows)
    if args.dry_run:
        return

    ws_name, lh_name = names()
    fabric = Fabric()
    ws_id = fabric.workspace_id(ws_name)
    lh_id = fabric.ensure_lakehouse(ws_id, lh_name)
    print(f"lakehouse: {ws_name}/{lh_name} ({lh_id})")

    # OneLake: file system = workspace, directory = <lakehouse id>/Files/...
    fs = DataLakeServiceClient(ONELAKE, credential=credential()).get_file_system_client(ws_id)
    for rel in [r[0] for r in rows] + ["MANIFEST.tsv"]:
        target = f"{lh_id}/Files/raw/{rel}"
        with open(RAW / rel, "rb") as fh:
            fs.get_file_client(target).upload_data(fh, overwrite=True)
        print(f"put   Files/raw/{rel}")
    print("done")


if __name__ == "__main__":
    main()

"""Test the semantic model's measures against the gold tables.

Each case runs a DAX query against the deployed model and the SQL query that reads the same published
cells straight from kanda_gold on the SQL endpoint, then compares them. The SQL is written independently
of the DAX, so a wrong rule in a measure shows up as a mismatch.

Usage: .venv/bin/python scripts/test_measures.py   (after scripts/deploy_semantic_model.py)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from assistant.db import connect  # noqa: E402
from deploy_semantic_model import MODEL_NAME, pbi  # noqa: E402
from fabric_client import Fabric, names  # noqa: E402

LATEST, PREVIOUS = 20260930, 20250930
G = "kanda_gold"

# Published NDIS cell at national level with every cohort dimension on ALL.
NAT = (f"FROM {G}.fact_ndis_plans f JOIN {G}.dim_region r ON r.region_key = f.region_key "
       "WHERE r.region_type = 'NATIONAL' AND f.is_all_disability_group = 1 AND f.is_all_age_band = 1 "
       "AND f.is_all_support_class = 1")
ALL3 = "is_all_disability_group = 1 AND is_all_age_band = 1 AND is_all_support_class = 1"
AGED = f"FROM {G}.fact_aged_care_services a JOIN {G}.dim_region r ON r.region_key = a.region_key"

# (name, DAX table expression returning key + value columns, SQL returning key + value)
# Scalar cases use the key 'x'.
CASES: list[tuple[str, str, str]] = [
    ("no filters: national participants = latest published cell",
     'ROW("k", "x", "v", [Active participants])',
     f"SELECT 'x', active_participants {NAT} AND f.date_key = {LATEST}"),
    ("no filters: national providers",
     'ROW("k", "x", "v", [Active providers])',
     f"SELECT 'x', provider_count FROM {G}.fact_ndis_providers f JOIN {G}.dim_region r ON r.region_key = f.region_key "
     f"WHERE r.region_type = 'NATIONAL' AND {ALL3} AND f.date_key = {LATEST}"),
    ("no filters: national average budget",
     'ROW("k", "x", "v", [Average annualised plan budget])',
     f"SELECT 'x', avg_annualised_budget {NAT} AND f.date_key = {LATEST}"),
    ("no filters: national utilisation (SIL/SDA = ALL)",
     'ROW("k", "x", "v", [Plan utilisation %])',
     f"SELECT 'x', utilisation FROM {G}.fact_ndis_utilisation f JOIN {G}.dim_region r ON r.region_key = f.region_key "
     f"WHERE r.region_type = 'NATIONAL' AND {ALL3} AND is_all_sil_or_sda = 1 AND f.date_key = {LATEST}"),
    ("utilisation, SIL/SDA = Yes",
     'CALCULATETABLE(ROW("k", "x", "v", [Plan utilisation %]), fact_ndis_utilisation[sil_or_sda] = "Yes")',
     f"SELECT 'x', utilisation FROM {G}.fact_ndis_utilisation f JOIN {G}.dim_region r ON r.region_key = f.region_key "
     f"WHERE r.region_type = 'NATIONAL' AND {ALL3} AND sil_or_sda = 'Yes' AND f.date_key = {LATEST}"),
    ("previous date selected",
     f'CALCULATETABLE(ROW("k", "x", "v", [Active participants]), dim_date[date_key] = {PREVIOUS})',
     f"SELECT 'x', active_participants {NAT} AND f.date_key = {PREVIOUS}"),
    ("participants by state (coarsest level = STATE)",
     'FILTER(SUMMARIZECOLUMNS(dim_region[state_code], "v", [Active participants]), dim_region[state_code] <> "AUS")',
     f"SELECT r.state_code, active_participants FROM {G}.fact_ndis_plans f JOIN {G}.dim_region r "
     f"ON r.region_key = f.region_key WHERE r.region_type = 'STATE' AND {ALL3} AND f.date_key = {LATEST}"),
    ("average budget by state",
     'FILTER(SUMMARIZECOLUMNS(dim_region[state_code], "v", [Average annualised plan budget]), dim_region[state_code] <> "AUS")',
     f"SELECT r.state_code, avg_annualised_budget FROM {G}.fact_ndis_plans f JOIN {G}.dim_region r "
     f"ON r.region_key = f.region_key WHERE r.region_type = 'STATE' AND {ALL3} AND f.date_key = {LATEST}"),
    ("QLD service districts (coarsest level = district)",
     'CALCULATETABLE(SUMMARIZECOLUMNS(dim_region[region_key], "v", [Active participants]), '
     'dim_region[state_code] = "QLD", dim_region[region_type] = "SERVICE_DISTRICT")',
     f"SELECT r.region_key, active_participants FROM {G}.fact_ndis_plans f JOIN {G}.dim_region r "
     f"ON r.region_key = f.region_key WHERE r.region_type = 'SERVICE_DISTRICT' AND r.state_code = 'QLD' "
     f"AND {ALL3} AND f.date_key = {LATEST}"),
    ("participants by disability group (one member each)",
     'SUMMARIZECOLUMNS(dim_disability_group[disability_group], "v", [Active participants])',
     f"SELECT disability_group, active_participants {NAT} AND 1 = 1".replace(
         "f.is_all_disability_group = 1", "f.is_all_disability_group = 0") + f" AND f.date_key = {LATEST}"),
    ("two disability groups: participants = sum of the two detail cells",
     'CALCULATETABLE(ROW("k", "x", "v", [Active participants]), '
     'dim_disability_group[disability_group] IN {"Autism", "Intellectual Disability"})',
     f"SELECT 'x', SUM(active_participants) {NAT} AND f.date_key = {LATEST} "
     "AND f.disability_group IN ('Autism', 'Intellectual Disability')".replace(
         "f.is_all_disability_group = 1", "f.is_all_disability_group = 0")),
    ("two disability groups: average budget is BLANK",
     'CALCULATETABLE(ROW("k", "x", "v", [Average annualised plan budget]), '
     'dim_disability_group[disability_group] IN {"Autism", "Intellectual Disability"})',
     "SELECT 'x', NULL"),
    ("HCP national, latest period",
     'ROW("k", "x", "v", [HCP people])',
     f"SELECT 'x', SUM(people) {AGED} WHERE a.program = 'HCP' AND a.is_all_care_level = 1 AND r.region_type = 'SA3' "
     f"AND a.date_key = (SELECT MAX(date_key) FROM {G}.fact_aged_care_services WHERE program = 'HCP')"),
    ("CHSP national, latest period",
     'ROW("k", "x", "v", [CHSP people served])',
     f"SELECT 'x', SUM(people) {AGED} WHERE a.program = 'CHSP' AND r.region_type = 'SA3' "
     f"AND a.date_key = (SELECT MAX(date_key) FROM {G}.fact_aged_care_services WHERE program = 'CHSP')"),
    ("HCP by state (default level = SA3)",
     'SUMMARIZECOLUMNS(dim_region[state_code], "v", [HCP people])',
     f"SELECT r.state_code, SUM(people) {AGED} WHERE a.program = 'HCP' AND a.is_all_care_level = 1 "
     f"AND r.region_type = 'SA3' AND a.date_key = 20250630 GROUP BY r.state_code"),
    ("HCP by PHN (one type in context = PHN)",
     'CALCULATETABLE(SUMMARIZECOLUMNS(dim_region[region_key], "v", [HCP people]), dim_region[region_type] = "PHN")',
     f"SELECT r.region_key, people {AGED} WHERE a.program = 'HCP' AND a.is_all_care_level = 1 "
     f"AND r.region_type = 'PHN' AND a.date_key = 20250630"),
    ("HCP by care level",
     'FILTER(SUMMARIZECOLUMNS(fact_aged_care_services[care_level], "v", [HCP people]), '
     'fact_aged_care_services[care_level] <> "Total")',
     f"SELECT a.care_level, SUM(people) {AGED} WHERE a.program = 'HCP' AND a.is_all_care_level = 0 "
     f"AND r.region_type = 'SA3' AND a.date_key = 20250630 GROUP BY a.care_level"),
    ("CHSP with a care level selected is BLANK",
     'CALCULATETABLE(ROW("k", "x", "v", [CHSP people served]), fact_aged_care_services[care_level] = "Level 1")',
     "SELECT 'x', NULL"),
]


def norm(v):
    return None if v is None else round(float(v), 4)


def main() -> None:
    fab = Fabric()
    ws = fab.workspace_id(names()[0])
    model = fab.item(ws, "SemanticModel", MODEL_NAME)["id"]
    cur = connect("admin").cursor()
    failed = 0
    for name, dax, sql in CASES:
        res = pbi("POST", f"/groups/{ws}/datasets/{model}/executeQueries",
                  json={"queries": [{"query": f"EVALUATE {dax}"}], "serializerSettings": {"includeNulls": True}}).json()
        got = {}
        for row in res["results"][0]["tables"][0]["rows"]:
            k, v = list(row.values())
            if v is not None or k == "x":
                got[str(k)] = norm(v)
        # Visuals drop BLANK rows, so suppressed (NULL) cells are compared only in scalar cases.
        want = {str(k): norm(v) for k, v in cur.execute(sql).fetchall() if v is not None or k == "x"}
        ok = got == want and bool(want)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({len(want)} value{'s' if len(want) != 1 else ''})")
        if not ok:
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    print(f"        {k}: model={got.get(k)} sql={want.get(k)}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

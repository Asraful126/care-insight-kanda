"""Test row-level security by running DAX as each test user (executeQueries impersonation).

Expected access comes from config/security_user_state.csv (the same file loaded into the model) and
expected numbers from SQL on kanda_gold, so the test doesn't trust the model's own view of itself.

Per test user:
- dim_region shows exactly the mapped states, never AUS (national) and nothing for an unmapped user;
- [Active participants] / [HCP people] with no filter = the user's states' published cells (STATE level / SA3 sum);
- by state, every visible measure value equals the SQL value for that state;
- security_user_state shows only the user's own rows.
The admin (no impersonation) must still see every state: RLS only binds role members.

Usage: .venv/bin/python scripts/test_rls.py   (after setup_rls.py, security_load.py and deploy_semantic_model.py)
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from assistant.db import connect  # noqa: E402
from deploy_semantic_model import MODEL_NAME, pbi  # noqa: E402
from fabric_client import Fabric, names  # noqa: E402
from setup_rls import TEST_USERS, tenant_domain  # noqa: E402

G = "kanda_gold"
ALL3 = "f.is_all_disability_group = 1 AND f.is_all_age_band = 1 AND f.is_all_support_class = 1"


def mapping() -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    with open(ROOT / "config/security_user_state.csv", newline="") as fh:
        for row in csv.DictReader(fh):
            m[row["user_principal_name"].strip().lower()].add(row["state_code"].strip().upper())
    return m


def main() -> None:
    fab = Fabric()
    ws = fab.workspace_id(names()[0])
    model = fab.item(ws, "SemanticModel", MODEL_NAME)["id"]
    cur = connect("admin").cursor()

    def dax(query: str, user: str | None = None) -> list[dict]:
        body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
        if user:
            body["impersonatedUserName"] = user
        res = pbi("POST", f"/groups/{ws}/datasets/{model}/executeQueries", json=body).json()
        return [{k.split("[")[-1].rstrip("]"): v for k, v in r.items()}
                for r in res["results"][0]["tables"][0]["rows"]]

    latest = cur.execute(f"SELECT MAX(date_key) FROM {G}.fact_ndis_plans").fetchone()[0]
    latest_hcp = cur.execute(f"SELECT MAX(date_key) FROM {G}.fact_aged_care_services WHERE program = 'HCP'").fetchone()[0]
    participants = dict(cur.execute(
        f"SELECT r.state_code, f.active_participants FROM {G}.fact_ndis_plans f JOIN {G}.dim_region r "
        f"ON r.region_key = f.region_key WHERE r.region_type = 'STATE' AND {ALL3} AND f.date_key = {latest}").fetchall())
    hcp = dict(cur.execute(
        f"SELECT r.state_code, SUM(a.people) FROM {G}.fact_aged_care_services a JOIN {G}.dim_region r "
        f"ON r.region_key = a.region_key WHERE a.program = 'HCP' AND a.is_all_care_level = 1 AND r.region_type = 'SA3' "
        f"AND a.date_key = {latest_hcp} GROUP BY r.state_code").fetchall())

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    # VALUES() also returns Direct Lake's virtual blank member (no fact rows reference it), so drop None.
    admin_states = {r["state_code"] for r in dax("EVALUATE VALUES(dim_region[state_code])")} - {None}
    check("admin (no impersonation) sees every state incl. AUS", "AUS" in admin_states and len(admin_states) > 9,
          f"{sorted(admin_states)}")

    m = mapping()
    domain = tenant_domain()
    for alias in TEST_USERS:
        upn = f"{alias}@{domain}"
        want = m.get(upn, set())
        who = f"{alias}@<tenant>"

        states = {r["state_code"] for r in dax("EVALUATE VALUES(dim_region[state_code])", upn)} - {None}
        check(f"{who}: sees exactly states {sorted(want) or 'none'}", states == want, f"got {sorted(states)}")
        check(f"{who}: no national row (AUS)", "AUS" not in states)

        card = dax('EVALUATE ROW("p", [Active participants], "h", [HCP people], "m", COUNTROWS(security_user_state))', upn)[0]
        want_p = sum(participants[s] for s in want) or None
        want_h = sum(hcp[s] for s in want) or None
        check(f"{who}: unfiltered Active participants = own states", card["p"] == want_p, f"model={card['p']} sql={want_p}")
        check(f"{who}: unfiltered HCP people = own states (SA3)", card["h"] == want_h, f"model={card['h']} sql={want_h}")
        check(f"{who}: sees only own mapping rows", (card["m"] or 0) == len(want), f"rows={card['m']}")

        by_state = dax('EVALUATE SUMMARIZECOLUMNS(dim_region[state_code], "p", [Active participants], "h", [HCP people])', upn)
        got = {r["state_code"]: (r["p"], r["h"]) for r in by_state}
        exp = {s: (participants[s], hcp[s]) for s in want}
        check(f"{who}: by-state values = SQL", got == exp, f"model={got} sql={exp}")

    failed = 0
    for name, ok, detail in results:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        {detail}"))
    print(f"\n{len(results) - failed}/{len(results)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

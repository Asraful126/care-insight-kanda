# Fabric notebook source (PySpark). Cells are split on "# %%" by scripts/fabric_notebook.py.
# Default lakehouse: lh_care_insights (set at deploy time).

# %% [markdown]
# # Gold tests
# Run after `gold_build`. Each test is recomputed from the tables themselves (not from the build's
# own log), so a bug in the build cannot hide itself. Results are appended to
# `kanda_gold.gold_test_results`; the notebook fails if any test fails.
#
# 1. **Keys**: grain columns unique and not null in every gold table.
# 2. **Referential integrity**: every fact key exists in its dimension.
# 3. **Row counts bronze → silver → gold**, with every difference explained by a documented rule.
# 4. **Anchors**: published headline figures that must survive the pipeline unchanged.

# %%
import uuid
from datetime import datetime, timezone

from pyspark.sql import functions as F

BRONZE, SILVER, GOLD = "kanda_bronze", "kanda_silver", "kanda_gold"
RUN_ID = str(uuid.uuid4())
RUN_AT = datetime.now(timezone.utc).replace(tzinfo=None)
results = []


def check(name: str, table: str, passed: bool, detail: str) -> None:
    results.append((RUN_ID, RUN_AT, name, table, bool(passed), detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name:<28} {table:<34} {detail}")


def g(t: str):
    return spark.table(f"{GOLD}.{t}")


def n(db: str, t: str, where: str | None = None) -> int:
    df = spark.table(f"{db}.{t}")
    return (df.where(where) if where else df).count()

# %% [markdown]
# ## 1. Keys

# %%
COHORT = ["disability_group", "age_band", "support_class"]
GRAIN = {
    "dim_date": ["date_key"],
    "dim_region": ["region_key"],
    "dim_disability_group": ["disability_group"],
    "dim_age_band": ["age_band"],
    "dim_support_class": ["support_class"],
    "fact_ndis_plans": ["date_key", "region_key", *COHORT],
    "fact_ndis_utilisation": ["date_key", "region_key", *COHORT, "sil_or_sda"],
    "fact_ndis_providers": ["date_key", "region_key", *COHORT],
    "fact_ndis_participants_sa3": ["date_key", "region_key", "participant_state_code"],
    "fact_aged_care_services": ["date_key", "program", "region_key", "care_level"],
}
for table, grain in GRAIN.items():
    df = g(table)
    nulls = df.where(" OR ".join(f"`{c}` IS NULL" for c in grain)).count()
    dups = df.groupBy(*grain).count().where("count > 1").count()
    check("not_null_keys", table, nulls == 0, f"{nulls} rows with a NULL key")
    check("unique_grain", table, dups == 0, f"{dups} duplicate keys on ({', '.join(grain)})")

# %% [markdown]
# ## 2. Referential integrity
# Orphan fact rows would silently vanish from Power BI visuals that use the dimension.

# %%
FKS = [(f, "date_key", "dim_date") for f in GRAIN if f.startswith("fact_")]
FKS += [(f, "region_key", "dim_region") for f in GRAIN if f.startswith("fact_")]
FKS += [(f, c, f"dim_{c}") for f in ("fact_ndis_plans", "fact_ndis_utilisation", "fact_ndis_providers") for c in COHORT]
for fact, col, dim in FKS:
    orphans = g(fact).select(col).distinct().join(g(dim).select(col), col, "left_anti")
    k = orphans.count()
    sample = [r[0] for r in orphans.limit(3).collect()]
    check("fk_exists", f"{fact}.{col}", k == 0, f"{k} values not in {dim} {sample if k else ''}")

# The is_all_* flags drive the "published cell" measures; a NULL flag makes a row invisible to them.
for table in ("fact_ndis_plans", "fact_ndis_utilisation", "fact_ndis_providers", "fact_aged_care_services"):
    flags = [c for c in g(table).columns if c.startswith("is_all_")]
    k = g(table).where(" OR ".join(f"{c} IS NULL" for c in flags)).count()
    check("not_null_all_flags", table, k == 0, f"{k} rows with a NULL is_all_* flag")

r = g("dim_region")
check("region_has_state", "dim_region", r.where("state_code IS NULL").count() == 0, "state_code (RLS key) on every row")
parents = r.where("parent_region_key IS NOT NULL").select(F.col("parent_region_key").alias("region_key")).distinct()
k = parents.join(r.select("region_key"), "region_key", "left_anti").count()
check("region_parent_exists", "dim_region", k == 0, f"{k} parent keys missing")

# %% [markdown]
# ## 3. Row counts bronze → silver → gold
# - NDIS: silver = bronze (no rows dropped). Gold = silver − the `state = ALL` district rows, each of
#   which either merged into its state-coded twin or was a cross-state `Other` row.
# - SA3: gold = silver = bronze.
# - GEN: silver = bronze rows × measure columns (HCP levels 1–4 + total; CHSP total). Gold = silver − MMM.

# %%
NDIS = {
    "fact_ndis_plans": ("bronze_ndis_plan_budgets", "silver_ndis_plan_budgets"),
    "fact_ndis_utilisation": ("bronze_ndis_utilisation", "silver_ndis_utilisation"),
    "fact_ndis_providers": ("bronze_ndis_active_providers", "silver_ndis_active_providers"),
}
for fact, (b, s) in NDIS.items():
    nb, ns, ng = n(BRONZE, b), n(SILVER, s), n(GOLD, fact)
    dup = n(SILVER, s, "is_all_state AND NOT is_all_service_district")
    check("rows_bronze_silver", s, nb == ns, f"bronze {nb} = silver {ns}")
    check("rows_silver_gold", fact, ng == ns - dup, f"gold {ng} = silver {ns} − state=ALL district rows {dup}")

nb, ns, ng = n(BRONZE, "bronze_ndis_participants_by_sa3"), n(SILVER, "silver_ndis_participants_by_sa3"), n(GOLD, "fact_ndis_participants_sa3")
check("rows_bronze_silver_gold", "fact_ndis_participants_sa3", nb == ns == ng, f"bronze {nb} = silver {ns} = gold {ng}")

REGION_TYPES = ["lga", "sa3", "acpr", "phn", "mmm"]
gen_silver = 0
for prog, silver_t in (("hcp", "silver_gen_home_care"), ("chsp", "silver_gen_home_support")):
    tables = [spark.table(f"{BRONZE}.bronze_gen_{prog}_{rt}") for rt in REGION_TYPES]
    measures = len([c for c in tables[0].columns if c.startswith("level_") or c == "total"])
    nb = sum(t.count() for t in tables)
    ns = n(SILVER, silver_t)
    gen_silver += ns
    check("rows_bronze_silver", silver_t, ns == nb * measures, f"silver {ns} = bronze {nb} × {measures} measures")
mmm = n(SILVER, "silver_gen_home_care", "region_type = 'MMM'") + n(SILVER, "silver_gen_home_support", "region_type = 'MMM'")
ng = n(GOLD, "fact_aged_care_services")
check("rows_silver_gold", "fact_aged_care_services", ng == gen_silver - mmm, f"gold {ng} = silver {gen_silver} − MMM {mmm}")

# %% [markdown]
# ## 4. Anchors and invariants
# Headline numbers read straight from the source CSVs (see docs/data-sources.md).

# %%
def national(fact: str, measure: str, report_date: str) -> int | None:
    row = (
        g(fact).where(f"region_key = 'NATIONAL:AUS' AND date_key = {report_date.replace('-', '')}")
        .where(" AND ".join(f"is_all_{c}" for c in COHORT))
        .select(measure).collect()
    )
    return row[0][0] if len(row) == 1 else None


v = national("fact_ndis_plans", "active_participants", "2026-09-30")
check("anchor_national_participants", "fact_ndis_plans", v == 792304, f"30 Sep 2026: {v} (published 792,304)")
v = national("fact_ndis_providers", "provider_count", "2026-09-30")
check("anchor_national_providers", "fact_ndis_providers", v == 11083, f"30 Sep 2026: {v} (published 11,083)")

# The merge may only remove suppression, never add it: a gold district cell is suppressed only if the
# state-coded silver cell was too.
silver_supp = n(SILVER, "silver_ndis_plan_budgets", "NOT is_all_state AND NOT is_all_service_district AND active_participants_is_suppressed")
gold_supp = n(GOLD, "fact_ndis_plans", "region_key LIKE 'SERVICE_DISTRICT:%' AND active_participants_is_suppressed")
check("merge_reduces_suppression", "fact_ndis_plans", gold_supp <= silver_supp, f"suppressed district cells: silver {silver_supp} → gold {gold_supp}")

bad = g("fact_aged_care_services").where(
    "(program = 'HCP' AND count_type <> 'stock') OR (program = 'CHSP' AND count_type <> 'flow')"
).count()
check("aged_care_count_type", "fact_aged_care_services", bad == 0, f"{bad} rows with the wrong stock/flow type")

u = g("fact_ndis_utilisation").where("utilisation < 0 OR utilisation > 1").count()
check("utilisation_range", "fact_ndis_utilisation", u == 0, f"{u} values outside 0–1")

# %% [markdown]
# ## Results

# %%
res = spark.createDataFrame(
    results, "run_id STRING, run_at TIMESTAMP, test_name STRING, target STRING, passed BOOLEAN, detail STRING"
)
res.write.mode("append").format("delta").saveAsTable(f"{GOLD}.gold_test_results")
failed = [r for r in results if not r[4]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
assert not failed, f"{len(failed)} gold tests failed: {[(r[2], r[3]) for r in failed]}"

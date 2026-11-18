# Fabric notebook source (PySpark). Cells are split on "# %%" by scripts/fabric_notebook.py.
# Default lakehouse: lh_care_insights (set at deploy time).

# %% [markdown]
# # Gold build
# `kanda_silver` → `kanda_gold`: a small star schema for Power BI (Direct Lake) and for the
# assistant's read-only SQL tool.
#
# | Table | Grain (one row = …) |
# |---|---|
# | `dim_date` | calendar day, 1 Jul 2019 – 30 Jun 2027 |
# | `dim_region` | region type × region (NATIONAL, STATE, SERVICE_DISTRICT, SA3, LGA, ACPR, PHN) |
# | `dim_disability_group`, `dim_age_band`, `dim_support_class` | NDIS cohort member, incl. `ALL` |
# | `fact_ndis_plans` | report date × region × disability group × age band × support class |
# | `fact_ndis_utilisation` | … same × SIL/SDA flag |
# | `fact_ndis_providers` | report date × region × disability group × age band × support class |
# | `fact_ndis_participants_sa3` | report date × SA3 × participant state |
# | `fact_aged_care_services` | period end × program (HCP/CHSP) × region × care level |
#
# Decisions (reasoning in docs/data-dictionary.md):
# 1. **Published cells, not sums.** NDIS facts keep the publisher's `ALL` subtotals. DAX picks the one
#    published cell for the current slice (no undercount from suppression, no re-weighting of averages
#    or utilisation). The region level is carried by `region_key` (NATIONAL / STATE / SERVICE_DISTRICT).
# 2. **One `dim_region`**, one row per region type × code, `state_code` on every row (the RLS key).
#    Keys are readable strings; the boundary edition is part of the key (`LGA_2016` ≠ `LGA_2017`).
#    MMM (remoteness class, no state) is not a region and is left out of gold.
# 3. **District cells are published twice** (under their state and under `state = ALL`) with different
#    secondary suppression. Gold merges them into one row: an exact value wins over a `<N` bound,
#    otherwise the tighter bound. The two must never conflict (asserted). The cross-state `Other`
#    district under `state = ALL` has no state and is dropped; national rows still include it.
# 4. **One aged care fact** with `program` and `count_type`: HCP is a stock (30 June), CHSP a flow
#    (financial year). Measures are always split by program; there is no "total aged care" measure.

# %%
import uuid
from datetime import date, datetime, timezone

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

SILVER, GOLD = "kanda_silver", "kanda_gold"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")

RUN_ID = str(uuid.uuid4())
LOADED_AT = datetime.now(timezone.utc).replace(tzinfo=None)

DATE_FROM, DATE_TO = date(2019, 7, 1), date(2027, 6, 30)
STATE_NAMES = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland", "SA": "South Australia",
    "WA": "Western Australia", "TAS": "Tasmania", "NT": "Northern Territory",
    "ACT": "Australian Capital Territory", "OT": "Other Territories", "UNK": "Unknown",
}
STATE_BY_DIGIT = {"1": "NSW", "2": "VIC", "3": "QLD", "4": "SA", "5": "WA", "6": "TAS", "7": "NT", "8": "ACT", "9": "OT"}
REGION_TYPE_ORDER = {"NATIONAL": 1, "STATE": 2, "SERVICE_DISTRICT": 3, "PHN": 4, "ACPR": 5, "SA3": 6, "LGA": 7}
NATIONAL_KEY = "NATIONAL:AUS"

results = []  # gold_load_log rows
stats = []    # merge/drop counts, also logged


def date_key(c: Column) -> Column:
    return F.date_format(c, "yyyyMMdd").cast("int")


def write_gold(df: DataFrame, table: str, grain: list[str], expected_rows: int, silver_rows: int | None = None) -> None:
    df = df.withColumn("_gold_loaded_at", F.lit(LOADED_AT).cast("timestamp")).withColumn("_gold_run_id", F.lit(RUN_ID))
    table = f"{GOLD}.{table}"
    df.write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable(table)
    t = spark.table(table)
    rows = t.count()
    nulls = t.where(" OR ".join(f"`{c}` IS NULL" for c in grain)).count()
    dups = t.groupBy(*grain).count().where("count > 1").count()
    results.append((RUN_ID, LOADED_AT, table, ",".join(grain), silver_rows, expected_rows, rows, nulls, dups))
    print(f"{table:<44} {rows:>8} rows  null-keys={nulls}  dup-grain={dups}")
    assert rows == expected_rows, f"{table}: expected {expected_rows} rows, got {rows}"
    assert nulls == 0, f"{table}: {nulls} rows with a NULL grain column"
    assert dups == 0, f"{table}: {dups} duplicate grain keys"

# %% [markdown]
# ## dim_date
# Daily calendar. Facts join on the **period end** date: NDIS report date (quarter end), HCP
# snapshot (30 June), CHSP financial year end (30 June). Financial year runs July–June.

# %%
fy_end = F.when(F.month("date") >= 7, F.year("date") + 1).otherwise(F.year("date"))
dim_date = spark.sql(
    f"SELECT explode(sequence(DATE'{DATE_FROM}', DATE'{DATE_TO}', INTERVAL 1 DAY)) AS date"
).select(
    date_key(F.col("date")).alias("date_key"),
    "date",
    F.year("date").alias("year"),
    F.quarter("date").alias("quarter"),
    F.format_string("%d Q%d", F.year("date"), F.quarter("date")).alias("quarter_label"),
    F.month("date").alias("month"),
    F.date_format("date", "MMM").alias("month_name"),
    F.date_format("date", "yyyy-MM").alias("year_month"),
    fy_end.alias("fy_end_year"),
    F.format_string("%d-%02d", fy_end - 1, fy_end % 100).alias("financial_year"),
    ((F.last_day("date") == F.col("date")) & (F.month("date") % 3 == 0)).alias("is_quarter_end"),
    ((F.month("date") == 6) & (F.dayofmonth("date") == 30)).alias("is_fy_end"),
)
write_gold(dim_date, "dim_date", ["date_key"], (DATE_TO - DATE_FROM).days + 1)

# %% [markdown]
# ## NDIS facts: region levels and the district merge
# Silver flags `is_all_state` / `is_all_service_district`. They become one `region_key`:
#
# | state | district | gold region |
# |---|---|---|
# | ALL | ALL | `NATIONAL:AUS` |
# | NSW | ALL | `STATE:NSW` |
# | NSW | Hunter New England | `SERVICE_DISTRICT:NSW:Hunter New England` (merged with the row below) |
# | ALL | Hunter New England | same cell, other suppression; state looked up from the district name |
# | ALL | Other | dropped: `Other` exists in every state, so it cannot be placed |

# %%
COHORT = ["disability_group", "age_band", "support_class"]
COHORT_FLAGS = [f"is_all_{c}" for c in COHORT]


def ndis_regions(table: str, extra_keys: list[str], counts: list[str], exacts: dict[str, str | None]) -> tuple[DataFrame, dict]:
    """Silver NDIS table → one row per report date × region_key × cohort (+ extra keys).

    counts: suppression-aware counts (`c`, `c_is_suppressed`, `c_upper_bound`).
    exacts: measures that must agree exactly between the two published versions → their
            suppression flag column (or None).
    """
    df = spark.table(f"{SILVER}.{table}")
    keys = ["report_date", *COHORT, *COHORT_FLAGS, *extra_keys]
    measures = [x for c in counts for x in (c, f"{c}_is_suppressed", f"{c}_upper_bound")]
    measures += [x for c, s in exacts.items() for x in ([c, s] if s else [c])]
    out_cols = [*keys, "region_key", "region_type", "state_code", "service_district", *measures]

    nat = df.where("is_all_state AND is_all_service_district").select(
        "*", F.lit(NATIONAL_KEY).alias("region_key"), F.lit("NATIONAL").alias("region_type"),
    ).withColumn("state_code", F.lit("AUS")).withColumn("service_district", F.lit(None).cast("string"))
    st = df.where("NOT is_all_state AND is_all_service_district").select(
        "*", F.concat(F.lit("STATE:"), "state_code").alias("region_key"), F.lit("STATE").alias("region_type"),
    ).withColumn("service_district", F.lit(None).cast("string"))

    s = df.where("NOT is_all_state AND NOT is_all_service_district")
    a = df.where("is_all_state AND NOT is_all_service_district")
    a_other = a.where(F.col("service_district") == "Other")
    a = a.where(F.col("service_district") != "Other")

    # District → state, from the state-coded rows. Must be one state per district name.
    lookup = s.where(F.col("service_district") != "Other").select("service_district", "state_code").distinct()
    ambiguous = lookup.groupBy("service_district").count().where("count > 1").count()
    assert ambiguous == 0, f"{table}: {ambiguous} district names map to more than one state"
    a = a.drop("state_code").join(lookup, "service_district", "left")
    unplaced = a.where("state_code IS NULL").count()
    assert unplaced == 0, f"{table}: {unplaced} state=ALL district rows have no state-coded district"

    jk = [*keys, "state_code", "service_district"]
    sv = s.select(*jk, *[F.col(m).alias(f"s_{m}") for m in measures])
    av = a.select(*jk, *[F.col(m).alias(f"a_{m}") for m in measures])
    j = sv.join(av, jk, "full_outer")

    merged, conflict = [], F.lit(False)
    for c in counts:
        sc, ac = F.col(f"s_{c}"), F.col(f"a_{c}")
        sub, aub = F.col(f"s_{c}_upper_bound"), F.col(f"a_{c}_upper_bound")
        value = F.coalesce(sc, ac)
        merged += [
            value.alias(c),
            (F.coalesce(F.col(f"s_{c}_is_suppressed"), F.lit(True)) & F.coalesce(F.col(f"a_{c}_is_suppressed"), F.lit(True))).alias(f"{c}_is_suppressed"),
            F.when(value.isNull(), F.least(sub, aub)).alias(f"{c}_upper_bound"),
        ]
        conflict = conflict | F.coalesce(
            (sc != ac) | (sc > aub) | (ac > sub), F.lit(False)
        )  # NULL comparisons (one side suppressed or absent) are not conflicts
    for c, supp in exacts.items():
        sc, ac = F.col(f"s_{c}"), F.col(f"a_{c}")
        merged.append(F.coalesce(sc, ac).alias(c))
        if supp:
            merged.append((F.coalesce(F.col(f"s_{supp}"), F.lit(True)) & F.coalesce(F.col(f"a_{supp}"), F.lit(True))).alias(supp))
        conflict = conflict | F.coalesce(sc != ac, F.lit(False))

    j = j.withColumn("_conflict", conflict)
    conflicts = j.where("_conflict").count()
    assert conflicts == 0, f"{table}: {conflicts} district cells disagree between the two published versions"

    first = counts[0] if counts else None
    improved = (
        j.where(F.col(f"s_{first}_is_suppressed") & ~F.col(f"a_{first}_is_suppressed")).count() if first else 0
    )
    dist = j.select(
        *jk, *merged,
        F.concat_ws(":", F.lit("SERVICE_DISTRICT"), "state_code", "service_district").alias("region_key"),
        F.lit("SERVICE_DISTRICT").alias("region_type"),
    )

    out = nat.select(*out_cols).unionByName(st.select(*out_cols)).unionByName(dist.select(*out_cols))
    info = {
        "table": table,
        "silver_rows": df.count(),
        "state_all_district_rows": a.count() + a_other.count(),
        "dropped_other_rows": a_other.count(),
        "merged_pairs": a.count(),
        "cells_unsuppressed_by_merge": improved,
    }
    stats.append(info)
    print(info)
    return out, info


def ndis_fact(df: DataFrame) -> DataFrame:
    # region attributes live in dim_region; the fact keeps only region_key
    return df.select(date_key(F.col("report_date")).alias("date_key"), "*").drop("region_type", "state_code", "service_district")


def expected_ndis(info: dict) -> int:
    # every state=ALL district row either merges into its state-coded twin or is a dropped `Other`
    return info["silver_rows"] - info["state_all_district_rows"]


NDIS_GRAIN = ["date_key", "region_key", *COHORT]

plans, plans_info = ndis_regions(
    "silver_ndis_plan_budgets", [], ["active_participants"],
    {"avg_annualised_budget": "avg_annualised_budget_is_suppressed"},
)
write_gold(ndis_fact(plans), "fact_ndis_plans", NDIS_GRAIN, expected_ndis(plans_info), plans_info["silver_rows"])

util, util_info = ndis_regions("silver_ndis_utilisation", ["sil_or_sda", "is_all_sil_or_sda"], [], {"utilisation": None})
write_gold(ndis_fact(util), "fact_ndis_utilisation", NDIS_GRAIN + ["sil_or_sda"], expected_ndis(util_info), util_info["silver_rows"])

prov, prov_info = ndis_regions("silver_ndis_active_providers", [], ["provider_count"], {})
write_gold(ndis_fact(prov), "fact_ndis_providers", NDIS_GRAIN, expected_ndis(prov_info), prov_info["silver_rows"])

# %% [markdown]
# ## NDIS participants by SA3
# Silver grain is report date × participant state × SA3. A few SA3s appear under two states (90 rows),
# so `participant_state_code` stays in the grain. RLS filters by the **SA3's** state (from `dim_region`).
# Unknown SA3 becomes one region per state: `SA3_2016:UNK:<state>`.

# %%
s3 = spark.table(f"{SILVER}.silver_ndis_participants_by_sa3")
sa3_key = F.when(
    F.col("sa3_2016_code") == "UNK", F.concat(F.lit("SA3_2016:UNK:"), "state_code")
).otherwise(F.concat(F.lit("SA3_2016:"), "sa3_2016_code"))
sa3_fact = s3.select(
    date_key(F.col("report_date")).alias("date_key"),
    "report_date",
    sa3_key.alias("region_key"),
    F.col("state_code").alias("participant_state_code"),
    "participants", "participants_is_suppressed", "participants_upper_bound",
)
write_gold(
    sa3_fact, "fact_ndis_participants_sa3", ["date_key", "region_key", "participant_state_code"], s3.count(), s3.count()
)

# %% [markdown]
# ## Aged care services (GEN): HCP + CHSP in one fact
# HCP: stock at the 30 June snapshot, by care level (`Total` = all levels, `is_all_care_level`).
# CHSP: people served in the financial year; no levels, so `care_level = 'Not applicable'` and
# `is_all_care_level = true` (it is the program total). MMM rows are left out (decision 2).

# %%
def gen_region_key() -> Column:
    return F.concat(F.col("region_type"), F.lit("_"), F.col("region_edition"), F.lit(":"), F.col("region_code"))


hc = spark.table(f"{SILVER}.silver_gen_home_care")
hs = spark.table(f"{SILVER}.silver_gen_home_support")
AGED_COLS = [
    "date_key", "period_end_date", "period_label", "program", "program_name", "count_type",
    "region_key", "care_level", "is_all_care_level", "people", "people_is_suppressed", "people_upper_bound",
]
hcp = hc.select(
    date_key(F.col("snapshot_date")).alias("date_key"),
    F.col("snapshot_date").alias("period_end_date"),
    F.date_format("snapshot_date", "d MMM yyyy").alias("period_label"),
    F.lit("HCP").alias("program"),
    F.lit("Home Care Packages").alias("program_name"),
    F.lit("stock").alias("count_type"),
    gen_region_key().alias("region_key"),
    "care_level", "is_all_care_level", "people", "people_is_suppressed", "people_upper_bound", "region_type",
)
chsp = hs.select(
    date_key(F.col("period_end_date")).alias("date_key"),
    "period_end_date",
    F.concat(F.lit("FY "), "financial_year").alias("period_label"),
    F.lit("CHSP").alias("program"),
    F.lit("Commonwealth Home Support Programme").alias("program_name"),
    F.lit("flow").alias("count_type"),
    gen_region_key().alias("region_key"),
    F.lit("Not applicable").alias("care_level"),
    F.lit(True).alias("is_all_care_level"),
    "people", "people_is_suppressed", "people_upper_bound", "region_type",
)
aged_all = hcp.unionByName(chsp)
mmm_rows = aged_all.where(F.col("region_type") == "MMM").count()
aged = aged_all.where(F.col("region_type") != "MMM").select(*AGED_COLS)
stats.append({"table": "silver_gen_home_care+home_support", "silver_rows": hc.count() + hs.count(), "dropped_mmm_rows": mmm_rows})
write_gold(
    aged, "fact_aged_care_services", ["date_key", "program", "region_key", "care_level"],
    hc.count() + hs.count() - mmm_rows, hc.count() + hs.count(),
)

# %% [markdown]
# ## dim_region
# Built from every region the facts reference, so referential integrity holds by construction
# (and is still tested in `gold_tests`). Names: where two sources name the same SA3, the
# alphabetically first name is kept and the number of variants is printed.

# %%
ndis_rows = (
    plans.select("region_key", "region_type", "state_code", "service_district")
    .unionByName(util.select("region_key", "region_type", "state_code", "service_district"))
    .unionByName(prov.select("region_key", "region_type", "state_code", "service_district"))
    .where("region_type = 'SERVICE_DISTRICT'")
    .distinct()
    .select(
        "region_key", "region_type",
        F.lit(None).cast("string").alias("region_edition"),
        F.concat_ws(":", "state_code", "service_district").alias("region_code"),
        F.col("service_district").alias("region_name"),
        "state_code",
    )
)
state_rows = spark.createDataFrame(
    [(f"STATE:{k}", "STATE", None, k, v, k) for k, v in STATE_NAMES.items()]
    + [(NATIONAL_KEY, "NATIONAL", None, "AUS", "Australia", "AUS")],
    "region_key STRING, region_type STRING, region_edition STRING, region_code STRING, region_name STRING, state_code STRING",
)
digit_state = F.create_map(*[F.lit(x) for kv in STATE_BY_DIGIT.items() for x in kv])
sa3_rows = s3.select(
    sa3_key.alias("region_key"),
    F.lit("SA3").alias("region_type"),
    F.lit("2016").alias("region_edition"),
    "sa3_2016_code",
    F.col("sa3_2016_name").alias("region_name"),
    F.when(F.col("sa3_2016_code") == "UNK", F.col("state_code"))
    .otherwise(digit_state[F.substring("sa3_2016_code", 1, 1)]).alias("state_code"),
).withColumn(
    "region_code", F.when(F.col("sa3_2016_code") == "UNK", F.concat(F.lit("UNK:"), "state_code")).otherwise(F.col("sa3_2016_code"))
).drop("sa3_2016_code")
gen_rows = hc.unionByName(hs, allowMissingColumns=True).where(F.col("region_type") != "MMM").select(
    gen_region_key().alias("region_key"), "region_type", "region_edition", "region_code", "region_name", "state_code",
)
REGION_COLS = ["region_key", "region_type", "region_edition", "region_code", "region_name", "state_code"]
regions = state_rows.unionByName(ndis_rows.select(REGION_COLS)).unionByName(sa3_rows.select(REGION_COLS)).unionByName(gen_rows)

name_variants = regions.groupBy("region_key").agg(F.countDistinct("region_name").alias("n")).where("n > 1").count()
state_variants = regions.groupBy("region_key").agg(F.countDistinct("state_code").alias("n")).where("n > 1").count()
print(f"dim_region: {name_variants} keys with more than one source name (first kept)")
assert state_variants == 0, f"dim_region: {state_variants} keys with conflicting state_code"

state_name = F.create_map(*[F.lit(x) for kv in {**STATE_NAMES, "AUS": "Australia"}.items() for x in kv])
type_order = F.create_map(*[F.lit(x) for kv in REGION_TYPE_ORDER.items() for x in kv])
dim_region = (
    regions.groupBy("region_key")
    .agg(
        F.first("region_type").alias("region_type"),
        F.first("region_edition", ignorenulls=True).alias("region_edition"),
        F.first("region_code").alias("region_code"),
        F.min("region_name").alias("region_name"),
        F.first("state_code").alias("state_code"),
    )
    .withColumn("state_name", state_name[F.col("state_code")])
    .withColumn("region_type_order", type_order[F.col("region_type")])
    .withColumn("parent_region_key", F.when(F.col("region_type") == "STATE", F.lit(NATIONAL_KEY))
                .when(F.col("region_type") != "NATIONAL", F.concat(F.lit("STATE:"), "state_code")))
)
expected_regions = regions.select("region_key").distinct().count()
write_gold(dim_region, "dim_region", ["region_key"], expected_regions)

# %% [markdown]
# ## NDIS cohort dimensions
# Shared by plans, utilisation and providers so one slicer filters all three. Each keeps its `ALL`
# member (`is_all = true`): the "published cell" measures select it when the dimension is not filtered.
# Report slicers hide it.

# %%
def cohort_dim(col: str) -> tuple[DataFrame, int]:
    vals = (
        plans.select(col).unionByName(util.select(col)).unionByName(prov.select(col)).distinct()
        .withColumn("is_all", F.col(col) == "ALL")
        .withColumn("is_unknown", F.col(col).isin("Unknown", "Not stated"))
    )
    if col == "age_band":  # numeric order: 0 to 8, 9 to 14, …, 65+
        sort = F.coalesce(F.regexp_extract(col, r"^(\d+)", 1).cast("int"), F.lit(0))
    else:
        sort = F.lit(0)
    rows = vals.withColumn("_s", sort).orderBy("is_all", "is_unknown", "_s", col).collect()
    out = spark.createDataFrame(
        [(r[col], r["is_all"], i + 1) for i, r in enumerate(rows)], f"{col} STRING, is_all BOOLEAN, sort_order INT"
    )
    return out, len(rows)


for col in COHORT:
    d, n = cohort_dim(col)
    write_gold(d, f"dim_{col}", [col], n)

# %% [markdown]
# ## Load log

# %%
log = spark.createDataFrame(
    results,
    "run_id STRING, loaded_at TIMESTAMP, table_name STRING, grain STRING, silver_rows LONG, expected_rows LONG, "
    "actual_rows LONG, null_key_rows LONG, duplicate_keys LONG",
)
log.write.mode("append").format("delta").saveAsTable(f"{GOLD}.gold_load_log")
log.select("table_name", "silver_rows", "expected_rows", "actual_rows", "null_key_rows", "duplicate_keys").show(50, truncate=False)
for s in stats:
    print(s)

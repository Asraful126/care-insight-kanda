# Fabric notebook source (PySpark). Cells are split on "# %%" by scripts/fabric_notebook.py.
# Default lakehouse: lh_care_insights (set at deploy time).

# %% [markdown]
# # Silver transform
# `kanda_bronze` (all strings, as received) → `kanda_silver`: typed, standardised tables,
# **one row per grain**.
#
# Decisions (see docs/data-dictionary.md for the reasoning):
# 1. **Suppression**: `<N` → value `NULL`, `is_suppressed = true`, `upper_bound = N - 1`; raw text kept.
#    No imputation: bounds run from `<11` up to `<45030` (secondary suppression).
# 2. **`ALL` subtotals are kept** and flagged per dimension (`is_all_<dim>`). They are the only
#    correct totals, because summing suppressed detail undercounts (by ~10% nationally).
# 3. **Utilisation** is stored as a fraction at its published grain; it is never re-aggregated here.
# 4. **Regions**: each source keeps its own geography; `state_code` is derived everywhere so state
#    is the shared level (and the RLS key). No allocation across boundaries.
#
# Standardisation: unknown markers → `UNK` (state) / `Unknown` (other dims), blank → `Not stated`; support class labels
# unified (`CapacityBuilding` → `Capacity Building`); dates parsed from `30SEP2026` / GEN titles.
# Rows are never dropped: silver row count = bronze row count (GEN: × unpivoted measure columns).

# %%
import uuid
from datetime import datetime, timezone

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

BRONZE, SILVER = "kanda_bronze", "kanda_silver"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SILVER}")

RUN_ID = str(uuid.uuid4())
LOADED_AT = datetime.now(timezone.utc).replace(tzinfo=None)

STATE_UNKNOWN = ["State_Missing", "MIS"]
# Any "<Dim>_Missing" marker (DsbltyGrp_, Disability_, Age_, SuppClass_, Region_Missing) → Unknown.
# A blank disability group is a *different* published population (64 utilisation rows, all 0%),
# so it gets its own member rather than being merged into Unknown.
DIM_NOT_STATED = "Not stated"
# First digit of ABS-based region codes (SA3, LGA, ACPR, PHN) → state
STATE_BY_DIGIT = {"1": "NSW", "2": "VIC", "3": "QLD", "4": "SA", "5": "WA", "6": "TAS", "7": "NT", "8": "ACT", "9": "OT"}

results = []

# %% [markdown]
# ## Helpers

# %%
def state(c: Column) -> Column:
    c = F.trim(c)
    return F.when(c.isin(STATE_UNKNOWN), F.lit("UNK")).otherwise(c)


def dim(c: Column) -> Column:
    c = F.trim(F.coalesce(c, F.lit("")))
    return (
        F.when(c.endswith("_Missing"), F.lit("Unknown"))
        .when(c == "", F.lit(DIM_NOT_STATED))
        .otherwise(c)
    )


def support_class(c: Column) -> Column:
    return F.when(F.trim(c) == "CapacityBuilding", F.lit("Capacity Building")).otherwise(dim(c))


def report_date(c: Column) -> Column:
    return F.to_date(c, "ddMMMyyyy")  # 30SEP2026


def count_cols(raw: Column, name: str) -> list[Column]:
    """Suppression-aware count: value, is_suppressed, upper_bound, raw text."""
    raw = F.trim(raw)
    suppressed = raw.startswith("<") | raw.isin("n.p.", "np")
    return [
        F.when(raw.rlike(r"^[\d,]+$"), F.regexp_replace(raw, ",", "").cast("int")).alias(name),
        suppressed.alias(f"{name}_is_suppressed"),
        F.when(raw.startswith("<"), F.substring(raw, 2, 10).cast("int") - 1).alias(f"{name}_upper_bound"),
        raw.alias(f"{name}_raw"),
    ]


def all_flags(cols: dict[str, str]) -> list[Column]:
    """is_all_<dim> for each bronze column that carries an ALL member."""
    return [(F.trim(F.col(src)) == "ALL").alias(f"is_all_{name}") for name, src in cols.items()]


LINEAGE = [
    F.col("_source_file"),
    F.col("_source_sha256"),
    F.col("_run_id").alias("_bronze_run_id"),
]


def write_silver(df: DataFrame, table: str, grain: list[str], expected_rows: int) -> None:
    df = df.withColumn("_silver_loaded_at", F.lit(LOADED_AT).cast("timestamp")).withColumn("_silver_run_id", F.lit(RUN_ID))
    table = f"{SILVER}.{table}"
    df.write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable(table)
    t = spark.table(table)
    rows = t.count()
    nulls = t.where(" OR ".join(f"`{c}` IS NULL" for c in grain)).count()
    dups = t.groupBy(*grain).count().where("count > 1").count()
    results.append((RUN_ID, LOADED_AT, table, ",".join(grain), expected_rows, rows, nulls, dups))
    print(f"{table:<46} {rows:>8} rows  null-keys={nulls}  dup-grain={dups}")
    assert rows == expected_rows, f"{table}: expected {expected_rows} rows, got {rows}"
    assert nulls == 0, f"{table}: {nulls} rows with a NULL grain column"
    assert dups == 0, f"{table}: {dups} duplicate grain keys"

# %% [markdown]
# ## NDIS: plan budgets, utilisation, active providers
# Grain: report date × state × service district × disability group × age band × support class
# (utilisation adds the SIL/SDA flag). District rows are published both under their own state
# and under `state = ALL`; both are kept and told apart by `is_all_state`.

# %%
NDIS_DIMS = {"state": "StateCd", "service_district": "SrvcDstrctNm", "disability_group": "DsbltyGrpNm", "age_band": "AgeBnd"}


def ndis_dims(supp_col: str) -> list[Column]:
    return [
        report_date(F.col("RprtDt")).alias("report_date"),
        state(F.col("StateCd")).alias("state_code"),
        dim(F.col("SrvcDstrctNm")).alias("service_district"),
        dim(F.col("DsbltyGrpNm")).alias("disability_group"),
        dim(F.col("AgeBnd")).alias("age_band"),
        support_class(F.col(supp_col)).alias("support_class"),
    ] + all_flags({**NDIS_DIMS, "support_class": supp_col})


NDIS_GRAIN = ["report_date", "state_code", "service_district", "disability_group", "age_band", "support_class"]

# Plan budgets: average annualised committed budget is blank where participants are suppressed.
b = spark.table(f"{BRONZE}.bronze_ndis_plan_budgets")
budget = F.trim(F.col("AvgAnlsdCmtdSuppBdgt"))
plan = b.select(
    *ndis_dims("SuppClass"),
    F.when(budget != "", F.regexp_replace(budget, ",", "").cast("decimal(14,2)")).alias("avg_annualised_budget"),
    (F.coalesce(budget, F.lit("")) == "").alias("avg_annualised_budget_is_suppressed"),
    *count_cols(F.col("ActvPrtcpnt"), "active_participants"),
    *LINEAGE,
)
write_silver(plan, "silver_ndis_plan_budgets", NDIS_GRAIN, b.count())

# Utilisation: "63%" → 0.63. Published as a ratio with no weight; never summed or averaged here.
b = spark.table(f"{BRONZE}.bronze_ndis_utilisation")
util = b.select(
    *ndis_dims("suppclass"),
    dim(F.col("SILorSDA")).alias("sil_or_sda"),
    (F.trim(F.col("SILorSDA")) == "ALL").alias("is_all_sil_or_sda"),
    (F.regexp_replace(F.trim(F.col("Utlstn")), "%", "").cast("decimal(7,4)") / 100).cast("decimal(7,4)").alias("utilisation"),
    F.trim(F.col("Utlstn")).alias("utilisation_raw"),
    *LINEAGE,
)
write_silver(util, "silver_ndis_utilisation", NDIS_GRAIN + ["sil_or_sda"], b.count())

# Active providers: counts are not suppressed by the publisher, but parsed the same way.
b = spark.table(f"{BRONZE}.bronze_ndis_active_providers")
prov = b.select(*ndis_dims("SuppClass"), *count_cols(F.col("PrvdrCnt"), "provider_count"), *LINEAGE)
write_silver(prov, "silver_ndis_active_providers", NDIS_GRAIN, b.count())

# %% [markdown]
# ## NDIS: participants by SA3 and the district → LGA map
# Participants by SA3 is per **state × SA3**: SA3s on a state border appear once per participant
# state. Unknown SA3 (`Other`, `-`) → `UNK`.

# %%
b = spark.table(f"{BRONZE}.bronze_ndis_participants_by_sa3")
sa3_raw = F.trim(F.col("SA3Cd2016"))
sa3 = b.select(
    report_date(F.col("RprtDt")).alias("report_date"),
    state(F.col("StateCd")).alias("state_code"),
    F.when(sa3_raw.rlike(r"^\d{5}$"), sa3_raw).otherwise(F.lit("UNK")).alias("sa3_2016_code"),
    F.when(sa3_raw.rlike(r"^\d{5}$"), F.trim(F.col("SA3Nm2016"))).otherwise(F.lit("Unknown")).alias("sa3_2016_name"),
    sa3_raw.alias("sa3_2016_code_raw"),
    *count_cols(F.col("PrtcpntCnt"), "participants"),
    *LINEAGE,
)
write_silver(sa3, "silver_ndis_participants_by_sa3", ["report_date", "state_code", "sa3_2016_code"], b.count())

b = spark.table(f"{BRONZE}.bronze_ndis_service_district_lga")
lga = b.select(
    state(F.col("RsdsInStateCd")).alias("state_code"),
    F.trim(F.col("RsdsInSrvcDstrctNm")).alias("service_district"),
    F.trim(F.col("LGACd2021")).alias("lga_2021_code"),
    F.trim(F.col("LGANm2021")).alias("lga_2021_name"),
    *LINEAGE,
)
write_silver(lga, "silver_ndis_service_district_lga", ["lga_2021_code"], b.count())

# %% [markdown]
# ## GEN aged care: home care (HCP) and home support (CHSP)
# Kept as **two tables** because they count different things: HCP is a stock (people in a package
# at 30 June), CHSP is a flow (distinct people served in the financial year). Adding them is wrong.
#
# Each program stacks its five region types (LGA, SA3, ACPR, PHN, MMM) in long form:
# one row per period × region type × region × care level (HCP) / region (CHSP).
# HCP `Total` is kept as `care_level = 'Total'` with `is_all_care_level = true` (decision 2).
# `state_code` comes from the first digit of the region code (MMM has no state).

# %%
REGION_TYPES = ["lga", "sa3", "acpr", "phn", "mmm"]


def gen_region(df: DataFrame, region_type: str) -> DataFrame:
    code_col = next(c for c in df.columns if c.startswith(region_type) and c.endswith("_code"))
    name_col = code_col.replace("_code", "_name")
    edition = code_col.split("_")[1]  # e.g. sa3_2016_code → 2016
    code = F.trim(F.col(code_col))
    digit = F.regexp_extract(code, r"^(?:PHN)?(\d)", 1)
    state_map = F.create_map(*[F.lit(x) for kv in STATE_BY_DIGIT.items() for x in kv])
    return df.select(
        "*",
        F.lit(region_type.upper()).alias("region_type"),
        F.lit(edition).alias("region_edition"),
        code.alias("region_code"),
        F.trim(F.col(name_col)).alias("region_name"),
        (F.lit(None).cast("string") if region_type == "mmm" else state_map[digit]).alias("state_code"),
    )


def gen_union(program: str) -> DataFrame:
    frames = [gen_region(spark.table(f"{BRONZE}.bronze_gen_{program}_{rt}"), rt) for rt in REGION_TYPES]
    keep = ["region_type", "region_edition", "region_code", "region_name", "state_code", "_sheet_title", "_source_row"]
    measures = [c for c in frames[0].columns if c.startswith("level_") or c == "total"]
    frames = [f.select(*keep, *measures, "_source_file", "_source_sha256", "_run_id") for f in frames]
    out = frames[0]
    for f in frames[1:]:
        out = out.unionByName(f)
    return out, measures


def unpivot(df: DataFrame, measures: list[str], label: str, value: str) -> DataFrame:
    stack = f"stack({len(measures)}, " + ", ".join(f"'{m}', `{m}`" for m in measures) + f") AS ({label}, {value})"
    others = [c for c in df.columns if c not in measures]
    return df.selectExpr(*[f"`{c}`" for c in others], stack)


GEN_KEEP = ["region_type", "region_edition", "region_code", "region_name", "state_code"]

# Home care (HCP): snapshot at 30 June, from the title ("…, 30 June 2024")
hcp, measures = gen_union("hcp")
hcp_long = unpivot(hcp, measures, "care_level_col", "value_raw")
home_care = hcp_long.select(
    F.to_date(F.regexp_extract("_sheet_title", r"(\d{1,2} \w+ \d{4})$", 1), "d MMMM yyyy").alias("snapshot_date"),
    *GEN_KEEP,
    F.when(F.col("care_level_col") == "total", F.lit("Total"))
    .otherwise(F.concat(F.lit("Level "), F.regexp_extract("care_level_col", r"(\d)$", 1))).alias("care_level"),
    (F.col("care_level_col") == "total").alias("is_all_care_level"),
    *count_cols(F.col("value_raw"), "people"),
    F.col("_source_row").cast("int").alias("_source_row"),
    *LINEAGE,
)
write_silver(
    home_care, "silver_gen_home_care",
    ["snapshot_date", "region_type", "region_code", "care_level"], hcp.count() * len(measures),
)

# Home support (CHSP): financial year from the title ("…, 2023–24"); period ends 30 June.
chsp, measures = gen_union("chsp")
fy_start = F.regexp_extract("_sheet_title", r"(\d{4})[–-]\d{2}$", 1).cast("int")
home_support = chsp.select(
    F.concat(fy_start.cast("string"), F.lit("-"), F.lpad(((fy_start + 1) % 100).cast("string"), 2, "0")).alias("financial_year"),
    F.make_date(fy_start, F.lit(7), F.lit(1)).alias("period_start_date"),
    F.make_date(fy_start + 1, F.lit(6), F.lit(30)).alias("period_end_date"),
    *GEN_KEEP,
    *count_cols(F.col("total"), "people"),
    F.col("_source_row").cast("int").alias("_source_row"),
    *LINEAGE,
)
write_silver(home_support, "silver_gen_home_support", ["financial_year", "region_type", "region_code"], chsp.count())

# %% [markdown]
# ## Parse checks and load log
# Typed columns must parse wherever the raw text was a plain value.

# %%
checks = {
    "silver_ndis_plan_budgets": "report_date IS NULL OR (active_participants IS NULL AND NOT active_participants_is_suppressed)",
    "silver_ndis_utilisation": "report_date IS NULL OR utilisation IS NULL OR utilisation > 1",
    "silver_ndis_active_providers": "report_date IS NULL OR provider_count IS NULL",
    "silver_ndis_participants_by_sa3": "report_date IS NULL OR (participants IS NULL AND NOT participants_is_suppressed)",
    "silver_gen_home_care": "snapshot_date IS NULL OR (people IS NULL AND NOT people_is_suppressed AND people_raw IS NOT NULL)",
    "silver_gen_home_support": "period_end_date IS NULL OR (people IS NULL AND NOT people_is_suppressed AND people_raw IS NOT NULL)",
}
for table, bad in checks.items():
    n = spark.table(f"{SILVER}.{table}").where(bad).count()
    print(f"{table:<46} unparsed={n}")
    assert n == 0, f"{table}: {n} rows failed to parse ({bad})"

log = spark.createDataFrame(
    results,
    "run_id STRING, loaded_at TIMESTAMP, table_name STRING, grain STRING, expected_rows LONG, "
    "actual_rows LONG, null_key_rows LONG, duplicate_keys LONG",
)
log.write.mode("append").format("delta").saveAsTable(f"{SILVER}.silver_load_log")
log.select("table_name", "expected_rows", "actual_rows", "null_key_rows", "duplicate_keys").show(50, truncate=False)

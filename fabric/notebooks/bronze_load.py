# Fabric notebook source (PySpark). Cells are split on "# %%" by scripts/fabric_notebook.py.
# Default lakehouse: lh_care_insights (set at deploy time).

# %% [markdown]
# # Bronze load
# Raw files in `Files/raw/` → one Delta table per source in schema `kanda_bronze`, **as received**:
# - every column is a string (no type casting, no cleaning; that is silver's job),
# - suppressed values (`<11`), `ALL` subtotals and symbols (`n.p.`) are kept as they are,
# - lineage columns: `_source_file`, `_source_sha256`, `_loaded_at`, `_run_id`,
# - each run **overwrites** each table (sources are full snapshots, so reruns are idempotent),
# - a row in `bronze_load_log` is appended per table per run (audit trail for row-count tests).
#
# The only reshaping is for the GEN Excel workbooks: find the header row, drop the empty
# spill columns and blank rows, and snake_case the header so Delta accepts it.

# %%
import io
import re
import uuid
from datetime import datetime, timezone

import pandas as pd
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

RAW = "Files/raw"
SCHEMA = "kanda_bronze"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
RUN_ID = str(uuid.uuid4())
LOADED_AT = datetime.now(timezone.utc).replace(tzinfo=None)  # one timestamp for the whole run

# Bronze table -> glob under Files/raw/
CSV_SOURCES = {
    "bronze_ndis_plan_budgets": "ndis/plan_budgets/plan_budgets_*.csv",
    "bronze_ndis_utilisation": "ndis/utilisation/utilisation_*.csv",
    "bronze_ndis_active_providers": "ndis/providers/active_providers_*.csv",
    "bronze_ndis_participants_by_sa3": "ndis/geography/participants_by_sa3.csv",
    "bronze_ndis_service_district_lga": "ndis/geography/service_district_to_lga_2021.csv",
}
# GEN workbook prefix -> program code used in the table name
GEN_PROGRAMS = {"home_care_recipient_location_": "hcp", "home_support_recipient_location_": "chsp"}

# %% [markdown]
# ## Manifest and integrity check
# The manifest was written by `scripts/download_data.sh`. Every file we load must be in it,
# and its SHA-256 in OneLake must match, so a bronze row can always be traced to the exact
# file that was downloaded.
#
# Scope: the data files bronze loads (`.csv`, `.xlsx`). Docs and PDFs are reference material
# (checked against the manifest by `upload_raw.py` before upload). Spark also treats names
# starting with `_` (e.g. `_data_rules.docx`) as hidden and never lists them.

# %%
DATA_FILE = r"\.(csv|xlsx)$"
manifest = (
    spark.read.option("sep", "\t")
    .schema("path STRING, bytes LONG, sha256 STRING")
    .csv(f"{RAW}/MANIFEST.tsv")
    .where(F.col("path").rlike(DATA_FILE))
)
landed = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .load(RAW)
    .select(
        F.regexp_extract("path", r"Files/raw/(.+)$", 1).alias("path"),
        F.sha2("content", 256).alias("landed_sha256"),
    )
    .where(F.col("path").rlike(DATA_FILE))
)
check = manifest.join(landed, "path", "full_outer")
problems = check.where("sha256 IS NULL OR landed_sha256 IS NULL OR sha256 != landed_sha256").collect()
assert not problems, f"Files/raw does not match MANIFEST.tsv: {problems}"
SHA = {r.path: r.sha256 for r in manifest.collect()}
print(f"integrity ok: {len(SHA)} files")

# %%
def add_lineage(df: DataFrame) -> DataFrame:
    sha_map = F.create_map(*[F.lit(x) for kv in SHA.items() for x in kv])
    return (
        df.withColumn("_source_sha256", sha_map[F.col("_source_file")])
        .withColumn("_loaded_at", F.lit(LOADED_AT).cast("timestamp"))
        .withColumn("_run_id", F.lit(RUN_ID))
    )


results = []


def write_bronze(df: DataFrame, table: str, expected_rows: int, files: list[str]) -> None:
    table = f"{SCHEMA}.{table}"
    df.write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable(table)
    actual = spark.table(table).count()
    results.append((RUN_ID, LOADED_AT, table, len(files), ",".join(sorted(files)), expected_rows, actual))
    assert actual == expected_rows, f"{table}: expected {expected_rows} rows, got {actual}"
    print(f"{table:<50} {actual:>8} rows from {len(files)} file(s)")

# %% [markdown]
# ## NDIS CSVs
# All columns as strings (`inferSchema` off). `FAILFAST` stops the load on a malformed row
# instead of silently nulling it. Expected rows = physical lines minus one header per file.

# %%
for table, pattern in CSV_SOURCES.items():
    path = f"{RAW}/{pattern}"
    df = (
        spark.read.option("header", True)
        .option("inferSchema", False)
        .option("mode", "FAILFAST")
        .csv(path)
        .withColumn("_source_file", F.regexp_extract(F.col("_metadata.file_path"), r"Files/raw/(.+)$", 1))
    )
    lines = (
        spark.read.text(path)
        .withColumn("f", F.col("_metadata.file_path"))
        .groupBy("f").count()
        .collect()
    )
    expected = sum(r["count"] - 1 for r in lines)
    files = df.select("_source_file").distinct().rdd.flatMap(lambda r: r).collect()
    write_bronze(add_lineage(df), table, expected, files)

# %% [markdown]
# ## GEN Excel workbooks
# One bronze table per program × region type (e.g. `bronze_gen_hcp_sa3`), both years stacked.
# `_sheet_title` keeps the table title row, which holds the snapshot date / financial year
# that silver needs.

# %%
def snake(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(name).lower()).strip("_")


def parse_sheet(raw: pd.DataFrame) -> pd.DataFrame:
    first = raw.iloc[:, 0].astype(str)
    header_idx = first[first.str.endswith(" code")].index[0]
    header = raw.iloc[header_idx]
    keep = [c for c in raw.columns if pd.notna(header[c])]
    data = raw.loc[header_idx + 1 :, keep].dropna(how="all")
    data.columns = [snake(header[c]) for c in keep]
    data.insert(0, "_source_row", data.index + 1)  # 1-based Excel row number
    data["_sheet_title"] = raw.iloc[0, 0]
    return data


gen_files = spark.read.format("binaryFile").load(f"{RAW}/gen/*.xlsx").select("path", "content").collect()
gen_tables: dict[str, list[pd.DataFrame]] = {}
for f in gen_files:
    rel = re.search(r"Files/raw/(.+)$", f.path).group(1)
    program = next(code for prefix, code in GEN_PROGRAMS.items() if prefix in rel)
    sheets = pd.read_excel(io.BytesIO(f.content), sheet_name=None, header=None, dtype=str)
    for sheet_name, raw in sheets.items():
        m = re.match(r"Table [\d.]+ \((\w+)\)", sheet_name)
        if not m:
            continue  # Contents, Notes
        data = parse_sheet(raw)
        data["_sheet"] = sheet_name
        data["_source_file"] = rel
        gen_tables.setdefault(f"bronze_gen_{program}_{m.group(1).lower()}", []).append(data)

for table, frames in sorted(gen_tables.items()):
    pdf = pd.concat(frames, ignore_index=True)
    pdf["_source_row"] = pdf["_source_row"].astype(str)
    pdf = pdf.astype(object).where(pdf.notna(), None)  # NaN -> null, everything else string
    schema = ", ".join(f"`{c}` STRING" for c in pdf.columns)
    df = spark.createDataFrame(pdf, schema=schema)
    write_bronze(add_lineage(df), table, len(pdf), pdf["_source_file"].unique().tolist())

# %% [markdown]
# ## Load log

# %%
log = spark.createDataFrame(
    results,
    "run_id STRING, loaded_at TIMESTAMP, table_name STRING, file_count INT, source_files STRING, "
    "expected_rows LONG, actual_rows LONG",
)
log.write.mode("append").format("delta").saveAsTable(f"{SCHEMA}.bronze_load_log")
log.select("table_name", "file_count", "expected_rows", "actual_rows").show(50, truncate=False)

# Fabric notebook source (PySpark). Cells are split on "# %%" by scripts/fabric_notebook.py.
# Default lakehouse: lh_care_insights (set at deploy time).

# %% [markdown]
# # Security: user → state mapping for row-level security
# `Files/security/user_state.csv` → `kanda_gold.security_user_state` (one row per user × state).
#
# The semantic model's one RLS role ("State access") filters `dim_region[state_code]` to the states
# mapped to `USERPRINCIPALNAME()` here. Access changes are data changes: edit the CSV, upload, re-run
# this notebook, re-run `scripts/deploy_semantic_model.py` (reframe). No model edit.
#
# Rules (fail the run rather than load a doubtful row; a bad row must never widen access):
# - `user_principal_name` is lower-cased and must look like a UPN (`x@y`).
# - `state_code` must be a STATE region in `dim_region` (`NSW`…`OT`, `UNK`). `AUS` is refused: state users
#   don't see national rows (decision D3).
# - No duplicate user × state pairs; no blanks.
# - A user with no row sees nothing (the role filter is empty): access is deny-by-default.

# %%
import uuid
from datetime import datetime, timezone

from pyspark.sql import functions as F

GOLD = "kanda_gold"
SOURCE = "Files/security/user_state.csv"
TABLE = f"{GOLD}.security_user_state"
RUN_ID = str(uuid.uuid4())
LOADED_AT = datetime.now(timezone.utc).replace(tzinfo=None)

raw = spark.read.option("header", True).csv(SOURCE)
missing = {"user_principal_name", "state_code"} - set(raw.columns)
assert not missing, f"{SOURCE}: missing columns {missing}"

df = raw.select(
    F.lower(F.trim("user_principal_name")).alias("user_principal_name"),
    F.upper(F.trim("state_code")).alias("state_code"),
)
rows = df.count()

states = {r.state_code for r in spark.table(f"{GOLD}.dim_region").where("region_type = 'STATE'").select("state_code").collect()}
blank = df.where("user_principal_name IS NULL OR user_principal_name = '' OR state_code IS NULL OR state_code = ''").count()
bad_upn = [r.user_principal_name for r in df.where(~F.col("user_principal_name").rlike(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")).collect()]
bad_state = sorted({r.state_code for r in df.where(~F.col("state_code").isin(sorted(states))).collect()})
dupes = rows - df.distinct().count()

assert blank == 0, f"{blank} blank value(s)"
assert not bad_upn, f"not a UPN: {bad_upn}"
assert not bad_state, f"unknown or refused state codes: {bad_state} (allowed: {sorted(states)})"
assert dupes == 0, f"{dupes} duplicate user x state row(s)"
print(f"{rows} rows, {df.select('user_principal_name').distinct().count()} users, states allowed: {sorted(states)}")

# %%
(
    df.withColumn("_gold_loaded_at", F.lit(LOADED_AT).cast("timestamp"))
    .withColumn("_gold_run_id", F.lit(RUN_ID))
    .write.mode("overwrite").option("overwriteSchema", "true").format("delta").saveAsTable(TABLE)
)
actual = spark.table(TABLE).count()
assert actual == rows, f"wrote {actual} rows, expected {rows}"

log = spark.createDataFrame(
    [(RUN_ID, LOADED_AT, "security_user_state", "user_principal_name, state_code", rows, rows, actual, blank, dupes)],
    "run_id STRING, loaded_at TIMESTAMP, table_name STRING, grain STRING, silver_rows LONG, expected_rows LONG, "
    "actual_rows LONG, null_key_rows LONG, duplicate_keys LONG",
)
log.write.mode("append").format("delta").saveAsTable(f"{GOLD}.gold_load_log")
spark.table(TABLE).groupBy("state_code").count().orderBy("state_code").show()

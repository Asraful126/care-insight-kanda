# Data dictionary

Lakehouse `lh_care_insights`, three schemas:

| Schema | Built by | What it holds |
|---|---|---|
| `kanda_bronze` | `fabric/notebooks/bronze_load.py` | Source files as received: every column a string, plus lineage |
| `kanda_silver` | `fabric/notebooks/silver_transform.py` | Typed and standardised, one row per published grain, nothing dropped |
| `kanda_gold` | `fabric/notebooks/gold_build.py` | Star schema for Power BI (Direct Lake) and the assistant's SQL tool |

`fabric/notebooks/gold_tests.py` runs after gold. Sources and licences are in [data-sources.md](data-sources.md).

---

## Design decisions

Each decision lists the options and the reason for the choice.

### 1. Suppression: NULL plus a flag, no imputation (silver)

Publishers hide small counts as `<11`, and secondary suppression produces bounds up to `<45030`.
Silver stores `value = NULL`, `<col>_is_suppressed = true` and `<col>_upper_bound = N − 1`, and keeps the raw text.

- *Rejected: impute (for example `<11` → 5).* With bounds as wide as 45,029, any imputed number would be invented,
  and it would then be summed into totals that look exact.
- *Rejected: drop suppressed rows.* That hides the fact that the cell exists. 35% of plan-budget rows are suppressed.

### 2. Keep the publisher's `ALL` subtotals (silver), and read published cells in gold

Every NDIS dimension has an `ALL` member, so the national total sits next to its breakdowns.
Summing the detail undercounts by about 10%, because suppressed cells count as nothing.

- Silver keeps `ALL` rows and flags them per dimension (`is_all_<dim>`, never NULL).
- Gold facts keep them too. The Power BI measures pick **the one published cell for the current slice**:
  if a dimension isn't filtered, they use its `ALL` row, and if it is filtered, its detail rows. Counts, average
  budgets and utilisation are then always the publisher's own figure, with no undercount and no re-weighting.
  For example, utilisation has no published weight, so a correct rollup can't be computed at all.
- *Rejected: detail rows only.* SUM is simple, but totals are about 10% low and utilisation can't be shown above
  the published grain.
- *Rejected: separate detail and totals facts.* That gives two sources of truth, and every visual has to choose one.
- If a user selects several members of one dimension, the measure has to sum detail rows. It then reports the figure as a lower bound.

### 3. Utilisation stays at its published grain

Utilisation is published as a percentage with no denominator. It's stored as a fraction (0–1) and never summed
or averaged in the pipeline.

### 4. One `dim_region`, keyed by region type × code (gold)

Sources use different geographies: NDIS plans by service district, NDIS participants by SA3 (2016), and GEN
by LGA, SA3, ACPR, PHN and MMM. Gold has one region dimension, with one row per `(region_type, region_code)` and
`state_code` on every row.

- `state_code` is the **RLS key**. One filter on `dim_region[state_code]` secures every fact.
- The national row has `state_code = 'AUS'`, so a state role can't see national totals.
- Cross-program comparison is valid only at **state** and **SA3**, the levels both programs publish.
  Nothing is allocated across boundaries.
- Keys are readable strings, for example `STATE:NSW`, `SERVICE_DISTRICT:NSW:Hunter New England`, `SA3_2016:10102`,
  `LGA_2016:10050` or `NATIONAL:AUS`. The data is small, so string keys cost little. They stay stable across
  reloads (no surrogate key map), and Claude can read them in SQL.
- The **boundary edition is part of the key**: HCP uses LGA 2016 and CHSP uses LGA 2017, and the same code
  can cover a different area in each.
- **MMM is excluded from gold.** Modified Monash is a remoteness class, not a place, and it has no state, so RLS
  can't cover it. It is still in silver.
- *Rejected: `dim_state` plus one dimension per geography.* The meaning is clearer, but it needs more tables
  and relationships for the same filters. *Rejected: state grain only.* There'd be no drill-down and no SA3 comparison.

### 5. District cells published twice: merged, the most precise version wins (gold)

The NDIS publishes each service district cell twice: under its own state, and again under `state = ALL`.
The two versions differ only in secondary suppression. For example, ACT / Autism shows `<5822` under ACT
and exactly `5813` under ALL. Gold merges them into one row:

- an exact value beats a `<N` bound, and if both are bounds, the tighter one wins;
- the build **asserts that they never conflict** (exact values equal, and an exact value within the other's bound).
  Average budgets and utilisation must match exactly;
- the district's state is looked up from its name. Every district name belongs to exactly one state, which is
  asserted. `Other` is the exception because it exists in every state.
- the `Other` district under `state = ALL` is a cross-state bucket with no state, so it is **dropped**. National rows
  still include it.

*Rejected: keep only the state-coded rows.* That would throw away about 4,000 exact counts that the publisher
released.

Result at the first load: suppressed district cells in plans fell from 44,539 to 42,792 (1,747 cells became exact),
with no conflicts.

### 6. One aged care fact, never added across programs (gold)

HCP (Home Care Packages) is a **stock**: people in a package on 30 June. CHSP (Commonwealth Home Support Programme)
is a **flow**: distinct people served during the financial year. Both go in `fact_aged_care_services`, with
`program` and `count_type`. Measures always split by program, and on purpose there's no "total aged care
recipients" measure. CHSP has no care levels, so its rows have `care_level = 'Not applicable'` and
`is_all_care_level = true`.

### 7. Shared NDIS cohort dimensions (gold)

`dim_disability_group`, `dim_age_band` and `dim_support_class` are shared by the plans, utilisation and providers
facts, so one slicer filters all three. That is what makes "participants per provider" possible. Each one
keeps its `ALL` member (`is_all = true`) for decision 2, and report slicers hide it.

### 8. Standardised members (silver)

- State: `State_Missing` and `MIS` become `UNK`. `OT` (Other Territories) is kept.
- Any `<Dim>_Missing` value becomes `Unknown`. A **blank** disability group becomes `Not stated`. It is a separately
  published population (all its utilisation values are 0%), so it isn't merged into Unknown.
- `CapacityBuilding` becomes `Capacity Building`. Dates are parsed from `30SEP2026` and from the GEN sheet titles.

---

## Gold tables

Every gold table also has `_gold_loaded_at` and `_gold_run_id`. Rows trace back to silver and bronze through
`date_key`, `region_key` and the member columns. Silver and bronze carry `_source_file` and `_source_sha256`.

### `dim_date`
Calendar day from 1 Jul 2019 to 30 Jun 2027. Facts join on the **period end** date.

| Column | Type | Notes |
|---|---|---|
| `date_key` | int | `yyyymmdd`, PK |
| `date` | date | |
| `year`, `quarter`, `month` | int | calendar |
| `quarter_label` | string | `2026 Q3` |
| `month_name`, `year_month` | string | `Sep`, `2026-09` |
| `fy_end_year` | int | Australian financial year (Jul–Jun) end year |
| `financial_year` | string | `2025-26` |
| `is_quarter_end`, `is_fy_end` | boolean | NDIS report dates are quarter ends. HCP/CHSP periods end 30 June |

### `dim_region`
| Column | Type | Notes |
|---|---|---|
| `region_key` | string | PK, `<TYPE>[_<edition>]:<code>` |
| `region_type` | string | `NATIONAL`, `STATE`, `SERVICE_DISTRICT`, `PHN`, `ACPR`, `SA3`, `LGA` |
| `region_edition` | string | ABS/Dept edition year (`2016`, `2017`, `2018`). NULL for NATIONAL, STATE, SERVICE_DISTRICT |
| `region_code` | string | source code. Service districts use `<state>:<name>`, and unknown SA3 uses `UNK:<state>` |
| `region_name` | string | if sources name a region differently, the alphabetically first name is kept |
| `state_code` | string | **RLS key**. `AUS` on the national row and `UNK` for unknown state |
| `state_name` | string | |
| `region_type_order` | int | sort order for region-type slicers |
| `parent_region_key` | string | state row for sub-state regions, `NATIONAL:AUS` for states |

### `dim_disability_group`, `dim_age_band`, `dim_support_class`
| Column | Type | Notes |
|---|---|---|
| `disability_group` / `age_band` / `support_class` | string | PK, the member label (including `ALL`, `Unknown`, `Not stated`) |
| `is_all` | boolean | the publisher's subtotal member |
| `sort_order` | int | members, then Unknown / Not stated, then ALL. Age bands are in numeric order |

### `fact_ndis_plans`
Grain: report date × region × disability group × age band × support class. Regions are NATIONAL, STATE or SERVICE_DISTRICT.

| Column | Type | Notes |
|---|---|---|
| `date_key`, `report_date` | int, date | quarter end |
| `region_key` | string | FK `dim_region`. Its region type is the published level |
| `disability_group`, `age_band`, `support_class` | string | FKs to the cohort dims |
| `is_all_disability_group`, `is_all_age_band`, `is_all_support_class` | boolean | copies of the dims' `is_all` for SQL convenience |
| `active_participants` | int | NULL if suppressed |
| `active_participants_is_suppressed` | boolean | |
| `active_participants_upper_bound` | int | `N − 1` for `<N` (the tighter bound after the merge). NULL when exact |
| `avg_annualised_budget` | decimal(14,2) | average annualised committed support budget per participant, in A$. **Not additive** |
| `avg_annualised_budget_is_suppressed` | boolean | blank in source where participants are suppressed |

### `fact_ndis_utilisation`
The same grain as `fact_ndis_plans`, plus `sil_or_sda` (`Yes` / `No` / `ALL`, Supported Independent Living or
Specialist Disability Accommodation) and `is_all_sil_or_sda`.

| Column | Type | Notes |
|---|---|---|
| `utilisation` | decimal(7,4) | fraction 0–1 of committed budget used. **Never sum or average**: read the published cell |

### `fact_ndis_providers`
The same grain as `fact_ndis_plans`.

| Column | Type | Notes |
|---|---|---|
| `provider_count` | int | active providers. **Not additive across regions or cohorts**, because one provider serves many. Plus `_is_suppressed` / `_upper_bound` (the publisher doesn't suppress these) |

### `fact_ndis_participants_sa3`
Grain: report date × SA3 × participant state. It covers 28 quarters, Dec 2019 to Sep 2026.

| Column | Type | Notes |
|---|---|---|
| `date_key`, `report_date` | int, date | |
| `region_key` | string | `SA3_2016:<code>`, or `SA3_2016:UNK:<state>` |
| `participant_state_code` | string | the participant's state. A few SA3s appear under two states, so it's part of the grain. RLS uses the SA3's state from `dim_region` |
| `participants` (+ `_is_suppressed`, `_upper_bound`) | int | additive across SA3s, but suppressed cells are missing from sums |

### `fact_aged_care_services`
Grain: period end × program × region × care level. Regions are LGA, SA3, ACPR or PHN.

| Column | Type | Notes |
|---|---|---|
| `date_key`, `period_end_date` | int, date | 30 June |
| `period_label` | string | `30 Jun 2025` (HCP) or `FY 2024-25` (CHSP) |
| `program`, `program_name` | string | `HCP` Home Care Packages, `CHSP` Commonwealth Home Support Programme |
| `count_type` | string | `stock` (HCP) or `flow` (CHSP). **Never add across programs** |
| `region_key` | string | FK `dim_region`. Region types overlap (an SA3 and its LGAs), so filter to one type before summing |
| `care_level` | string | HCP `Level 1`–`Level 4` and `Total`. CHSP `Not applicable` |
| `is_all_care_level` | boolean | HCP `Total` and all CHSP rows |
| `people` (+ `_is_suppressed`, `_upper_bound`) | int | `n.p.` → NULL and suppressed. Records with no usable address are excluded at the source, so region sums are slightly below national figures |

### Logs
| Table | One row per |
|---|---|
| `kanda_bronze.bronze_load_log`, `kanda_silver.silver_load_log`, `kanda_gold.gold_load_log` | table × run: expected vs actual rows, NULL keys, duplicate keys |
| `kanda_gold.gold_test_results` | test × run: name, target, passed, detail |

---

## Tests (`gold_tests.py`)

| Test | What it checks |
|---|---|
| `not_null_keys`, `unique_grain` | every gold table's grain is unique and fully populated |
| `fk_exists` | every fact `date_key`, `region_key` and cohort member exists in its dimension (orphans vanish from visuals) |
| `not_null_all_flags` | no `is_all_*` flag is NULL. The published-cell measures depend on them |
| `region_has_state`, `region_parent_exists` | every region has an RLS key and a valid parent |
| `rows_bronze_silver` | NDIS: silver = bronze. GEN: silver = bronze × measure columns (unpivot) |
| `rows_silver_gold` | NDIS: gold = silver − `state = ALL` district rows (merged or dropped `Other`). GEN: gold = silver − MMM |
| `anchor_national_*` | 30 Sep 2026 national active participants = 792,304 and providers = 11,083, as published |
| `merge_reduces_suppression` | the district merge only ever removes suppression |
| `aged_care_count_type`, `utilisation_range` | HCP is stock and CHSP is flow, and utilisation is in 0–1 |

The tests recompute everything from the tables, not from the build's own log, so a bug in the build can't hide itself.

# Data sources

All inputs are public Australian Government data. Nothing here is client or personal data: every
file is published already aggregated, with small cells suppressed by the publisher.

Raw files are not committed. Run `scripts/download_data.sh` to fetch them into `data/raw/` (git-ignored).
The script also writes `data/raw/MANIFEST.tsv` (path, bytes, SHA-256), which the bronze load uses to
stamp lineage.

**Downloaded:** 18 Nov 2026.

**Scope trimmed to save capacity time.** NDIS plan budgets and utilisation are kept for two
quarters only: Sep 2025 and Sep 2026, which is enough for year-on-year comparison. The publisher has 8
quarters back to Dec 2024; to load them, uncomment the lines in `scripts/download_data.sh` (about 130 MB
in total, compared with 45 MB now). Participants by SA3 is a single 375 KB file covering 28 quarters, so
it is kept whole.

## Summary

| # | Dataset | Publisher | Licence | Data as at | Refresh | Grain (one row = …) |
|---|---|---|---|---|---|---|
| 1 | NDIS participant numbers and plan budgets | NDIA | CC BY 4.0 | 30 Sep 2025, 30 Sep 2026 | Quarterly | quarter × state × service district × disability group × age band × support class |
| 2 | NDIS utilisation of plan budgets | NDIA | CC BY 4.0 | 30 Sep 2025, 30 Sep 2026 | Quarterly | quarter × state × service district × disability group × age band × SIL/SDA flag × support class |
| 3 | NDIS active providers | NDIA | CC BY 4.0 | 30 Sep 2025, 30 Sep 2026 | Quarterly | quarter × state × service district × disability group × age band × support class |
| 4 | NDIS participants by SA3 | NDIA | CC BY 4.0 | 28 quarters to 30 Sep 2026 | Quarterly | quarter × SA3 (2016) |
| 5 | NDIS service district to LGA 2021 | NDIA | CC BY 4.0 | Static | — | service district × LGA (2021) |
| 6 | GEN people using home care (HCP) by recipient location | AIHW | CC BY 4.0 | 30 Jun 2024, 30 Jun 2025 | Annual | snapshot date × region × HCP level (one sheet per region type) |
| 7 | GEN people using home support (CHSP) by recipient location | AIHW | CC BY 4.0 | FY 2023–24, FY 2024–25 | Annual | financial year × region (one sheet per region type) |
| 8 | NDIS Practice Standards and Quality Indicators, v4 (Nov 2021) | NDIS Quality and Safeguards Commission | To confirm (Commonwealth; likely CC BY 4.0) | Nov 2021 | On revision | 45-page PDF, for RAG |
| 9 | Strengthened Aged Care Quality Standards (Nov 2025) | Department of Health, Disability and Ageing | To confirm (Commonwealth; likely CC BY 4.0) | In force from 1 Nov 2025 | On revision | ~51-page PDF, for RAG |

Attribution:
- NDIA data: "© National Disability Insurance Scheme Agency 2013". Source: dataresearch.ndis.gov.au.
- AIHW data: unchanged tables say "Source: Australian Institute of Health and Welfare"; derived tables and
  charts say "Based on Australian Institute of Health and Welfare material". Source: gen-agedcaredata.gov.au.

## Source pages

- NDIS participant datasets (1, 2, 4, 5): https://dataresearch.ndis.gov.au/datasets/participant-datasets
- NDIS provider datasets (3): https://dataresearch.ndis.gov.au/datasets/provider-datasets
- GEN people using aged care by region, release of 12 Feb 2026 (6, 7):
  https://www.gen-agedcaredata.gov.au/resources/access-data/2026/february/gen-data-people-using-aged-care-by-region
- NDIS Practice Standards (8): https://www.ndiscommission.gov.au/rules-and-standards/ndis-practice-standards
- Strengthened Aged Care Quality Standards (9): https://www.health.gov.au/our-work/aged-care-quality-standards

Each NDIS dataset comes with a "data rules" DOCX, which is downloaded next to the CSVs in `data/raw/ndis/`.

## Files

```
data/raw/
  ndis/plan_budgets/plan_budgets_{2025-09,2026-09}.csv  + _data_rules.docx
  ndis/utilisation/utilisation_{2025-09,2026-09}.csv    + _data_rules.docx
  ndis/providers/active_providers_YYYY-MM.csv      (2)  + _data_rules.docx
  ndis/geography/participants_by_sa3.csv, service_district_to_lga_2021.csv (+ rules)
  ndis/commonly_used_data_terms.docx
  gen/home_care_recipient_location_{2024,2025}-06-30.xlsx
  gen/home_support_recipient_location_{2023-24,2024-25}.xlsx
  policy/ndis_practice_standards_v4.pdf, strengthened_aged_care_quality_standards.pdf
  MANIFEST.tsv
```

## Columns and quirks found when profiling

These affect the silver and gold design.

### NDIS (1–4)

Columns: `RprtDt` (e.g. `30SEP2026`), `StateCd`, `SrvcDstrctNm`, `DsbltyGrpNm`, `AgeBnd`, plus:
- Plan budgets: `SuppClass`, `AvgAnlsdCmtdSuppBdgt` (average annualised committed budget, text like
  `"221,000.00"`), `ActvPrtcpnt` (active participants).
- Utilisation: `SILorSDA`, `suppclass`, `Utlstn` (text like `63%`).
- Providers: `SuppClass`, `PrvdrCnt`.
- SA3: `SA3Cd2016`, `SA3Nm2016`, `PrtcpntCnt`.

Quirks:
1. **Pre-aggregated subtotals.** Every dimension has an `ALL` member, so the national total (792,304
   active participants at 30 Sep 2026) sits in the same table as its breakdowns. Summing all rows
   double-counts.
2. **Not additive.** The budget is an average per participant and utilisation is a percentage. Neither
   can be summed; they can only be rolled up as weighted values (budget × participants), and
   utilisation has no published weight.
3. **Suppression.** Small counts are published as `<11`, with the budget left blank (34,843 of 99,265
   plan-budget rows at Sep 2026). There are also `<18` to `<21` values, which look like secondary
   suppression so that hidden cells can't be worked out by subtraction.
4. **Unknown members.** `State_Missing`, `OT` (other territories), `MIS`, `DsbltyGrp_Missing`.
5. **Inconsistent labels.** Support class is `CapacityBuilding` in plan budgets and `Capacity Building` in
   utilisation and providers.
6. **Format drift** (only matters if older quarters are added back). `utilisation_2024-12` is an Excel workbook (the publisher's link says CSV). It has
   real dates and stores utilisation as a fraction (`0.72`); later files use text (`30SEP2026`, `72%`).
7. **Geography.** Plans, utilisation and providers go down to **service district** (about 80). Only the
   participant-count file is at **SA3 (2016)**. NDIS publishes a service district → LGA 2021 mapping.

### GEN (6–7)

Each workbook has `Contents` and `Notes` sheets, then one table per region type: LGA, SA3 (2016),
ACPR (2018), PHN, MMM. Each table has a title row and a "Return to contents" row above the header.

Quirks:
1. **Two programs with two kinds of count.** HCP (home care) is a **stock**: people in a package at
   30 June, split by level 1–4. CHSP (home support) is a **flow**: distinct people served during the
   financial year. Adding them together would count wrong.
2. **Symbols in numeric columns:** `n.p.` (not published), `n.a.`, `. .`.
3. Records with no usable address are excluded, so region totals are slightly below national totals.
4. **Program change.** HCP and CHSP were replaced by Support at Home from 1 Nov 2025. These tables are
   from before the change. No regional Support at Home table had been published by the download date.
   The Department's quarterly Support at Home reports are PDF/DOCX, not data tables.

### Shared region key

SA3 (2016) appears in both NDIS (participants by SA3) and GEN (tables 1.2 / 2.2), so it is the natural
key for comparing disability and aged care in the same area. NDIS budgets and utilisation are only by
service district, which maps to LGA 2021. GEN's LGA tables use the 2016/2017 LGA editions.

#!/usr/bin/env bash
# Download the public source files into data/raw/ (git-ignored).
# Re-runnable: skips files already present. See docs/data-sources.md for what each file is.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$ROOT/data/raw"
UA="Mozilla/5.0 (care-insights-portfolio)"

NDIS="https://dataresearch.ndis.gov.au/media"
GEN="https://www.gen-agedcaredata.gov.au/getmedia"

# target path (relative to data/raw) | source URL
FILES=(
  # NDIS: participant numbers and plan budgets. Trimmed to Sep 2025 + Sep 2026 (enough for YoY)
  # to save capacity time; uncomment the other quarters for a full quarterly trend.
  # "ndis/plan_budgets/plan_budgets_2024-12.csv|$NDIS/4233/download?attachment"
  # "ndis/plan_budgets/plan_budgets_2025-03.csv|$NDIS/4314/download?attachment"
  # "ndis/plan_budgets/plan_budgets_2025-06.csv|$NDIS/4366/download?attachment"
  "ndis/plan_budgets/plan_budgets_2025-09.csv|$NDIS/4396/download?attachment"
  # "ndis/plan_budgets/plan_budgets_2025-12.csv|$NDIS/4484/download?attachment"
  # "ndis/plan_budgets/plan_budgets_2026-03.csv|$NDIS/4528/download?attachment"
  # "ndis/plan_budgets/plan_budgets_2026-06.csv|$NDIS/4573/download?attachment"
  "ndis/plan_budgets/plan_budgets_2026-09.csv|$NDIS/4611/download?attachment"
  "ndis/plan_budgets/_data_rules.docx|$NDIS/4260/download?attachment"

  # NDIS: utilisation of plan budgets. Same two quarters as plan budgets.
  # "ndis/utilisation/utilisation_2024-12.xlsx|$NDIS/4253/download?attachment"
  # "ndis/utilisation/utilisation_2025-03.csv|$NDIS/4315/download?attachment"
  # "ndis/utilisation/utilisation_2025-06.csv|$NDIS/4367/download?attachment"
  "ndis/utilisation/utilisation_2025-09.csv|$NDIS/4398/download?attachment"
  # "ndis/utilisation/utilisation_2025-12.csv|$NDIS/4485/download?attachment"
  # "ndis/utilisation/utilisation_2026-03.csv|$NDIS/4529/download?attachment"
  # "ndis/utilisation/utilisation_2026-06.csv|$NDIS/4574/download?attachment"
  "ndis/utilisation/utilisation_2026-09.csv|$NDIS/4612/download?attachment"
  "ndis/utilisation/_data_rules.docx|$NDIS/4270/download?attachment"

  # NDIS: active providers, latest and same quarter last year
  "ndis/providers/active_providers_2025-09.csv|$NDIS/4450/download?attachment"
  "ndis/providers/active_providers_2026-09.csv|$NDIS/4621/download?attachment"
  "ndis/providers/_data_rules.docx|$NDIS/4255/download?attachment"

  # NDIS: geography
  "ndis/geography/participants_by_sa3.csv|$NDIS/4241/download?attachment"
  "ndis/geography/participants_by_sa3_data_rules.docx|$NDIS/4264/download?attachment"
  "ndis/geography/service_district_to_lga_2021.csv|$NDIS/3218/download?attachment"
  "ndis/geography/service_district_to_lga_2021_data_rules.docx|$NDIS/3219/download?attachment"
  "ndis/commonly_used_data_terms.docx|$NDIS/1965/download?attachment"

  # AIHW GEN: people using aged care by region (release 12 Feb 2026)
  "gen/home_care_recipient_location_2025-06-30.xlsx|$GEN/6e2f01d1-f230-4e0d-b869-8c746aa3e372/GEN-data-People-using-aged-care-by-region-30-June-2025-2-home-care-(recipient-location)"
  "gen/home_care_recipient_location_2024-06-30.xlsx|$GEN/5f954840-c9f2-4d9e-adc8-445c9329ee40/GEN-data-People-using-aged-care-by-region-30-June-2024-2-home-care-(recipient-location)"
  "gen/home_support_recipient_location_2024-25.xlsx|$GEN/53b57e6e-6be8-401b-b1e8-9baf430d18d4/GEN-data-People-using-aged-care-by-region-2024-25-1-home-support-(recipient-location)"
  "gen/home_support_recipient_location_2023-24.xlsx|$GEN/989a2cb9-7793-40c8-a000-5ea2a980bb03/GEN-data-People-using-aged-care-by-region-2023-24-1-home-support-(recipient-location)"

  # Policy documents for RAG
  "policy/ndis_practice_standards_v4.pdf|https://www.ndiscommission.gov.au/sites/default/files/2024-10/ndis-practice-standards-and-quality-indicators.pdf"
  "policy/strengthened_aged_care_quality_standards.pdf|https://www.health.gov.au/sites/default/files/2026-08/strengthened-aged-care-quality-standards_0.pdf"
)

for entry in "${FILES[@]}"; do
  rel="${entry%%|*}"
  url="${entry#*|}"
  out="$RAW/$rel"
  if [[ -s "$out" ]]; then
    echo "skip  $rel"
    continue
  fi
  mkdir -p "$(dirname "$out")"
  echo "get   $rel"
  curl -fsSL --retry 3 -A "$UA" -o "$out.part" "$url"
  mv "$out.part" "$out"
done

# Record what was downloaded and when, so the bronze load can stamp it.
( cd "$RAW" && find . -type f ! -name MANIFEST.tsv ! -name '*.part' | sort | while read -r f; do
    printf '%s\t%s\t%s\n' "${f#./}" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)"
  done ) > "$RAW/MANIFEST.tsv"
echo "manifest: $RAW/MANIFEST.tsv ($(wc -l < "$RAW/MANIFEST.tsv") files)"

# Care Insights Platform

A data platform and AI assistant for a care provider (disability/NDIS and aged home care), built end to
end on **public data** in my own Azure tenant and Microsoft Fabric capacity.

It shows the pieces a care provider's data platform needs: a governed **Fabric lakehouse**, an
**LLM assistant with RAG and tools** that can only read approved data, automated tests and an evaluation
set, and (in progress) **Power BI with row-level security** and a **containerised deployment with CI/CD**.

> Personal portfolio project. Not affiliated with any provider. Public data only: no client data, code or tenants.

## Status

| Part | Status |
|---|---|
| Public data sources and download script | Done |
| Bronze → silver → gold lakehouse, with 60 automated tests | Done |
| Read-only assistant views and a least-privilege service principal | Done |
| Claude assistant: governed SQL tool + policy search with page citations | Done: **20/20** on the eval set |
| Power BI: Direct Lake semantic model, DAX measures, RLS by state | In progress |
| Docker → Azure Container Apps, GitHub Actions CI/CD | In progress |
| Governance doc, architecture diagram, demo video | In progress |

## What you can do with the assistant

![The assistant answering a question about NDIS participants in Queensland, with a citation](docs/images/chat-data.png)

Ask in plain English, and get an answer with its source:

- **Explore NDIS statistics**: active participants, average plan budgets, plan utilisation and provider numbers
  by state and service district (Sep 2025 and Sep 2026), broken down by disability group, age band or support class,
  plus participants by SA3 every quarter since 2019.
- **Explore aged care at home**: people in Home Care Packages (by level) and people served by the Commonwealth
  Home Support Programme, by LGA, SA3, planning region or PHN.
- **Compare and track change**: year-on-year change and % growth, highest and lowest regions, one region against another.
- **Ask about the quality standards**: what the NDIS Practice Standards and the Strengthened Aged Care Quality
  Standards require, with the document and page cited.
- **Check every answer**: expand **Sources** to see the exact SQL that ran and the rows it returned, or the
  policy passages found.

It **won't** identify or speculate about individual people or providers, give clinical, legal or funding advice,
answer beyond its data, or change any data. Hidden small counts are reported as "fewer than N", never guessed.

Full guide with example questions: [docs/user-guide.md](docs/user-guide.md).

## Architecture

```text
Public sources (NDIA, AIHW, policy PDFs)
  │  scripts/download_data.sh  → data/raw/ + MANIFEST.tsv (size + SHA-256)
  │  scripts/upload_raw.py     → OneLake Files/raw/
  ▼
Microsoft Fabric lakehouse  lh_care_insights
  kanda_bronze   15 tables   files as received, every column a string, load timestamp + lineage
  kanda_silver    7 tables   typed, standard region keys, one row per published grain
  kanda_gold     star schema 5 dimensions + 5 facts           ──►  Power BI (Direct Lake, RLS)  [in progress]
  gold_tests     60 checks   unique / not-null keys, row counts bronze → gold
  kanda_assistant  7 read-only views (joined, documented contract over gold)
  ▼
Assistant (Streamlit → Claude API, claude-opus-5-5)
  query_gold(sql)       one SELECT over allow-listed views, run as a read-only service principal
  search_policy(query)  NDIS Practice Standards + Aged Care Quality Standards, cited by page
  every question, tool call and answer logged as JSON lines
```

## Data

| Source | Publisher | Grain |
|---|---|---|
| NDIS participants, plan budgets, utilisation, active providers | NDIA | quarter × state × service district × cohort |
| NDIS participants by SA3 | NDIA | quarter × SA3 |
| People using home care (HCP) and home support (CHSP) | AIHW GEN | period × region |
| NDIS Practice Standards v4; Strengthened Aged Care Quality Standards (Nov 2025) | NDIS Commission; Dept of Health | PDFs for retrieval |

All CC BY 4.0 or Commonwealth publications. Licences, refresh dates and quirks: [docs/data-sources.md](docs/data-sources.md).

## Key design decisions

Full reasoning, including the options I rejected, is in [docs/data-dictionary.md](docs/data-dictionary.md)
and [docs/assistant.md](docs/assistant.md).

### Lakehouse

- **Suppressed cells stay NULL with a flag and an upper bound**, never imputed: with bounds as wide as 45,029,
  any imputed number would be invented and then summed into totals that look exact.
- **Publisher subtotals are kept, and measures read the published cell** rather than summing the parts:
  summing the detail undercounts by about 10%, because suppressed cells count as nothing.
- **One `dim_region` keyed by region type × code**, with `state_code` on every row as the row-level security key.
- **Aged care programs are never added together**: HCP is a stock (people in a package on 30 June) and CHSP
  is a flow (people served during the year), so there is deliberately no "total recipients" measure.

### Assistant and governance

- **The database is the security boundary.** The service principal is a workspace Viewer with
  `GRANT SELECT` on the assistant views only and `DENY` on the bronze, silver and gold tables (ownership chaining keeps the
  views working). `scripts/setup_assistant_sql.py` proves on every run that all 7 views are readable, all 36
  tables are denied, and DDL is denied.
- **App-side SQL guard** (sqlglot): exactly one SELECT, allow-listed views only, no DML/DDL/EXEC/`SELECT INTO`,
  200-row cap, 30-second timeout. 24 unit tests. It returns precise errors that Claude can correct.
- **Hybrid retrieval without a vector database**: local fastembed embeddings (bge-small) plus BM25, merged
  with reciprocal rank fusion. 217 page-aware chunks, so every answer cites one PDF page.
- **Scope rules in the prompt**: answer only from tool results and cite them, decline questions about individuals,
  give no clinical, legal or funding advice, and treat tool output as data, not instructions.

## Evaluation

[eval/questions.yaml](eval/questions.yaml) holds 20 questions with expected answers, written from the views and PDFs
independently of the assistant: 13 data questions, 4 policy questions and 3 scope questions (personal data,
out of coverage, prompt injection). Grading is deterministic: required numbers within a tolerance,
phrases, citations, tools used, and no successful write.

**Latest: 20/20, US$0.70 per run.** Details: [eval/results/latest.md](eval/results/latest.md).

## How to run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                      # fill in; .env is git-ignored

scripts/download_data.sh                  # public sources → data/raw/
.venv/bin/python scripts/upload_raw.py    # → OneLake
for nb in bronze_load silver_transform gold_build gold_tests; do
  .venv/bin/python scripts/fabric_notebook.py deploy-run fabric/notebooks/$nb.py
done

.venv/bin/python scripts/setup_assistant_sql.py       # views, grants, proof of access
.venv/bin/python -m assistant.policy_search build     # chunk + embed the PDFs
.venv/bin/streamlit run assistant/app.py              # chat page
.venv/bin/python -m pytest tests                      # SQL guard tests (no cloud needed)
.venv/bin/python eval/run_eval.py                     # 20-question eval
```

Needs a Fabric capacity (F2 here), a service principal for the reader identity, the Microsoft ODBC Driver 18
for SQL Server, and an Anthropic API key.

## Cost

Total budget about A$50, with an Azure budget alert at A$30. The F2 capacity is paused between sessions;
the Anthropic key is prepaid with a hard spend limit. One eval run costs about US$0.70.

## Repository

| Path | What |
|---|---|
| `fabric/notebooks/` | Bronze, silver, gold and test notebooks (`.py` is the source of truth, deployed by script) |
| `fabric/sql/` | Assistant views |
| `assistant/` | Agent loop, SQL tool and guard, policy search, Streamlit app |
| `eval/` | Questions, runner, latest results |
| `scripts/` | Download, upload, Fabric API client, notebook deploy/run, SQL access setup |
| `tests/` | SQL guard unit tests |
| `docs/` | User guide, data sources, data dictionary, assistant design |

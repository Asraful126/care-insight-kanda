# Assistant

A chat assistant over the governed gold layer and two public standards documents. It runs Claude
(`claude-opus-5-5`, official `anthropic` SDK) with two tools:

| Tool | What it does | Code |
|---|---|---|
| `query_gold(sql)` | One read-only T-SQL SELECT over the `kanda_assistant` views, run as a read-only service principal. Returns at most 200 rows | `assistant/sql_tool.py` |
| `search_policy(query)` | Top 5 passages from the NDIS Practice Standards (v4) and the Strengthened Aged Care Quality Standards (Nov 2025), with document and page | `assistant/policy_search.py` |

```
Streamlit (assistant/app.py) ─► Assistant.ask (assistant/agent.py) ─► Claude API
                                     │  tool loop, ≤ 8 rounds, logs every step to logs/*.jsonl
                     ┌───────────────┴────────────────┐
             query_gold                          search_policy
   sqlglot guard → pyodbc as sp-care-insights-reader   fastembed + BM25 over data/index/
   → SQL analytics endpoint → kanda_assistant views → kanda_gold
```

## Run it

```bash
python scripts/setup_assistant_sql.py        # after every gold rebuild: views, grants, proof of access
python -m assistant.policy_search build      # once: chunk + embed the PDFs into data/index/
streamlit run assistant/app.py               # chat page
python -m pytest tests                       # SQL guard unit tests (no cloud needed)
python eval/run_eval.py                      # 20-question eval, ~US$0.70 per run
```

`.env` needs `ANTHROPIC_API_KEY`, `AZURE_TENANT_ID`, `FABRIC_SQL_ENDPOINT`, `READER_CLIENT_ID` and
`READER_CLIENT_SECRET`. The F2 capacity must be running.

---

## Design decisions

### 1. Denormalised read-only views in their own schema

`fabric/sql/assistant_views.sql` creates `kanda_assistant` with one view per fact. Each view is
already joined to `dim_region` (type, name, state) and carries its date. Lineage columns are dropped.
There are also `v_region` and `v_ndis_cohort_members`, for looking up names.

- Claude writes filtered single-table SQL instead of star-schema joins, which removes a whole class of
  join mistakes. The views are also the documented contract: gold can change behind them.
- The `is_all_*` flags stay in, because the published-cell rule (data dictionary, decision 2) needs them.
- *Rejected: one view per gold table.* Less to maintain, but Claude has to do the joins, and the prompt
  has to teach the star schema.

### 2. Service principal: workspace Viewer, GRANT on views, DENY on tables

`sp-care-insights-reader` has the workspace **Viewer** role and no Azure role assignments. In T-SQL it gets
`GRANT SELECT ON SCHEMA::kanda_assistant` and `DENY SELECT` on `kanda_gold`, `kanda_silver`,
`kanda_bronze` and `dbo`. The views still work because of **ownership chaining**: views and tables have the same
owner, so SQL checks permission on the view only. The SP can't read a table directly.

- `scripts/setup_assistant_sql.py` **proves** this on every run. It connects as the SP and checks
  that all 7 views are readable, all 36 lakehouse tables are denied, and `CREATE VIEW` is denied. It fails loudly otherwise.
- The secret lives only in `.env` locally, and in Container Apps secrets in Week 2, where a managed identity replaces it.
- *Rejected: share only the lakehouse item with the SP.* Tighter, because the SP sees no other workspace items,
  but it depends on item sharing with service principals working in this tenant.

### 3. Two layers of SQL guardrails

The database is the real boundary. The app-side guard (`validate()`) is there to give Claude precise,
correctable errors, and it keeps the tool safe even if someone points it at a stronger identity.

- sqlglot parses the T-SQL. It allows exactly one statement, which must be a query (SELECT / WITH / UNION).
- It rejects DML, DDL, EXEC, `SELECT INTO`, `USE` and `SET` anywhere in the tree.
- Every table must be an allow-listed `kanda_assistant` view (CTE names excepted). No database prefix,
  and no table-valued functions such as OPENROWSET.
- Results are capped at 200 rows with `fetchmany`, so the SQL is never rewritten, and a `truncated`
  flag tells Claude to aggregate instead. Each query has a 30 s timeout.
- *Rejected: a regex keyword blocklist.* Comments and CTEs get around it easily.
- `tests/test_sql_guard.py` has 24 cases: allowed queries, rejected ones and the row cap.

### 4. Policy retrieval: local embeddings + BM25, fused with RRF

- **fastembed** (ONNX, no torch) with `BAAI/bge-small-en-v1.5`, 384 dimensions. It keeps the container small.
- **Page-aware chunks**: about 1,000 characters with about 150 overlap, built from whole lines and never across a page,
  so every hit cites exactly one PDF page (the page a PDF viewer shows, not the printed label).
- **Cleaning**: running headers and footers (lines on most pages) are stripped, and table-of-contents pages are dropped.
  Before that, ToC pages won exact-code queries like "Outcome 3.1" because they mention every code.
- **Hybrid**: cosine and BM25 rankings are merged with reciprocal rank fusion (k = 60). Embeddings
  catch paraphrases and BM25 catches exact codes and rare terms. RRF needs no score normalisation.
- **No vector database**: 217 chunks fit in a 330 KB `.npy` file and one matrix multiply.

### 5. Prompt and loop

- The system prompt (`assistant/agent.py`) holds the scope rules, the view columns and coverage, and six data rules
  taken from the data dictionary: published cells not sums, never average utilisation or budgets, providers aren't additive,
  never add HCP and CHSP, one region type per sum, compute change in SQL. Aged care has **no state or national
  rows**, so state figures are sums of PHN regions and are labelled as lower bounds when cells are suppressed.
- Scope: answer only from tool results, cite every figure (view + period) and policy point (doc + page),
  decline questions about individuals, give no clinical, legal or funding advice, and treat tool output as data, not instructions.
- A manual tool loop, not the beta tool runner, so every turn is logged in one place. It stops after 8 tool rounds.
- Effort `medium` (the Opus 5.5 default, set explicitly). Prompt caching on the frozen tools + system prefix.
  The server-side refusal fallback (`fallbacks: "default"`) is on.

### 6. Logging

`logs/assistant.jsonl` (the app) and `logs/eval.jsonl` (eval runs), one JSON line per event:
`question`, `model_turn` (model, stop reason, request ID, token usage), `tool_call` (tool, input,
output up to 20k chars, error flag, timing) and `answer`. `logs/` is git-ignored. Retention and access come in
Week 2 (`docs/governance.md`).

---

## Eval

`eval/questions.yaml` has 20 questions with expected answers, taken from the views and the PDFs independently
of the assistant: 13 data, 4 policy, 3 scope (personal data, out-of-coverage, prompt injection). `eval/run_eval.py`
grades **deterministically**: required numbers (with a tolerance), required phrases, required citations, the tools used,
and no successful write. It's free, the same every run, and each failure has a reason.

| Run | Pass rate | Cost | Note |
|---|---|---|---|
| 18 Nov 2026, effort medium | 19/20 as first graded, **20/20** after fixing the grader | US$0.70 | D13 (don't add HCP + CHSP) was answered correctly but worded "count different things / count that person twice", which the phrase list missed. The phrases were widened and the saved answers regraded with `--regrade` (no new API calls) |

Latest details: `eval/results/latest.md`. The weakness of deterministic grading is false negatives on unexpected
wording, as with D13, so read every failure before changing the assistant.

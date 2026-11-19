# Governance, privacy and security

This page gathers the platform's data governance controls in one place: what the data is, who and what can
reach it, what the assistant will and won't do, what's logged, where secrets live, and the known limits.
Each control names where it's enforced and how it was tested. The reasoning behind the data design is in
[data-dictionary.md](data-dictionary.md), and the assistant's design is in [assistant.md](assistant.md).

## 1. Data classification

There are three levels. Each asset is classified by its most sensitive content.

| Level | Meaning | Assets here |
|---|---|---|
| **Public** | Already published; no harm if disclosed | Source files and every lakehouse table built from them (bronze, silver, gold), the semantic model's data, the Power BI report, the policy index (excerpts of two published standards), the container image |
| **Internal** | Not published; harm if disclosed is limited but real | Assistant logs (they hold whatever users type), `kanda_gold.security_user_state` (staff e-mail → state), Fabric and Azure configuration |
| **Confidential** | Disclosure gives someone access or spends money | The Anthropic API key, service principal secrets, the app's access code |

**Public data only.** All sources are published aggregates (NDIS public data, AIHW GEN, two standards documents;
see [data-sources.md](data-sources.md)). There's no client, participant or staff record data anywhere in the platform.
Real deployments at a care provider would add a **Restricted** level for client records, which this design keeps
out of the assistant's reach entirely (section 3).

## 2. Access model

Every identity gets the least it needs, and each limit is enforced by the platform, not by the app.

| Who or what | Can access | Enforced by | Tested by |
|---|---|---|---|
| Platform owner (one Entra member user) | Everything: workspace Admin, subscription Owner | Entra + Fabric roles. Per-user MFA is enforced | |
| **Assistant, locally**: service principal `sp-care-insights-reader` | Only the 7 views in `kanda_assistant`. SELECT only | Workspace **Viewer**; T-SQL `GRANT SELECT` on the view schema and `DENY SELECT` on `kanda_gold`, `kanda_silver`, `kanda_bronze` and `dbo` | `scripts/setup_assistant_sql.py`: 7 views readable, every table denied, DDL denied |
| **Assistant, in Azure**: the Container App's **managed identity** | Same as the line above | Same roles and GRANT/DENY. **No secret exists**: Azure issues its tokens | A data question answered in Azure (QLD participants 30 Sep 2026 = 167,942) |
| **Semantic model**: service principal `sp-care-insights-model` | Read on `Tables/kanda_gold` in OneLake only | Workspace Viewer + a OneLake security role (`GoldModelReader`). No *ReadAll* | Reads gold; silver, bronze and `Files/` (including the RLS mapping) are denied |
| **Report users** | Their own states' rows | Model **row-level security** (one dynamic role, see below). Read on the model only, **no workspace role** | `scripts/test_rls.py`: 19/19 |
| **CI/CD**: GitHub Actions | Update one Container App | **OIDC**: no stored secret. The deploy identity is Contributor on the app and its environment only, not the resource group. It's trusted only for jobs in the repo's `production` environment | Every merge to `main` deploys itself |

**Row-level security.** One role filters `dim_region[state_code]` to the states mapped to the signed-in user in
`security_user_state`. Because every region row carries its state, that one filter reaches every fact.

- **Deny by default:** a user with no mapping sees nothing.
- **No national rows:** the national row can't be mapped (the load refuses `AUS`), so a state user never sees
  national figures.
- **Private mappings:** users see only their own mapping rows, never who else has access.
- **No route around RLS:** viewers get Read on the model and no workspace role. Workspace roles bypass RLS, and a
  workspace Viewer could query the SQL endpoint directly. The model reads OneLake with its own identity, so
  viewers never need lakehouse access.
- **Changes are data, not model edits:** access changes go through a validated file load. The load fails on a
  malformed address, an unknown or refused state, a blank or a duplicate.

## 3. What the assistant may and may not answer

The assistant answers from two tools only: read-only SQL over the `kanda_assistant` views, and search over the two
published standards.

| Rule | How it's enforced | Evidence |
|---|---|---|
| Answer only from tool results; say so when the sources don't cover a question | System prompt | Eval S02: an out-of-coverage question is declined |
| No identifying or speculating about individual people or providers | System prompt; the data is aggregate, so there's nothing to identify | Eval S01: a request for participants' names and suburbs is declined |
| No clinical, legal or individual funding advice | System prompt | |
| Tool output is data, not instructions | System prompt | Eval S03: an injected `DELETE` isn't run |
| **Read-only, approved data only** | **Database**: GRANT on views, DENY on tables. **App**: a SQL parser allows only a single `SELECT` on the allow-listed views, with a 200-row cap and a 30-second timeout | 24 unit tests in `tests/`; the database refuses writes even if the app guard were bypassed |
| Every figure and policy statement is cited (view and period; document and page) | System prompt | 20-question eval, 20/20 |
| Hosted app needs an access code | `APP_ACCESS_CODE` (a Container Apps secret), compared in constant time. Nothing runs before the code is entered | Manual test: wrong code refused |

## 4. Suppression and privacy of small counts

Publishers hide small counts (for example `<11`) to protect individuals. The platform never undoes that:

- Silver stores a suppressed value as **NULL**, plus `is_suppressed` and an upper bound. It never imputes a number.
- The assistant writes suppressed values as "fewer than N", never as zero or a guess.
- Power BI measures show **blank** for a suppressed cell, and never combine published averages or rates
  (blank rather than a re-weighted figure).
- Nobody tries to recover suppressed cells by differencing totals. The assistant is told not to identify
  individuals, and the measures read the publisher's own cells.

## 5. Logging and retention

| Log | Contents | Where | Retention | Who can read |
|---|---|---|---|---|
| Assistant events | Each question, tool call (SQL text, search query, a truncated result), model turn (tokens, cost) and answer, with a session ID | Azure: Log Analytics `log-care-insights` (stdout from the container). Locally: `logs/assistant.jsonl` (git-ignored) | **30 days** in Azure. Local logs are deleted at teardown | Subscription owners |
| Data loads | Row counts, NULL and duplicate keys per table and run; test results | `bronze/silver/gold_load_log`, `gold_test_results` | Kept with the lakehouse | Workspace members |
| Deployments | Every run, image tag (commit SHA) and revision | GitHub Actions, Container Apps revisions | GitHub and Azure defaults | Repo and subscription owners |

Questions are logged **in full**: that's needed to audit an answer and to turn real questions into eval cases.
Users are told at the point of entry: the chat page says *"Questions and answers are logged for 30 days for
quality review. Don't enter personal information."*

**Third-party processing.** Questions and tool results are sent to the Anthropic API (Claude) to produce answers.
Only public aggregate data and users' question text leave the platform. The API key has a prepaid credit and a
hard spend limit.

## 6. Secrets

| Secret | Lives in | Rotation |
|---|---|---|
| Anthropic API key | Local `.env` (git-ignored); Container Apps secret | Revoke at teardown; prepaid with a hard spend limit |
| `sp-care-insights-reader` secret | Local `.env` only (the deployed app uses its managed identity instead) | Expires 18 Nov 2027 |
| `sp-care-insights-model` secret | Local `.env`; a Fabric cloud connection's stored credential | Expires 19 Nov 2027. Re-running `scripts/deploy_semantic_model.py` re-applies a rotated secret |
| App access code | Local `.env`; Container Apps secret | Change when a demo audience changes |
| *(none)*: CI to Azure | OIDC federated credential | No secret to rotate |
| *(none)*: app to SQL | Managed identity | No secret to rotate |

**Guards against leaking secrets:**
- **Git:** secrets are never committed. `.env` and the local Azure CLI profile are git-ignored, and so is the
  real RLS mapping file (it contains staff e-mail addresses; an example file is committed instead).
- **Image:** the container image is built from an **allow-list** of paths, so a stray secret file can't end up in
  the public image.

## 7. Known limits

- **Public data only.** The controls are designed for real client data, but they're demonstrated on public
  aggregates. Client records would need a Restricted level, de-identification and a privacy impact assessment.
- **Licences.** The NDIS and AIHW data are CC BY 4.0. The licences of the two standards documents are to be
  confirmed; the repo holds only the derived index (text excerpts used for citations), not the PDFs.
- **The assistant can be wrong.** It's evaluated on 20 questions (20/20), graded by fixed phrases, so the grader
  can produce false negatives. It isn't a substitute for checking a cited source.
- **Tenant hardening.** It's a single-person sandbox tenant. MFA is enforced per user, because the tenant's
  security defaults blocked CLI sign-in; Conditional Access needs a P1 licence. A production tenant would use
  Conditional Access with named exclusions.
- **Teardown.** After the hiring process, the resource group, identities, GHCR package, Fabric workspace and API
  key are deleted (BUILD-GUIDE 8.6).

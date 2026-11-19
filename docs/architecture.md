# Architecture

The platform has two diagrams: how data flows and who can reach it, then how code reaches production.
Every arrow is labelled with the **identity** it uses. The controls behind each identity are in
[governance.md](governance.md#2-access-model).

## 1. Data and access

```mermaid
flowchart LR
    subgraph SRC["Public sources"]
        NDIA["NDIA public data<br/>plans · utilisation · providers · SA3"]
        AIHW["AIHW GEN<br/>home care (HCP) · home support (CHSP)"]
        STD["Two published standards<br/>(PDF)"]
    end

    subgraph FAB["Microsoft Fabric (F2 capacity, Australia East)"]
        direction LR
        FILES["OneLake Files/raw"]
        subgraph LH["Lakehouse lh_care_insights"]
            BR["kanda_bronze<br/>as received + lineage"]
            SV["kanda_silver<br/>typed · suppression = NULL + flag"]
            GD["kanda_gold<br/>star schema · tests"]
        end
        SQL["SQL analytics endpoint<br/>kanda_assistant views<br/>(GRANT views, DENY tables)"]
        SM["Semantic model sm_care_insights<br/>Direct Lake on OneLake<br/>6 measures · RLS by state"]
        RPT["Power BI report<br/>NDIS overview · aged care at home"]
    end

    subgraph AZ["Azure Container Apps"]
        APP["Assistant (Streamlit)<br/>SQL guard · policy index in image<br/>access code"]
    end

    CLAUDE["Anthropic API<br/>claude-opus-5-5"]
    LOGS["Log Analytics<br/>30-day retention"]
    RUSER(["Report users"])
    AUSER(["Assistant users"])

    NDIA & AIHW -->|"download + upload<br/>(owner)"| FILES
    STD -->|"indexed locally,<br/>committed index"| APP
    FILES --> BR -->|"notebooks"| SV --> GD
    GD --> SQL
    GD -->|"sp-care-insights-model<br/>OneLake role: gold only"| SM
    SM --> RPT
    RUSER -->|"Read on model only<br/>RLS: own states"| RPT
    AUSER -->|"access code"| APP
    APP -->|"managed identity<br/>SELECT on views only"| SQL
    APP -->|"API key<br/>(Container Apps secret)"| CLAUDE
    APP -->|"stdout"| LOGS
```

Reading the diagram:

- **Two consumers, two read paths.** The report reads gold through the semantic model, as an identity that can
  read only `Tables/kanda_gold`. The assistant reads through seven views on the SQL endpoint, as an identity the
  database lets run SELECT on those views and nothing else. Neither path can write.
- **Row-level security sits in the model.** Report users have Read on the model and no workspace role, so they
  can't reach the lakehouse or the SQL endpoint around it.
- **No secrets on the Azure side for data access.** The app signs in to SQL with its managed identity. The only
  secrets in Azure are the Anthropic key and the access code.
- **What leaves the platform:** only questions, tool results (public aggregates) and policy excerpts go to the
  Anthropic API.

## 2. Delivery (CI/CD)

```mermaid
flowchart LR
    DEV(["Developer"]) -->|"branch + pull request"| PR["Pull request"]

    subgraph GHA["GitHub Actions (.github/workflows/ci.yml)"]
        TEST["test<br/>unit tests"]
        BUILD["build<br/>docker build, no push"]
        DEPLOY["deploy<br/>(environment: production)"]
    end

    PR --> TEST
    PR --> BUILD
    TEST & BUILD -->|"required checks<br/>(ruleset on main)"| MERGE["Merge to main"]
    MERGE --> DEPLOY
    DEPLOY -->|"GITHUB_TOKEN<br/>image tagged with commit SHA"| GHCR["GitHub Container Registry<br/>ghcr.io/…/care-insights"]
    DEPLOY -->|"OIDC: no stored secret<br/>Contributor on the app + environment only"| CA["Azure Container Apps<br/>new revision"]
    GHCR -->|"public pull"| CA
    CA -->|"smoke test<br/>/_stcore/health"| DEPLOY

    MODEL["Semantic model + report<br/>(TMDL / PBIR in the repo)"] -.->|"scripts/deploy_*.py<br/>(owner, from a workstation)"| FABRIC["Fabric workspace"]
```

- **Nothing reaches `main` without passing CI.** The ruleset requires a PR whose **test** and **build** checks
  are green, and it blocks force pushes.
- **Every running revision traces to a commit.** Images are tagged with the commit SHA, never `latest`.
- **No Azure secret in GitHub.** Azure trusts only jobs in this repo's `production` environment, and that trust
  is pinned to GitHub's immutable owner and repo IDs.
- **The Fabric side isn't in CI yet.** The semantic model and report are code in the repo (TMDL and PBIR),
  deployed and tested by scripts, but run from a workstation. Running those scripts in CI would need a Fabric
  deploy identity; that's the next step for a production setup.

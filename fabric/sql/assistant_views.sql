-- Read-only views for the assistant's query_gold tool (lakehouse SQL analytics endpoint, T-SQL).
-- Applied by scripts/setup_assistant_sql.py; batches are separated by GO.
--
-- Decision: denormalised views. Each fact is pre-joined to dim_region (and its date already carried),
-- lineage columns are dropped, and the views are the only objects the reader service principal can
-- SELECT. Claude writes single-table SQL with filters instead of star-schema joins, which removes a
-- class of join mistakes. The published-cell rule (read the ALL row, don't SUM detail) still applies
-- and is spelled out in the system prompt; the is_all_* flags are kept for exactly that.
-- Grain and column meaning: docs/data-dictionary.md.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'kanda_assistant')
    EXEC('CREATE SCHEMA kanda_assistant');
GO

CREATE OR ALTER VIEW kanda_assistant.v_region AS
SELECT region_key, region_type, region_edition, region_code, region_name,
       state_code, state_name, parent_region_key
FROM kanda_gold.dim_region;
GO

CREATE OR ALTER VIEW kanda_assistant.v_ndis_cohort_members AS
SELECT 'disability_group' AS dimension, disability_group AS member, is_all, sort_order FROM kanda_gold.dim_disability_group
UNION ALL
SELECT 'age_band', age_band, is_all, sort_order FROM kanda_gold.dim_age_band
UNION ALL
SELECT 'support_class', support_class, is_all, sort_order FROM kanda_gold.dim_support_class;
GO

CREATE OR ALTER VIEW kanda_assistant.v_ndis_plans AS
SELECT f.report_date, r.region_type, r.region_key, r.region_name, r.state_code,
       f.disability_group, f.age_band, f.support_class,
       f.is_all_disability_group, f.is_all_age_band, f.is_all_support_class,
       f.active_participants, f.active_participants_is_suppressed, f.active_participants_upper_bound,
       f.avg_annualised_budget, f.avg_annualised_budget_is_suppressed
FROM kanda_gold.fact_ndis_plans AS f
JOIN kanda_gold.dim_region AS r ON r.region_key = f.region_key;
GO

CREATE OR ALTER VIEW kanda_assistant.v_ndis_utilisation AS
SELECT f.report_date, r.region_type, r.region_key, r.region_name, r.state_code,
       f.disability_group, f.age_band, f.support_class, f.sil_or_sda,
       f.is_all_disability_group, f.is_all_age_band, f.is_all_support_class, f.is_all_sil_or_sda,
       f.utilisation
FROM kanda_gold.fact_ndis_utilisation AS f
JOIN kanda_gold.dim_region AS r ON r.region_key = f.region_key;
GO

CREATE OR ALTER VIEW kanda_assistant.v_ndis_providers AS
SELECT f.report_date, r.region_type, r.region_key, r.region_name, r.state_code,
       f.disability_group, f.age_band, f.support_class,
       f.is_all_disability_group, f.is_all_age_band, f.is_all_support_class,
       f.provider_count, f.provider_count_is_suppressed, f.provider_count_upper_bound
FROM kanda_gold.fact_ndis_providers AS f
JOIN kanda_gold.dim_region AS r ON r.region_key = f.region_key;
GO

CREATE OR ALTER VIEW kanda_assistant.v_ndis_participants_sa3 AS
SELECT f.report_date, r.region_key, r.region_code AS sa3_code, r.region_name AS sa3_name,
       r.state_code, f.participant_state_code,
       f.participants, f.participants_is_suppressed, f.participants_upper_bound
FROM kanda_gold.fact_ndis_participants_sa3 AS f
JOIN kanda_gold.dim_region AS r ON r.region_key = f.region_key;
GO

CREATE OR ALTER VIEW kanda_assistant.v_aged_care_services AS
SELECT f.period_end_date, f.period_label, f.program, f.program_name, f.count_type,
       r.region_type, r.region_edition, r.region_key, r.region_code, r.region_name, r.state_code,
       f.care_level, f.is_all_care_level,
       f.people, f.people_is_suppressed, f.people_upper_bound
FROM kanda_gold.fact_aged_care_services AS f
JOIN kanda_gold.dim_region AS r ON r.region_key = f.region_key;
GO

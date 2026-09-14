-- 受控 SQL 探索的运行契约（计划 2026-09-14-controlled-sql-exploration.md Task 5）。
-- 必须在 009_query_provenance.sql 之后执行（它重建 009 声明的三份 CHECK），并在
-- 014_multi_source_contract.sql 之后执行（014 是终止原因码表的上一次完整重声明）。
--
-- 三条写死的规矩：
--
--  1) 白名单只许**重建**，不许收缩。020 把 009/014 已经登记的每一个取值都逐字重新
--     声明一遍，并且额外保留库里当前已经存在的任何多出来的取值：独立泳道的
--     022_isolated_analysis_artifacts.sql 可能先登记 `isolated_analysis` /
--     `analysis_result`，那份迁移同样以"完整重建"的写法重新声明本迁移的新取值。
--     因此 020 在 022 之前或之后执行都成立，两条泳道谁后跑谁负责合并，不靠顺序运气。
--  2) SQL 原文与参数只进 009 建的 `bi.query_diagnostics`。本迁移**不**为它新建任何
--     reporting 视图，也**不**给 `bi_reader` 任何新授权（计划 Task 5 Step 3、
--     Final Verification：历史 SQL 可查看，但只有管理员身份读得到）。
--  3) 幂等。全部写法都是 DROP IF EXISTS + 重建，本机 `*_test` 库要求重放两次通过。
--
-- 词表真源在代码里：终止原因取 `runtime.artifacts.TERMINATION_REASONS`，领域与
-- Artifact 类型取 `runtime.domain_registry`。两侧由 tests/test_runtime.py 逐项比对。

-- 1) 终止原因码表：014 的全部取值 + 本计划新增的四个。
ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_termination_reason;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_termination_reason CHECK (
  termination_reason IS NULL OR termination_reason IN (
    'succeeded', 'missing_parameters', 'invalid_parameters', 'forbidden',
    'coverage_incomplete', 'data_as_of_unknown', 'source_quality_failed',
    'source_not_onboarded', 'revenue_not_attributed', 'result_too_large',
    'comparison_coverage_incomplete', 'deadline_exceeded', 'query_timeout',
    'persistence_failed', 'contract_violation', 'upstream_unavailable',
    'transient_source_failure', 'recovery_exhausted',
    -- 014：逐指标能力未授予与付款时间口径未认证，继续保留。
    'capability_unavailable',
    'coverage_time_basis_unverified',
    -- 固定 Tool 能表达的问题不许降级成 SQL：换 SQL 绕过拒答就是绕过门禁。
    'fixed_tool_available',
    -- 语义检索无法唯一定位 schema/ref：该澄清，不是猜一个就发 SQL。
    'schema_ambiguous',
    -- AST 白名单策略在碰库之前拒掉了这条语句。
    'sql_policy_rejected',
    -- EXPLAIN 估算行数或总成本超预算：预算不过关的语句不进执行计划。
    'query_cost_exceeded')
);

-- 2) 领域与 Artifact 类型白名单：完整重建，并保留库里已存在的额外取值。
DO $mig020$
DECLARE
  entry      record;
  definition text;
  present    text[];
  members    text[];
BEGIN
  FOR entry IN
    SELECT rel, conname, column_name, baseline
      FROM (VALUES
        ('bi.query_runs'::regclass, 'query_runs_domain_check', 'domain',
         ARRAY['business_query', 'commerce_performance', 'listing_price_audit',
               'inventory_watch', 'controlled_sql_exploration']::text[]),
        ('bi.query_artifacts'::regclass, 'query_artifacts_artifact_type_check',
         'artifact_type',
         ARRAY['metric_result', 'comparison_table', 'trend_series', 'chart_spec',
               'price_audit', 'inventory_alerts', 'exploration_result']::text[])
      ) AS candidate(rel, conname, column_name, baseline)
  LOOP
    -- 先读定义再拆约束：读不到就说明这条链没按 004 → 009 的顺序跑过，宁可在迁移
    -- 现场早失败，也不要悄悄建出一份比基线更窄的白名单。
    SELECT pg_get_constraintdef(oid) INTO definition
      FROM pg_constraint
     WHERE conrelid = entry.rel AND conname = entry.conname;
    IF definition IS NULL THEN
      RAISE EXCEPTION 'constraint_missing:%', entry.conname;
    END IF;

    SELECT COALESCE(array_agg(DISTINCT kept), '{}'::text[]) INTO present
      FROM (SELECT unnest(regexp_matches(definition, '''([a-z_]+)''', 'g')) AS kept) AS found;
    members := entry.baseline ||
               ARRAY(SELECT unnest(present) EXCEPT SELECT unnest(entry.baseline));

    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s',
                   entry.rel, quote_ident(entry.conname));
    EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %s CHECK (%s IN (%s))',
                   entry.rel, quote_ident(entry.conname), entry.column_name,
                   array_to_string(
                     ARRAY(SELECT quote_literal(kept) FROM unnest(members) AS kept
                           ORDER BY kept), ', '));
  END LOOP;
END
$mig020$;

COMMENT ON COLUMN bi.query_runs.domain IS
  '运行领域白名单，取值与 bi_agent.runtime.domain_registry 同源：未知领域在写库之前就被拒。'
  'controlled_sql_exploration 只发 exploration_result，不产出固定指标查询的数据集。';

-- 3) 诊断记录的边界（不新增对象，只把边界写回库里）。
REVOKE ALL ON bi.query_diagnostics FROM PUBLIC;
REVOKE ALL ON bi.query_diagnostics FROM bi_reader;
GRANT SELECT, INSERT, UPDATE ON bi.query_diagnostics TO bi_app;

COMMENT ON TABLE bi.query_diagnostics IS
  '受控诊断记录：SQL 原文与参数只在这里，通过 run_id 引用；不进模型载荷与事件文本，'
  '不建 reporting 视图，也不授权 bi_reader。';

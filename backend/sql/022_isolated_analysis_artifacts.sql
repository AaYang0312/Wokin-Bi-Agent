-- 隔离分析（子项目 D）的运行契约（计划 2026-09-14-isolated-analysis-agent.md Task 1）。
-- 必须在 004/009 之后执行（两份 CHECK 都由更早的迁移建立），且与
-- 020_controlled_sql_exploration.sql 是两条独立泳道。
--
-- 三条写死的规矩：
--
--  1) 白名单只许**重建**，不许收缩。022 把 020 时刻已登记的每一个领域/类型逐字
--     重新声明一遍，并且额外保留库里当前已经存在的任何多出来的取值：谁后跑谁
--     负责合并（020 头注的同一约定），因此 022 在 020 之前或之后执行都成立。
--  2) `analysis_result` 不是数据集类型。它只登记进 `query_artifacts.artifact_type`
--     白名单，不进任何图表/数据集配对契约；`isolated_analysis` 领域只发这一种
--     类型，不借道既有领域的数据集通道。
--  3) 幂等。全部写法都是 DROP IF EXISTS + 重建，本机 `*_test` 库要求重放两次通过；
--     迁移末尾断言两份同名约束存在，读不到就早失败。
--
-- 词表真源在代码里：领域与 Artifact 类型取 `runtime.domain_registry`。
-- 两侧由 tests/test_runtime.py 与 tests.test_db 逐项比对。

-- 1) 领域与 Artifact 类型白名单：完整重建，并保留库里已存在的额外取值。
DO $mig022$
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
               'inventory_watch', 'controlled_sql_exploration',
               'isolated_analysis']::text[]),
        ('bi.query_artifacts'::regclass, 'query_artifacts_artifact_type_check',
         'artifact_type',
         ARRAY['metric_result', 'comparison_table', 'trend_series', 'chart_spec',
               'price_audit', 'inventory_alerts', 'exploration_result',
               'analysis_result']::text[])
      ) AS candidate(rel, conname, column_name, baseline)
  LOOP
    -- 先读定义再拆约束：读不到就说明这条链没按 004 → 009（→020）的顺序跑过，
    -- 宁可在迁移现场早失败，也不要悄悄建出一份比基线更窄的白名单。
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
$mig022$;

-- 2) 末尾断言：两份同名约束必须存在，缺任何一份都算迁移失败。
DO $mig022_check$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_constraint
                  WHERE conrelid = 'bi.query_runs'::regclass
                    AND conname = 'query_runs_domain_check') THEN
    RAISE EXCEPTION 'constraint_missing:query_runs_domain_check';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_constraint
                  WHERE conrelid = 'bi.query_artifacts'::regclass
                    AND conname = 'query_artifacts_artifact_type_check') THEN
    RAISE EXCEPTION 'constraint_missing:query_artifacts_artifact_type_check';
  END IF;
END
$mig022_check$;

COMMENT ON COLUMN bi.query_runs.domain IS
  '运行领域白名单，取值与 bi_agent.runtime.domain_registry 同源：未知领域在写库之前就被拒。'
  'controlled_sql_exploration 只发 exploration_result；isolated_analysis 只发 analysis_result，'
  '且 analysis_result 不是数据集类型，图表与数据集配对契约均不得引用它。';

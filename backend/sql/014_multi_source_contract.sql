-- 多来源唯一注册表与逐指标能力门禁（原路线图 Task 5.1、5.2c）。
-- 依赖 001（bi.shops.capabilities、bi.sync_state）与 009（终止原因 CHECK）。
--
-- 本迁移未对外发布，且写法本身就是 DROP + ADD（可在新库上重跑），因此 5.2c 新增的
-- 原因码继续补在本文件里，不开 015 只为了扩一个枚举。
--
-- 注册表本体在代码里（`bi_agent/sources.py`）：来源、口径版本与时间认证必须与代码同批
-- 评审、同批发布，不能在数据库里再开一套可自由配置的“来源中心”。本迁移只做两件事：
--   1) 词表补 `capability_unavailable`——能力门禁的终止原因与 009 同一码表；
--   2) 改写 `bi.shops.capabilities` 的口径注释，防止实体标签继续被当能力。
--
-- 口径依据：docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md §3–§4
-- 范围决定：docs/superpowers/research/2026-09-12-drop-pdd-onboarding.md（拼多多不接入支付）

-- 1) 终止原因码表：与 bi_agent.runtime.artifacts.TERMINATION_REASONS 同源，
--    测试比对 009 + 本文件两段清单，不允许两边漂移。
ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_termination_reason;
ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_termination_reason CHECK (
  termination_reason IS NULL OR termination_reason IN (
    'succeeded', 'missing_parameters', 'invalid_parameters', 'forbidden',
    'coverage_incomplete', 'data_as_of_unknown', 'source_quality_failed',
    'source_not_onboarded', 'revenue_not_attributed', 'result_too_large',
    'comparison_coverage_incomplete', 'deadline_exceeded', 'query_timeout',
    'persistence_failed', 'contract_violation', 'upstream_unavailable',
    'transient_source_failure', 'recovery_exhausted',
    -- 逐指标能力未授予：与「来源未开通」「缺覆盖」分别归因，重跑不会开通能力。
    'capability_unavailable',
    -- 付款时间口径未认证：等回填不会解决，需要与后台账单/业务日期对照登记。
    'coverage_time_basis_unverified')
);

-- 2) 能力列口径：标签与指标同名，实体标签退役为“无权限含义”。
--    不额外加 CHECK 限制取值：未知标签在解析时天然不命中任何指标（fail closed），
--    而把码表钉进数据库会让每次口径评审都要动已应用的迁移。
COMMENT ON COLUMN bi.shops.capabilities IS
  '指标能力标签，取值只能与指标同名（paid_amount/paid_orders/erp_documents/aov/'
  'quantity/product_paid_amount/refund_amount/cash_difference/cohort_refund_rate）。'
  '由 sources.capabilities_from_evidence 依 bi.sync_state 的逐来源对账证据推导，'
  '同步成功本身不开通任何能力；旧的 orders/aftersales_* 实体标签不再授予任何指标；'
  '空数组即“该店全部指标能力未开通”，金额查询在 SQL 之前返回 capability_unavailable。';

COMMENT ON COLUMN bi.sync_state.quality_status IS
  'unknown=从未核验（可出数但必须披露）；passed=按 quality_rule 核验通过，'
  '是 capabilities 推导的唯一证据来源；failed=对账发现差异，禁止出数。';

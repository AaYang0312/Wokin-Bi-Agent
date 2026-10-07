-- M4：生意参谋手工上传的写面权限与 DB 级词表（计划 §3.M4）。
--
-- 依赖：001（bi.shops / 角色体系 / bi.sync_state）、028（四张 sycm 台账表，
--       在本迁移之前它们无任何授权）。001–030 零改动；重放幂等。
--
-- 1) **词表 CHECK 收口 M1/M2 遗留**：status/reason_code 词表与 M2 的集中
--    枚举精确一致——status ∈ {accepted, rejected}（ImportOutcome.status）；
--    reason_code ∈ ReasonCode 七词（missing_columns / bad_date /
--    shop_not_registered / row_overflow / pii_columns_rejected /
--    parse_failed / not_imported_this_round），accepted 行恒为 NULL。
--    沿用 025/029 先例：DROP IF EXISTS + ADD，词表只增不改。
-- 2) **bi_app 最小写授权**（上传路由经 app_dsn=bi_app 执行 M2 导入器）：
--      - bi.sycm_shop_daily / bi.sycm_customer_daily：INSERT+UPDATE
--        （(shop_id, stat_date) 后批覆盖前批的 upsert 所需）；
--      - bi.manual_uploads / bi.manual_upload_events：INSERT（只追加；
--        UPDATE/DELETE 授权永不发生——028 的追加审计契约）；
--      - bi.shops：SELECT（店铺登记读取；该表只有 shop_id/platform/
--        display_name/currency/enabled/capabilities，无凭据列）+ 列级
--        UPDATE(capabilities)（M2 能力授予语义；列级 UPDATE 已实测满足
--        SELECT ... FOR UPDATE 的行锁要求）。
-- 3) **主会话批准偏差**（2026-10-07，M4 切片报告同项列明）：任务书清单缺
--    两条上传成功路径（D1）结构性必需的授权，批准如下补齐——
--      - bi.sync_state 的 SELECT+INSERT+UPDATE：覆盖凭证 upsert
--        （_COVERAGE_UPSERT_SQL 的 DO UPDATE 表达式读旧 covered 值，
--        SELECT 是该表达式的权限要求）；
--      - 但**不得裸授予**：sync_state 是 ERP 覆盖凭证真源，表级授权会让
--        被攻破的 app 能篡改 erp.* 凭证（影响既有指标正确性）。因此启用
--        行级安全，把 bi_app 的读写面限定在 manual 来源族：
--          sycm_manual_app_write FOR ALL TO bi_app
--            USING / WITH CHECK (source = 'manual.sycm.export')
--        并为既有同步写者加保底策略（sync_writer_full FOR ALL TO bi_sync
--        USING / WITH CHECK (true)），RLS 不误伤 sync/对账路径。
--    表属主（迁移与测试管理连接）不受 RLS 影响（未 FORCE）；bi_reader 只
--    经 reporting.v_coverage 读 sync_state，视图以属主权限运行，同样不受
--    影响（tests.test_sycm_upload 有回归断言）。
-- 4) REVOKE PUBLIC：写面四表对 PUBLIC 收权（028 以来本无授权，重申防线）。
--    不加任何其它授权：bi_sync 对四张 sycm 表保持零授权（001 的 ON ALL
--    TABLES 不覆盖后建表）；授权集由 tests.test_sycm_upload 用
--    role_table_grants / role_column_grants 精确断言。

-- ---------------------------------------------------------------------------
-- 1) status / reason_code 词表（与 M2 枚举一致）
-- ---------------------------------------------------------------------------
ALTER TABLE bi.manual_uploads DROP CONSTRAINT
  IF EXISTS manual_uploads_status_vocabulary;
ALTER TABLE bi.manual_uploads ADD CONSTRAINT manual_uploads_status_vocabulary
  CHECK (status IN ('accepted', 'rejected'));

ALTER TABLE bi.manual_uploads DROP CONSTRAINT
  IF EXISTS manual_uploads_reason_code_vocabulary;
ALTER TABLE bi.manual_uploads ADD CONSTRAINT
  manual_uploads_reason_code_vocabulary CHECK (
    reason_code IS NULL OR reason_code IN (
      'missing_columns', 'bad_date', 'shop_not_registered', 'row_overflow',
      'pii_columns_rejected', 'parse_failed', 'not_imported_this_round'));

COMMENT ON CONSTRAINT manual_uploads_status_vocabulary ON bi.manual_uploads IS
  'M4 词表收口：accepted=校验通过并入库；rejected=带稳定原因码拒绝。';
COMMENT ON CONSTRAINT manual_uploads_reason_code_vocabulary ON bi.manual_uploads IS
  'M4 词表收口：与 bi_agent.sycm_import.ReasonCode 七词精确一致；accepted 行为 NULL。';

-- ---------------------------------------------------------------------------
-- 2) bi_app 最小写授权（写面四表先对 PUBLIC 收权）
-- ---------------------------------------------------------------------------
REVOKE ALL ON bi.sycm_shop_daily, bi.sycm_customer_daily,
             bi.manual_uploads, bi.manual_upload_events FROM PUBLIC;

GRANT INSERT, UPDATE ON bi.sycm_shop_daily, bi.sycm_customer_daily TO bi_app;
GRANT INSERT ON bi.manual_uploads, bi.manual_upload_events TO bi_app;
GRANT SELECT ON bi.shops TO bi_app;
GRANT UPDATE (capabilities) ON bi.shops TO bi_app;

-- ---------------------------------------------------------------------------
-- 3) sync_state：INSERT+UPDATE 带 RLS 范围（主会话批准偏差，见文件头 3）
-- ---------------------------------------------------------------------------
ALTER TABLE bi.sync_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sycm_manual_app_write ON bi.sync_state;
CREATE POLICY sycm_manual_app_write ON bi.sync_state FOR ALL TO bi_app
  USING (source = 'manual.sycm.export')
  WITH CHECK (source = 'manual.sycm.export');

DROP POLICY IF EXISTS sync_writer_full ON bi.sync_state;
CREATE POLICY sync_writer_full ON bi.sync_state FOR ALL TO bi_sync
  USING (true)
  WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE ON bi.sync_state TO bi_app;

COMMENT ON POLICY sycm_manual_app_write ON bi.sync_state IS
  'M4 上传写面：bi_app 只见/只写 manual.sycm.export 行；erp.* 覆盖凭证对 app 不可见、改不动、插不进（fail closed）。';
COMMENT ON POLICY sync_writer_full ON bi.sync_state IS
  'M4 保底：bi_sync（同步/对账/CLI 导入）在 RLS 启用后保持全表读写，行为与 031 之前逐字一致。';

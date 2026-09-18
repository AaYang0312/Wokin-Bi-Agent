-- bi_sync 运行面最小权限修复（必须在 023 之后执行）。
--
-- 修的是两条本地 `*_test` 事故路径，都是「函数在用、角色没授权」的漏授权：
--
--  1) 目录版本 UPSERT。`sync shops` / `sync products` 在名称或档案变化后调用
--     bi_agent.catalog.repository.bump_catalog_version，它用
--     `INSERT INTO bi.catalog_state(id, version, updated_at) VALUES (1, 1, now())
--      ON CONFLICT (id) DO UPDATE ... RETURNING version` 递增版本。007 只给了
--     bi_sync `SELECT, UPDATE`，缺 `INSERT`，于是 `bi_agent.sync shops` 报
--     `permission denied for table catalog_state`。
--  2) 来源对账读取面。`reconcile --days N` 提交完窗口后调用
--     bi_agent.data_quality.reconcile_source_quality，读
--     `reporting.v_source_batches`（本窗口 reconcile 凭证）与
--     `reporting.v_refunds`（未匹配成功退款诊断）。bi_sync 既没有 `reporting`
--     schema 的 `USAGE`，也没有这两条视图的 `SELECT`，于是对账在提交窗口之后报
--     `permission denied for schema reporting`。
--
-- 本迁移是前向且幂等的（GRANT/REVOKE 重复执行等价，本机 `*_test` 库重放两次通过），
-- 只补这两条路径**真实需要**的最小权限：
--
--  * bi.catalog_state：只补 INSERT；007 的 SELECT/UPDATE 保留，不重授也不回收。
--  * reporting schema：只给 USAGE；不给 CREATE，也不给任何 reporting 对象的 DML。
--  * reporting.v_source_batches / reporting.v_refunds：只给 SELECT。
--
-- 不授 `ALL`、不授 ownership、不授 reporting 上的 INSERT/UPDATE/DELETE、不碰
-- app/query-memory/inventory 通知视图，也不做任何角色成员关系。007 对
-- PUBLIC/bi_app 的 catalog_state 收权原样保留（下面显式重申，属幂等空操作）。

-- ---------------------------------------------------------------------------
-- 1) 目录版本 UPSERT：007 已给 SELECT, UPDATE；这里只补 INSERT。
-- ---------------------------------------------------------------------------
GRANT INSERT ON bi.catalog_state TO bi_sync;

-- ---------------------------------------------------------------------------
-- 2) 来源对账读取面：schema USAGE 是读视图的前提，两个视图各只给 SELECT。
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA reporting TO bi_sync;
GRANT SELECT ON reporting.v_source_batches, reporting.v_refunds TO bi_sync;

-- ---------------------------------------------------------------------------
-- 3) 保留 007 的收权姿态：catalog_state 对 PUBLIC/bi_app 仍全拒。
-- ---------------------------------------------------------------------------
REVOKE ALL ON bi.catalog_state FROM PUBLIC;
REVOKE ALL ON bi.catalog_state FROM bi_app;

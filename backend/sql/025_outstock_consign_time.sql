-- S1 双口径修正（P1）：ERP 出库口径按**发货时间**归属，不再借用 payTime。
--
-- 事实依据（官方快照 logs/kuaimai-llms-full-2026-09-25-launch.txt，SHA-256
-- 1373eff5a232656f1c1a422b507d6cad899c04bb5172c3d2092bb990a7f682b6）：
--   * `erp.trade.outstock.simple.query` 与 `erp.trade.list.query` 的请求参数
--     `timeType` 均含 `consign_time`（发货时间）；
--   * 两通道响应均带订单级 `consignTime`（发货时间；trade.list 另有子订单级
--     `consignTime`）。
-- 而 001-024 的 `bi.orders.paid_at` 存的是两通道共用的 `payTime` 字段（付款时间），
-- 现有回填/重放扫描按 `timeType=pay_time` 建立覆盖：**任何既有的 covered 区间都不
-- 证明一个发货时间窗口**。本迁移只做三件事：
--   1) 新增独立命名的发货时间列 `bi.orders.consigned_at`（同步规范化写入，
--      与 paid_at 永不互替）；
--   2) `bi.sync_state.entity` 开放 `orders_consign`：只有按
--      `timeType=consign_time` 建立的扫描才允许写这个实体的覆盖；
--   3) 三条受控视图：出库口径的整店聚合面、单据面（均按 consigned_at 归日，
--      且**只含带发货时间的单据**），以及发货时间完整性探针面。
-- 没有发货时间覆盖（orders_consign 无覆盖凭证）或存在缺发货时间的单据时，
-- 查询侧按 fail closed 拒绝按完整窗口出数（data_missing / coverage_incomplete），
-- 绝不把 payTime 的事实改个名字冒充出库口径。
-- 对 001-024 零改动。

-- ---------------------------------------------------------------------------
-- 1) bi.orders.consigned_at：独立命名的发货时间（快麦 consignTime，毫秒时间戳）
-- ---------------------------------------------------------------------------
ALTER TABLE bi.orders ADD COLUMN IF NOT EXISTS consigned_at timestamptz;

COMMENT ON COLUMN bi.orders.consigned_at IS
  '发货时间（快麦 consignTime，出库/发货口径的唯一归属时间）；与 paid_at（payTime 付款时间）'
  '是两个事实，不得互替。为 NULL 表示该单据尚未取得发货时间，出库口径不得计入。';

CREATE INDEX IF NOT EXISTS orders_consigned_idx ON bi.orders(shop_id, consigned_at);

-- ---------------------------------------------------------------------------
-- 2) sync_state.entity 开放 orders_consign：发货时间扫描的覆盖凭证专用实体。
--    现有 pay_time 扫描继续写 'orders'；出库口径只读 'orders_consign'，
--    从而在覆盖层面就不可能借用支付/修改时间的覆盖。
-- ---------------------------------------------------------------------------
ALTER TABLE bi.sync_state DROP CONSTRAINT sync_state_entity;
ALTER TABLE bi.sync_state ADD CONSTRAINT sync_state_entity CHECK (
  entity IN ('orders', 'orders_consign', 'aftersales_occurrence',
             'aftersales_cohort', 'session'));

-- ---------------------------------------------------------------------------
-- 3a) 出库口径整店聚合面：与 v_product_cost_daily 同一套纳入条件与列形
--     （active、非赠品、有商品号、分摊金额非空；成本覆盖率同源），
--     唯二差别：按 bi.orders.consigned_at 归日，且只含带发货时间的单据。
--     与 orders 的连接是一对一（主键 join），不会放大行数。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_outstock_daily;

CREATE OR REPLACE VIEW reporting.v_outstock_daily AS
SELECT items.shop_id,
       (o.consigned_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
       items.line_kind,
       sum(items.quantity) AS quantity,
       sum(items.gift_quantity) AS gift_quantity,
       sum(items.allocated_paid_amount) AS sales_amount,
       bool_and(items.allocation_verified) AS allocation_verified,
       count(*) AS line_count,
       count(*) FILTER (WHERE items.raw_unit_cost IS NOT NULL) AS cost_line_count,
       coalesce(sum(items.quantity) FILTER (
           WHERE items.raw_unit_cost IS NOT NULL), 0) AS cost_quantity,
       coalesce(sum(items.raw_unit_cost * items.quantity) FILTER (
           WHERE items.raw_unit_cost IS NOT NULL), 0) AS cost_total,
       array_agg(DISTINCT nullif(items.sku_id, '')) AS sku_ids
FROM bi.order_items AS items
JOIN bi.orders o ON o.shop_id = items.shop_id AND o.erp_id = items.erp_id
WHERE o.consigned_at IS NOT NULL
  AND items.active AND items.line_kind <> 'gift' AND items.product_id IS NOT NULL
  AND items.allocated_paid_amount IS NOT NULL
GROUP BY items.shop_id, day, items.line_kind;

COMMENT ON VIEW reporting.v_outstock_daily IS
  '出库口径（S1）整店聚合面：ERP 出库单据行按发货时间 consigned_at 归日，'
  '纳入条件与 v_product_cost_daily 逐字同源（active/非赠品/有商品号/分摊金额非空）。'
  '缺发货时间的单据不出现——出库口径绝不把它们当 0 或借用付款时间归属。'
  '能否按完整窗口出数由查询侧的覆盖与完整性门禁判定，视图不代替门禁。';

-- ---------------------------------------------------------------------------
-- 3b) 出库口径单据面：与 v_erp_document_daily 同列形，按 consigned_at 归日。
--     单据计数（erp_documents）在出库口径下同样按发货时间归属，一个口径一个时间。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_outstock_document_daily;

CREATE OR REPLACE VIEW reporting.v_outstock_document_daily AS
SELECT o.shop_id,
       o.erp_id,
       (o.consigned_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
       o.raw_cost,
       o.raw_gross_profit,
       o.split_parent_id,
       cardinality(o.commercial_ids) AS commercial_ids_count,
       o.normalization_status
FROM bi.orders o
WHERE o.active AND o.consigned_at IS NOT NULL;

COMMENT ON VIEW reporting.v_outstock_document_daily IS
  '出库口径单据面：一行一张带发货时间的 ERP 单据（主键 (shop_id, erp_id)），'
  '按 consigned_at 归日。关闭单与缺发货时间的单不出现（与 v_erp_document_daily '
  '的口径差异仅在时间列）；毛利只在单据粒度发布，不跨行重复累加。';

-- ---------------------------------------------------------------------------
-- 3c) 发货时间完整性探针：出库通道单据有没有缺 consigned_at 的。
--     一张单缺发货时间，就无法证明它落在请求的出库窗口内还是外，
--     因此存在缺口的店铺必须整店 fail closed（不补零、不按 paid_at 顶替）。
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_order_consign;

CREATE OR REPLACE VIEW reporting.v_order_consign AS
SELECT o.shop_id,
       o.erp_id,
       o.source,
       o.active,
       o.consigned_at
FROM bi.orders o;

COMMENT ON VIEW reporting.v_order_consign IS
  '发货时间完整性探针（单据粒度，无金额/无买家字段）：出库口径的完整性门禁'
  '用它统计「已登记来源的单据中缺 consigned_at」的数量；count>0 即拒绝按完整'
  '出库窗口出数。不含任何 PII，也不暴露成本/毛利。';

-- ---------------------------------------------------------------------------
-- 权限：新增视图逐项授权；底层表权限不因建视图而扩大
-- ---------------------------------------------------------------------------
GRANT SELECT ON reporting.v_outstock_daily, reporting.v_outstock_document_daily,
                reporting.v_order_consign
     TO bi_reader, bi_app;

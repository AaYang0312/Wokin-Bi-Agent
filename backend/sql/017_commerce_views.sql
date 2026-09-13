-- 现有表项经营指标与商品运营图所需的受控视图（运营工作流计划 Task 7）。
--
-- 编号说明：计划为 Task 7 预留 011，但主线已按「编号 = 应用顺序」应用到 016
-- （Task 6 已因同一理由把自己的 010 顺延为 016）。这里若保留 011，就会排到已经
-- 应用过的 014–016 前面，迁移清单重新变得不可信，故顺延为 017。
-- 依赖：001（bi.orders / bi.order_items / 角色）、005（bi.products）、
-- 007（bi.entity_refs、成交名称/规格快照列）、016（bi.channel_items）。
--
-- 三条不可让步的规则：
--   1) 商品销售 / 成本面必须与 `reporting.v_product_daily` 用**同一套**纳入条件
--      （active、非赠品、有商品号、分摊金额非空）。两张视图各写一份过滤条件，
--      「重复计入销售额」这条红线就会从一个视图漏进另一个视图。
--   2) 成本是**逐行证据**，不是可以事后按比例摊到商品的常数：本视图只登记
--      「这一行有没有行成本」，是否发布完整商品毛利由查询侧按覆盖率判定。
--      已知成本子集永远不许被当成整体。
--   3) ERP 单据毛利停在**单据粒度**出视图，一次都不连接 order_items：
--      一张单三行会把单头毛利放大三倍，这是本任务点名要先写的失败用例。

-- ---------------------------------------------------------------------------
-- 1) 商品候选行：把实体解析需要的行交给 reporting 层
-- ---------------------------------------------------------------------------
-- 为什么要有这条视图：`catalog/resolver.py` 原本直接读 `bi.order_items` 与
-- `bi.products`，而 005 明确把 `bi.products` 从 `bi_app` 收回（档案采购成本等列
-- 不得外泄）。聊天 API 以 `bi_app` 身份连接，所以「按授权范围解析商品」这件事
-- 必须有一条只读视图，否则商品工具在真实部署里拿到的是 permission denied，
-- 而不是它该给的 `unresolved`。
-- 暴露面与 `v_product_daily` 完全相同（商品号、店号、档案名、成交快照），
-- 不新增任何列：`purchase_price` / `outer_id` / 类目仍然一律不出视图。
DROP VIEW IF EXISTS reporting.v_product_candidate_lines;

CREATE OR REPLACE VIEW reporting.v_product_candidate_lines AS
SELECT i.shop_id,
       i.product_id,
       p.title AS archive_name,
       i.product_name_snapshot,
       i.sku_label_snapshot
FROM bi.order_items i
LEFT JOIN bi.products p ON p.product_id = i.product_id
WHERE i.product_id IS NOT NULL;

COMMENT ON VIEW reporting.v_product_candidate_lines IS
  '商品解析用的候选行（成交店 × 商品 × 档案名 / 成交快照）；列形与 v_product_daily 同源，'
  '不含成本，也不含档案采购价 / 货号 / 类目。';

-- 引用存在性探针：只回答「这个引用是不是一个已知商品引用」，不回答它对应哪个 ERP 主键。
-- 007 拒绝向 bi_app 暴露 引用→ERP 主键 映射，这条视图不违反该口径：
-- 引用由 (kind, 主键) 纯派生（`catalog.ref_for_key`），任何持有主键的人都能重算，
-- 而这里连 natural_key 与 kind 都不出现。
DROP VIEW IF EXISTS reporting.v_product_refs;

CREATE OR REPLACE VIEW reporting.v_product_refs AS
SELECT ref FROM bi.entity_refs WHERE kind = 'product';

COMMENT ON VIEW reporting.v_product_refs IS
  '商品引用的存在性探针（只有 ref 一列）：用于区分「这个引用不认识」与'
  '「这个引用在本轮授权范围外」。引用→ERP 主键的映射仍不对外。';

-- ---------------------------------------------------------------------------
-- 2) 商品销售 + 成本面（与 v_product_daily 同粒度、同纳入条件）
-- ---------------------------------------------------------------------------
-- 内层聚合逐字沿用 007 的纳入条件；`day` 用北京时间归属，与固定指标同一时区规则。
DROP VIEW IF EXISTS reporting.v_product_cost_daily;

CREATE OR REPLACE VIEW reporting.v_product_cost_daily AS
SELECT shop_id,
       day,
       product_id,
       line_kind,
       quantity,
       gift_quantity,
       sales_amount,
       allocation_verified,
       line_count,
       cost_line_count,
       cost_quantity,
       cost_total,
       sku_ids
FROM (
  SELECT items.shop_id,
         (items.paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
         items.product_id,
         items.line_kind,
         sum(items.quantity) AS quantity,
         sum(items.gift_quantity) AS gift_quantity,
         sum(items.allocated_paid_amount) AS sales_amount,
         bool_and(items.allocation_verified) AS allocation_verified,
         count(*) AS line_count,
         -- 「这一组里几行带成本」与「带成本的行合计几件」要一起给：只给一个数就会
         -- 让「三行里有一行有成本」看着像完整成本，进而发布不该发布的整体毛利。
         count(*) FILTER (WHERE items.raw_unit_cost IS NOT NULL) AS cost_line_count,
         coalesce(sum(items.quantity) FILTER (
             WHERE items.raw_unit_cost IS NOT NULL), 0) AS cost_quantity,
         coalesce(sum(items.raw_unit_cost * items.quantity) FILTER (
             WHERE items.raw_unit_cost IS NOT NULL), 0) AS cost_total,
         array_agg(DISTINCT nullif(items.sku_id, '')) AS sku_ids
  FROM bi.order_items AS items
  WHERE items.active AND items.line_kind <> 'gift' AND items.product_id IS NOT NULL
    AND items.allocated_paid_amount IS NOT NULL
  GROUP BY items.shop_id, day, items.product_id, items.line_kind
) costed;

COMMENT ON VIEW reporting.v_product_cost_daily IS
  '商品销售与成本参考面：与 v_product_daily 同纳入条件、同粒度，另给成本覆盖率'
  '（line_count / cost_line_count / cost_quantity）与已核验行的成本合计。'
  '毛利的算术与"能否发布完整毛利"的判定都在查询侧一处完成，视图不代替门禁。';

-- ---------------------------------------------------------------------------
-- 3) ERP 单据毛利面：一行一张 ERP 单据，永不连接订单行
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS reporting.v_erp_document_daily;

CREATE OR REPLACE VIEW reporting.v_erp_document_daily AS
SELECT o.shop_id,
       o.erp_id,
       (o.paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
       o.raw_cost,
       o.raw_gross_profit,
       o.split_parent_id,
       cardinality(o.commercial_ids) AS commercial_ids_count,
       o.normalization_status
FROM bi.orders o
WHERE o.active AND o.paid_at IS NOT NULL;

COMMENT ON VIEW reporting.v_erp_document_daily IS
  'ERP 单据口径面：主键即 (shop_id, erp_id)，与 bi.orders 一对一。active=false 的关闭单'
  '与无支付时间的单不出现（与 v_shop_daily 的 documents 同一集合）。拆单/合单以列形式'
  '给出，供覆盖披露使用；本视图不把毛利摊到商品，也不跨行重复累加。';

-- ---------------------------------------------------------------------------
-- 权限：新增视图逐项授权；底层表权限不因建视图而扩大
-- （bi_app 只能读 reporting 视图这条边界由 tests.test_commerce 的权限用例钉住）
-- ---------------------------------------------------------------------------
GRANT SELECT ON reporting.v_product_candidate_lines, reporting.v_product_refs,
                reporting.v_product_cost_daily, reporting.v_erp_document_daily
     TO bi_reader, bi_app;

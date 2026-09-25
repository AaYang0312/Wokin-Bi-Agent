-- S3 关闭单口径决策（计划 2026-09-25-first-two-workflows-launch §2 S3）：
-- tb/tm（local_filter）的支付口径在**查询侧**剔除「结构性证明的关闭后退款单」的
-- 已核验支付；退款仍单独列示；`paid_amount − refund_amount` 只叫期间收支差额。
-- 口径默认建议待所有者追认（docs/superpowers/research/2026-09-25-s3-acceptance.md）。
--
-- 为什么需要一条迁移：查询路径（bi_app）只能读 reporting 视图，而「单据已关闭」
-- （bi.orders.active）与「退款已成立」（bi.aftersales.platform_success +
-- refund_canonical）这两件取证事实都只存在于 bi.* 底表。没有这条视图，
-- 查询侧政策无法在不碰底表的前提下执行。
--
-- 为什么它仍然只是查询政策：本迁移**不改写任何存量事实**——不给 bi.orders 改
-- active，不删不改建支付/退款行，因此历史数字无需重述、replay 仍非必需
-- （普通增量同版本不重写的语义原样保留）。旧口径数字与本口径的差值由查询结果
-- 的 closed_refunded_excluded 诊断逐项给出。
--
-- 「结构性证明」的谓词（三条同时成立才剔除，宁可漏剔不可错剔）：
--   1) 该商业号的**全部**单据都已关闭（NOT active）：还存在活动单据就不算已关闭；
--   2) 至少存在一张含该商业号的关闭单据（不是凭空消失的商业号）;
--   3) 存在挂同一商业号、平台成功且 canonical 的退款（关闭未退款不算）。
-- 与 sync 既有谓词同源：active 语义即 unifiedStatus=CLOSED；canonical 成功退款
-- 与 refund_amount 的分母集合一致（001 v_refunds / 设计 §5）。

CREATE OR REPLACE VIEW reporting.v_closed_refunded_payments AS
SELECT p.shop_id,
       p.commercial_id,
       p.paid_at,
       (p.paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
       p.amount,
       p.verified
FROM bi.order_payments p
WHERE EXISTS (
        SELECT 1 FROM bi.orders o
        WHERE o.shop_id = p.shop_id
          AND o.commercial_ids @> ARRAY[p.commercial_id]
          AND NOT o.active)
  AND NOT EXISTS (
        SELECT 1 FROM bi.orders o
        WHERE o.shop_id = p.shop_id
          AND o.commercial_ids @> ARRAY[p.commercial_id]
          AND o.active)
  AND EXISTS (
        SELECT 1 FROM bi.aftersales a
        WHERE a.shop_id = p.shop_id
          AND a.commercial_id = p.commercial_id
          AND a.platform_success AND a.refund_canonical);

COMMENT ON VIEW reporting.v_closed_refunded_payments IS
  '结构性证明的关闭后退款单的支付事实（S3 查询政策输入）：支付口径据此在查询侧'
  '剔除这些商业单的已核验支付，退款本身仍按发生额单独列示。粒度=商业单支付行；'
  '不含任何 PII（字段集为 v_payments 子集加归属日）。谓词：全部含该商业号的'
  'bi.orders 均已关闭（NOT active），且存在同商业号的平台成功 canonical 退款。'
  '本视图不改写任何事实；迁移可重复执行。';

-- 权限逐项授权（与 017/025 同规）：底层表权限不因建视图而扩大。
GRANT SELECT ON reporting.v_closed_refunded_payments TO bi_reader, bi_app;

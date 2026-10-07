-- M3（计划 §3.M3）：生意参谋手工指标的查询读取面。
--
-- 依赖：028（bi.sycm_shop_daily）。001–029 零改动。
--
-- 028 按 M1 纪律没有授予任何角色任何权限；M3 的固定查询路径以 bi_reader /
-- bi_app 身份运行，只能读 reporting 视图。本迁移只加一个列原样的只读视图并
-- 授予读权限：聚合、覆盖与能力门禁全部仍在查询层（fail-closed 不依赖本视图），
-- 视图不隐藏、不改写任何列，paid_amount_sycm 与 ERP paid_amount 的口径隔离
-- 由查询层测试钉死（tests.test_sycm_metrics.CaliberIsolationTests）。

CREATE OR REPLACE VIEW reporting.v_sycm_shop_daily AS
SELECT shop_id, stat_date, visitors, item_visitors, views, paid_amount_sycm,
       paid_buyers, paid_items, order_amount, refund_amount, ad_spend_all,
       ad_spend_keyword, ad_spend_crowd, ad_spend_smart, ke_commission,
       pay_conversion_rate, aov, cart_count, favorite_count, batch_id, recorded_at
FROM bi.sycm_shop_daily;

COMMENT ON VIEW reporting.v_sycm_shop_daily IS
  '生意参谋手工导出店铺维度的只读投影（028 列原样）。查询层经它做窗口聚合；'
  '写面仍只走 M2 导入器，本视图不授予任何写权限。';

REVOKE ALL ON reporting.v_sycm_shop_daily FROM PUBLIC;
GRANT SELECT ON reporting.v_sycm_shop_daily TO bi_reader, bi_app;

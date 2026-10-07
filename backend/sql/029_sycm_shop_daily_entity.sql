-- M2：bi.sync_state.entity 开放 'shop_daily'——生意参谋手工导出的覆盖凭证专用实体。
--
-- 背景：M1（028）建了 bi.sycm_shop_daily 等事实表，但没有开放 sync_state 的
-- entity 词表；M2 导入侧要按计划写覆盖凭证（source='manual.sycm.export'、
-- entity='shop_daily'、covered multirange 累加、data_as_of、
-- quality_status='unknown'、quality_rule='manual-export/unverified'），不开放
-- 词表就写不进去。沿用 025 的先例：整表 DROP+ADD 同名约束，词表只增不改。
-- 授权零改动；028 的四张表与其"无授权"契约不受影响。
--
-- 依赖：025（entity 词表当前版本）、028（manual sycm 事实表）。

ALTER TABLE bi.sync_state DROP CONSTRAINT IF EXISTS sync_state_entity;
ALTER TABLE bi.sync_state ADD CONSTRAINT sync_state_entity CHECK (
  entity IN ('orders', 'orders_consign', 'aftersales_occurrence',
             'aftersales_cohort', 'session', 'shop_daily'));

COMMENT ON CONSTRAINT sync_state_entity ON bi.sync_state IS
  'shop_daily = 生意参谋手工导出的店铺维度日聚合覆盖（manual.sycm.export 专用）。';

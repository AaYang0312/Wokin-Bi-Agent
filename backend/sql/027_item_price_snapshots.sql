-- S4：ERP 档案价快照（erp.item.list.get 的 priceOutput / purchasePrice）。
--
-- 依赖：001（bi.shops / 角色）、005（bi.products 提供扫描枚举）。001–026 零改动。
--
-- 设计契约（计划 §2/S4 + 多来源指标设计 §3–§6 + 运营工作流设计 §5.4/§10）：
--   1) **档案价是档案目录价，不是渠道促销价/实售价**：priceOutput → reference_price，
--      purchasePrice → 档案参考成本。两者只在「SKU 级参考」语义下与成交均价并列展示，
--      永不互相冒充，也永不进排名。
--   2) **scan_complete 凭据同 019 契约精神**：「看完全店目录」是一个主张而不是布尔
--      偏好：完整声明必须带分页/枚举凭据（batches、expected/fetched 计数等聚合），
--      不完整扫描由同步侧整批回滚，根本不落行；这里的配对 CHECK 是最后一道闸。
--   3) **activeStatus=0 必须显式已停用**：停用状态随行存储并进 reporting 视图，
--      查询侧按「已停用档案」披露，不得静默丢弃，也不得冒充在售。
--   4) **身份 = (店, 快照, 系统商品, 系统规格)**：sys_sku_id 为空串表示商品级行
--      （无规格商品的自然身份，019 channel_items 同一形状）；SKU 级参考只认
--      sys_sku_id 与订单行 sku_id（skuSysId）的显式相等。
--   5) **idempotent 日频快照**：同一 (店, 北京时间日) 重扫生成同一 snapshot_id，
--      先删后插、单事务提交；过期判定（默认 7 天，计划建议值、待所有者追认）
--      由查询侧按 captured_at 计算，缺快照/过期 = data_missing，不补零。
--   6) 拼多多店铺永不进入本表：档案价扫描只对已登记非 PDD 平台开放
--      （`sources.item_price_scan_allowed`）。

-- ---------------------------------------------------------------------------
-- 1) 快照头：一次「全店目录扫描」一份头表，完整性凭据挂在头上
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.item_price_snapshots (
  snapshot_id     text NOT NULL,
  shop_id         text NOT NULL REFERENCES bi.shops(shop_id) ON DELETE CASCADE,
  source          text NOT NULL,
  captured_at     timestamptz NOT NULL,
  -- 仅当这次扫描把枚举目录取尽且有凭据时才允许为 true；同步侧对不完整扫描
  -- 整批回滚，所以正常数据里不会出现 false 行——约束仍保留，防止旁路写入。
  scan_complete   boolean NOT NULL DEFAULT false,
  scan_evidence   text,
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, snapshot_id),
  CONSTRAINT item_price_snapshots_source CHECK (
    source IN ('kuaimai_item_api')),
  CONSTRAINT item_price_snapshots_shop CHECK (btrim(shop_id) <> ''),
  -- 挡「未来抓取」；与 019 同一只留一天时钟宽容度。
  CONSTRAINT item_price_snapshots_captured_not_future CHECK (
    captured_at <= now() + interval '1 day'),
  CONSTRAINT item_price_snapshots_scan_pair CHECK (
    (scan_complete AND btrim(coalesce(scan_evidence, '')) <> '')
    OR (NOT scan_complete AND scan_evidence IS NULL))
);

CREATE INDEX IF NOT EXISTS item_price_snapshots_shop_captured_idx
  ON bi.item_price_snapshots (shop_id, captured_at DESC);

COMMENT ON TABLE bi.item_price_snapshots IS
  'ERP 档案价快照头：一次全店目录扫描一份。档案目录价不是渠道促销价/实售价，'
  '两者永不互相冒充；拼多多店铺永不进入本表。';
COMMENT ON COLUMN bi.item_price_snapshots.scan_complete IS
  'true 仅当这次扫描把枚举目录取尽且带分页/枚举凭据；缺失的行不得被判成「档案无价」。';
COMMENT ON COLUMN bi.item_price_snapshots.scan_evidence IS
  '聚合凭据（批次数、期望/实取商品数、非空价格计数、停用计数等），只留写入侧：'
  'reporting 视图不暴露。';

-- ---------------------------------------------------------------------------
-- 2) 快照明细：商品级 + SKU 级档案价（缺价格/成本存 NULL，绝不补零）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.item_price_snapshot_items (
  shop_id            text NOT NULL,
  snapshot_id        text NOT NULL,
  sys_item_id        text NOT NULL,
  -- 空串 = 商品级行（无规格商品的自然身份）；SKU 级参考只认与订单行
  -- sku_id（skuSysId）显式相等的非空 sys_sku_id。
  sys_sku_id         text NOT NULL DEFAULT '',
  outer_id           text,
  sku_outer_id       text,
  reference_price    numeric(20, 6),
  purchase_price     numeric(20, 6),
  -- 档案可用状态：0=停用，1=启用；停用必须显式披露，不得静默丢弃。
  item_active_status integer NOT NULL CHECK (item_active_status IN (0, 1)),
  sku_active_status  integer CHECK (sku_active_status IN (0, 1)),
  captured_at        timestamptz NOT NULL,
  PRIMARY KEY (shop_id, snapshot_id, sys_item_id, sys_sku_id),
  CONSTRAINT item_price_items_snapshot_fk FOREIGN KEY (shop_id, snapshot_id)
    REFERENCES bi.item_price_snapshots (shop_id, snapshot_id) ON DELETE CASCADE,
  CONSTRAINT item_price_items_item CHECK (btrim(sys_item_id) <> ''),
  CONSTRAINT item_price_items_captured_not_future CHECK (
    captured_at <= now() + interval '1 day')
);

COMMENT ON TABLE bi.item_price_snapshot_items IS
  'ERP 档案价快照明细：reference_price=priceOutput（档案销售价），'
  'purchase_price=purchasePrice（档案成本价）。缺价格/成本存 NULL：'
  '查询侧给 data_missing，不补零、不外推。';
COMMENT ON COLUMN bi.item_price_snapshot_items.sys_sku_id IS
  '空串=商品级行；非空=系统规格编码（与订单行 sku_id/skuSysId 同一身份空间）。';

-- ---------------------------------------------------------------------------
-- 3) reporting 只读视图：证据列不进视图（bi_app 拿不到凭据，与 019 同一纪律）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW reporting.v_item_price_snapshots AS
SELECT snapshot_id, shop_id, source, captured_at, scan_complete
FROM bi.item_price_snapshots;

CREATE OR REPLACE VIEW reporting.v_item_price_snapshot_items AS
SELECT snapshot_id, shop_id, sys_item_id, sys_sku_id, outer_id, sku_outer_id,
       reference_price, purchase_price, item_active_status, sku_active_status,
       captured_at
FROM bi.item_price_snapshot_items;

COMMENT ON VIEW reporting.v_item_price_snapshots IS
  '档案价快照头（无凭据列）：查询侧按 (店, captured_at) 判新鲜度，'
  '过期/缺失 = data_missing。';
COMMENT ON VIEW reporting.v_item_price_snapshot_items IS
  '档案价快照明细（无凭据列）。档案目录价不是渠道促销价；'
  'item_active_status=0 的行按「已停用档案」显式披露。';

-- ---------------------------------------------------------------------------
-- 4) 授权：同步身份写事实；应用/读取身份只读视图（001 的
--    “ON ALL TABLES IN SCHEMA”不覆盖后建表，必须逐项授权/收权）
-- ---------------------------------------------------------------------------
REVOKE ALL ON bi.item_price_snapshots, bi.item_price_snapshot_items FROM PUBLIC;
REVOKE ALL ON bi.item_price_snapshots, bi.item_price_snapshot_items FROM bi_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON bi.item_price_snapshots, bi.item_price_snapshot_items TO bi_sync;
GRANT SELECT ON reporting.v_item_price_snapshots,
  reporting.v_item_price_snapshot_items TO bi_reader, bi_app;

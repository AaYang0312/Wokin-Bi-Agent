-- 跨渠道商品 / SKU 映射（运营工作流计划 Task 6）。
-- 编号说明：计划原为 Task 6 预留 010，但主线已按时间顺序应用到 015（Task 5 的多来源
-- 契约与血缘版本），保留 010 会让“编号=应用顺序”这一约定失效，因此本迁移顺延为 016。
-- 依赖 001（bi.shops / bi.order_items）与 005（bi.products）。
--
-- 两条不可让步的规则：
--   1) 自动合并只接受**已确认的显式标识映射**；平台商品名只能辅助候选，不能决定合并。
--   2) 映射身份含 namespace（账号/租户范围）：另一账号里的相同数字 ID 不是同一个商品。

CREATE TABLE IF NOT EXISTS bi.channel_items (
  namespace        text NOT NULL,
  platform         text NOT NULL,
  shop_id          text NOT NULL REFERENCES bi.shops(shop_id),
  listing_id       text NOT NULL DEFAULT '',
  platform_sku_id  text NOT NULL DEFAULT '',
  erp_product_id   text,
  erp_sku_id       text,
  status           text NOT NULL DEFAULT 'approved',
  source           text NOT NULL,
  evidence         text NOT NULL,
  mapping_version  text NOT NULL,
  valid_from       date NOT NULL,
  valid_to         date,
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, shop_id, listing_id, platform_sku_id, valid_from),
  CONSTRAINT channel_items_status CHECK (status IN ('approved', 'ambiguous', 'unresolved')),
  CONSTRAINT channel_items_source CHECK (
    source IN ('trade_line', 'manual_map', 'channel_api', 'import')),
  -- approved 必须说清合并依据：证据字段为空就是没依据。
  CONSTRAINT channel_items_evidence_required CHECK (evidence <> ''),
  CONSTRAINT channel_items_needs_erp_product CHECK (erp_product_id IS NOT NULL),
  -- 有效期不得倒挂；来源切换按有效期间分段，禁止两条重叠区间同时生效。
  CONSTRAINT channel_items_valid_window CHECK (valid_to IS NULL OR valid_to >= valid_from),
  CONSTRAINT channel_items_version CHECK (mapping_version ~ '^[a-z0-9._/-]+$'),
  -- 成交行给不出渠道链接号：listing_id 为空时只能来自 trade_line。
  CONSTRAINT channel_items_listing_source CHECK (
    listing_id <> '' OR source = 'trade_line')
);

CREATE INDEX IF NOT EXISTS channel_items_product_idx
  ON bi.channel_items (erp_product_id, namespace, shop_id);
CREATE INDEX IF NOT EXISTS channel_items_validity_idx
  ON bi.channel_items (erp_product_id, valid_from, valid_to);

CREATE OR REPLACE VIEW reporting.v_channel_items AS
SELECT namespace, platform, shop_id, listing_id, platform_sku_id, erp_product_id,
       erp_sku_id, status, source, mapping_version, valid_from, valid_to
FROM bi.channel_items;

COMMENT ON COLUMN bi.channel_items.listing_id IS
  '渠道商品链接号（num_iid / 商品ID 等）。空串表示这条记录只登记 ERP 侧身份'
  '（来自成交行），不代表渠道存在某个链接。';
COMMENT ON COLUMN bi.channel_items.evidence IS
  '合并依据：探针编号、人工映射单号或渠道接口批次。自动合并只接受显式标识映射，'
  '平台商品名相同不构成依据。';
COMMENT ON COLUMN bi.channel_items.mapping_version IS
  '映射口径版本；参与结果血缘，版本一变旧结果不得复用。';

-- 应用身份与同步写入权限：bi_sync 才能登记映射，读取走 reporting 视图。
REVOKE ALL ON bi.channel_items FROM PUBLIC;
REVOKE ALL ON bi.channel_items FROM bi_app;
GRANT SELECT, INSERT, UPDATE ON bi.channel_items TO bi_sync;
GRANT SELECT ON reporting.v_channel_items TO bi_reader, bi_app;

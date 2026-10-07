-- M1：生意参谋手工导出导入的数据层（计划 §0–§2、§3.M1）。
--
-- 依赖：001（bi.shops / 角色体系）。001–027 零改动。
--
-- 设计契约（计划 manual-sycm-export-import）：
--   1) **列白名单**：店铺维度只收计划选定的 17 个指标列，客户维度只收 8 个日聚合
--      列。表结构是解析白名单之外的第二道闸：白名单外的列——含任何个体级明细列
--      ——在解析侧被拒（pii_columns_rejected），库内也没有可承载它们的列。
--   2) **口径不混**：生意参谋支付金额是独立口径列 paid_amount_sycm，与 ERP
--      paid_amount 永不比较/合计/互相替代（§2/D2，查询侧测试钉死）。
--   3) **身份 = (店, 北京时间日)**：按 (shop_id, stat_date) 幂等覆盖，后批替换前批
--      由导入侧先删后插完成；batch_id 记批次（文件内容哈希），lineage 永可追溯。
--   4) **比率列存分数**：导出为百分数（实测支付转化率形如 0.14%），解析侧 ÷100
--      存 [0,1] 分数；CHECK 拦下忘除 100 的解析回归。
--   5) **缺值存 NULL 不补零**：只有解析侧确认的「零花费+披露」才写 0（§1）。
--   6) **manual_uploads 是写面台账**；manual_upload_events 只追加——本迁移不授予
--      任何角色任何权限（001 的 ON ALL TABLES 不覆盖后建表），所以事件表现在对
--      任何角色都不可 UPDATE/DELETE；后续写面切片落地授权时，事件表也只能追加
--      SELECT/INSERT，UPDATE/DELETE 授权永不发生。
--   7) 覆盖凭证复用 bi.sync_state（source='manual.sycm.export'、entity='shop_daily'），
--      本迁移不建新凭证表（§3.M1）。
--   8) 拼多多店铺永不进入：manual 来源挂 bi.shops.capabilities 既有机制，注册侧对
--      pdd 平台永不授予（§3.M2）；表结构不区分平台。
--
-- 表头映射以 2026-10-01 导出实测为准（docs/手动导出表格/ 12 文件）。

-- ---------------------------------------------------------------------------
-- 1) 店铺维度日聚合：流量、交易（sycm 口径）、推广实耗、转化
--    实测表头 → 列：访客数→visitors、商品访客数→item_visitors、浏览量→views、
--    支付金额→paid_amount_sycm、支付买家数→paid_buyers、支付件数→paid_items、
--    下单金额→order_amount、成功退款金额→refund_amount、全站推广花费→
--    ad_spend_all、关键词推广花费→ad_spend_keyword、精准人群推广花费→
--    ad_spend_crowd、智能场景花费→ad_spend_smart、淘宝客佣金→ke_commission、
--    支付转化率→pay_conversion_rate、客单价→aov、加购人数→cart_count、
--    商品收藏买家数→favorite_count；统计日期/店铺名称 → (stat_date, shop_id)。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.sycm_shop_daily (
  shop_id             text NOT NULL REFERENCES bi.shops(shop_id) ON DELETE CASCADE,
  stat_date           date NOT NULL,
  visitors            bigint CHECK (visitors >= 0),
  item_visitors       bigint CHECK (item_visitors >= 0),
  views               bigint CHECK (views >= 0),
  paid_amount_sycm    numeric(20, 6) CHECK (paid_amount_sycm >= 0),
  paid_buyers         bigint CHECK (paid_buyers >= 0),
  paid_items          bigint CHECK (paid_items >= 0),
  order_amount        numeric(20, 6) CHECK (order_amount >= 0),
  refund_amount       numeric(20, 6) CHECK (refund_amount >= 0),
  ad_spend_all        numeric(20, 6) CHECK (ad_spend_all >= 0),
  ad_spend_keyword    numeric(20, 6) CHECK (ad_spend_keyword >= 0),
  ad_spend_crowd      numeric(20, 6) CHECK (ad_spend_crowd >= 0),
  ad_spend_smart      numeric(20, 6) CHECK (ad_spend_smart >= 0),
  ke_commission       numeric(20, 6) CHECK (ke_commission >= 0),
  pay_conversion_rate numeric(10, 8) CHECK (pay_conversion_rate BETWEEN 0 AND 1),
  aov                 numeric(20, 6) CHECK (aov >= 0),
  cart_count          bigint CHECK (cart_count >= 0),
  favorite_count      bigint CHECK (favorite_count >= 0),
  batch_id            text NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, stat_date),
  CONSTRAINT sycm_shop_daily_batch CHECK (btrim(batch_id) <> ''),
  CONSTRAINT sycm_shop_daily_stat_date_not_future CHECK (
    stat_date <= (now() + interval '1 day')::date)
);

COMMENT ON TABLE bi.sycm_shop_daily IS
  '生意参谋手工导出·店铺维度日聚合（白名单 17 指标列）。手工来源立即可用+披露'
  '（quality unknown）；paid_amount_sycm 是 sycm 独立口径，永不与 ERP paid_amount '
  '混算。缺值存 NULL 不补零；拼多多店铺永不进入。';
COMMENT ON COLUMN bi.sycm_shop_daily.paid_amount_sycm IS
  '生意参谋「支付金额」，独立口径列；与 ERP paid_amount 不得出现在同一合计/比较。';
COMMENT ON COLUMN bi.sycm_shop_daily.pay_conversion_rate IS
  '「支付转化率」÷100 后的分数（0.14% → 0.0014）；CHECK 拦忘除 100 的解析回归。';
COMMENT ON COLUMN bi.sycm_shop_daily.batch_id IS
  '导入批次（文件内容哈希）；后批替换前批后，lineage 仍按行可追溯。';

-- ---------------------------------------------------------------------------
-- 2) 客户维度日聚合：只收白名单聚合列，任何个体级明细列永不入库（PII 白名单不变）
--    实测表头 → 列：用户数→users、已购用户数→paid_users、新访客户数→
--    new_customers、父订单数→parent_orders、复购父订单数→repeat_parent_orders、
--    180天复购率→repeat_rate_180d、店铺粉丝数→shop_fans、店铺会员数→shop_members。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.sycm_customer_daily (
  shop_id              text NOT NULL REFERENCES bi.shops(shop_id) ON DELETE CASCADE,
  stat_date            date NOT NULL,
  users                bigint CHECK (users >= 0),
  paid_users           bigint CHECK (paid_users >= 0),
  new_customers        bigint CHECK (new_customers >= 0),
  parent_orders        bigint CHECK (parent_orders >= 0),
  repeat_parent_orders bigint CHECK (repeat_parent_orders >= 0),
  repeat_rate_180d     numeric(10, 8) CHECK (repeat_rate_180d BETWEEN 0 AND 1),
  shop_fans            bigint CHECK (shop_fans >= 0),
  shop_members         bigint CHECK (shop_members >= 0),
  batch_id             text NOT NULL,
  recorded_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, stat_date),
  CONSTRAINT sycm_customer_daily_batch CHECK (btrim(batch_id) <> ''),
  CONSTRAINT sycm_customer_daily_stat_date_not_future CHECK (
    stat_date <= (now() + interval '1 day')::date)
);

COMMENT ON TABLE bi.sycm_customer_daily IS
  '生意参谋手工导出·客户维度日聚合（白名单 8 聚合列）。只收日聚合：个体级明细列'
  '（买家昵称/ID 等）在解析侧拒绝、库内无载体。repeat_rate_180d 存 ÷100 后的分数。';
COMMENT ON COLUMN bi.sycm_customer_daily.repeat_rate_180d IS
  '「180天复购率」÷100 后的分数（15.76% → 0.1576）。';

-- ---------------------------------------------------------------------------
-- 3) 上传台账：一次文件导入一条；kind/status/reason_code 词表随写面切片钉住
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.manual_uploads (
  id             uuid PRIMARY KEY,
  subject_id     text NOT NULL,
  filename       text NOT NULL,
  content_sha256 text NOT NULL,
  kind           text CHECK (kind IN ('shop', 'customer')),
  status         text NOT NULL,
  reason_code    text,
  rows_read      integer NOT NULL DEFAULT 0,
  rows_written   integer NOT NULL DEFAULT 0,
  shops          text[] NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  CONSTRAINT manual_uploads_subject CHECK (btrim(subject_id) <> ''),
  CONSTRAINT manual_uploads_filename CHECK (btrim(filename) <> ''),
  CONSTRAINT manual_uploads_sha256 CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE bi.manual_uploads IS
  '手工导入上传台账：一次文件导入一条，内容以 sha256 定位，批次 lineage 从这里'
  '挂到 sycm_*_daily.batch_id。kind 允许暂未知（分类失败也要留台账）；status/'
  'reason_code 词表由写面切片（M2/M4）钉住。';
COMMENT ON COLUMN bi.manual_uploads.shops IS
  '本次导入覆盖的 shop_id 列表；未映射店铺整文件拒绝时不留任何 shop。';

-- ---------------------------------------------------------------------------
-- 4) 上传事件：只追加审计流。无授权 + CASCADE，事件随台账生死、对任何角色不可改删
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.manual_upload_events (
  id         uuid PRIMARY KEY,
  upload_id  uuid NOT NULL REFERENCES bi.manual_uploads(id) ON DELETE CASCADE,
  event_type text NOT NULL,
  payload    jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT manual_upload_events_type CHECK (btrim(event_type) <> '')
);

CREATE INDEX IF NOT EXISTS manual_upload_events_upload_idx
  ON bi.manual_upload_events (upload_id, created_at);

COMMENT ON TABLE bi.manual_upload_events IS
  '手工导入事件（只追加）：received/rejected 等审计事件按 upload 归组；本迁移'
  '不授予任何角色权限，UPDATE/DELETE 授权永不发生，后续只可追加 SELECT/INSERT。';

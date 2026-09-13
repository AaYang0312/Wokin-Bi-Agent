-- 上架价复核（运营工作流计划 Task 9）：渠道在售快照、期望 roster 与本轮目标价冻结。
--
-- 编号说明：计划为 Task 9 预留 012，但主线已按「编号 = 应用顺序」应用到 017
-- （Task 6 / Task 7 都因同一理由把各自预留的 010 / 011 顺延为 016 / 017）。
-- 这里若回填 012，就会排到已应用过的 014–017 前面，迁移清单重新变得不可信，
-- 故顺延为 018。依赖：001（bi.shops / 角色）、009（bi.query_runs 与 price_audit 类型）、
-- 016（bi.channel_items 提供 SKU / 链接落点）。
--
-- 四条不可让步的规则：
--   1) **快照必须带来源与抓取时点**。没有 captured_at 就没有"当前标价"可言；
--      没有来源声明就无法判断时效。二者都是 NOT NULL，不给"半个声明"留活路。
--   2) **完整性声明必须带依据**。enumeration_complete = true 只有在同时写下
--      enumeration_evidence（全量枚举的分页 / 批次凭据）时才成立，否则 SQL 直接拒。
--      这条是 spec §5.4「缺列表 / 无权限不能当未上架」在存储层的落点。
--   3) **ERP 建议价不是渠道在售价**：source 词表里没有它，也没有任何列可以把它
--      伪装成 listing 标价（spec §9）。
--   4) **本轮目标价只进 price_audit_expectations，且 captured_from 恒为
--      'current_user_input'**。这是一条 CHECK，不是一个约定：长期价格表不是首版
--      前置任务（计划 Task 9），而"从上一轮悄悄继承一个标准"必须先能写出这一列。
--      复核图也不读这张表（见 tests/test_listing_audit.py 的 SQL 断言）。

-- ---------------------------------------------------------------------------
-- 1) 渠道在售快照：一次抓取 = 一个批次
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.listing_snapshots (
  snapshot_id           text NOT NULL,
  namespace             text NOT NULL,
  platform              text NOT NULL,
  shop_id               text NOT NULL REFERENCES bi.shops(shop_id),
  source                text NOT NULL,
  -- 取证凭据（探针编号 / 官方导出单号 / 渠道接口批次）；空证据不配当已核验来源。
  evidence              text NOT NULL,
  captured_at           timestamptz NOT NULL,
  -- 「这家店的上架全集是否被完整枚举」：决定缺行能不能判 not_listed。
  enumeration_complete  boolean NOT NULL DEFAULT false,
  enumeration_evidence  text,
  batch_id              text,
  schema_version        text NOT NULL DEFAULT '018',
  recorded_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, shop_id, snapshot_id),
  -- 快照号是主键的一部分：空串能存进去就会有一行"哪一批都不是"的快照写进血缘。
  CONSTRAINT listing_snapshots_id_non_blank CHECK (btrim(snapshot_id) <> ''),
  CONSTRAINT listing_snapshots_source CHECK (
    source IN ('channel_api', 'official_export', 'manual_import')),
  CONSTRAINT listing_snapshots_evidence_required CHECK (btrim(evidence) <> ''),
  -- 不能声称一个比写入时刻还晚的抓取：`recorded_at` 是服务端默认值，调用方改不了，
  -- 所以这条能挡住"把旧数据重新贴一个今天的 captured_at"。只留时钟偏移的宽容度。
  CONSTRAINT listing_snapshots_captured_not_future CHECK (
    captured_at <= recorded_at + interval '5 minutes'),
  -- 完整性声明与依据成对出现：true 无凭据 = 没凭据；false 带凭据 = 说了半句。
  CONSTRAINT listing_snapshots_enumeration_pair CHECK (
    (enumeration_complete AND btrim(coalesce(enumeration_evidence, '')) <> '')
    OR (NOT enumeration_complete AND enumeration_evidence IS NULL)),
  CONSTRAINT listing_snapshots_platform CHECK (platform ~ '^[a-z0-9][a-z0-9_-]{0,15}$'),
  CONSTRAINT listing_snapshots_namespace CHECK (btrim(namespace) <> ''),
  CONSTRAINT listing_snapshots_schema_version CHECK (schema_version ~ '^[0-9]{3}$')
);

CREATE INDEX IF NOT EXISTS listing_snapshots_shop_captured_idx
  ON bi.listing_snapshots (shop_id, captured_at DESC);

COMMENT ON TABLE bi.listing_snapshots IS
  '渠道在售快照头：一次抓取一批。来源、抓取时点与完整性声明都在这一行；'
  '快照是否"可用"仍要过代码侧的来源注册表（listing_audit/rules），两个门禁缺一不可。';
COMMENT ON COLUMN bi.listing_snapshots.enumeration_complete IS
  'true 仅当该店铺的上架全集被完整枚举（分页取尽）且有凭据；否则缺行不能判 not_listed。';
COMMENT ON COLUMN bi.listing_snapshots.source IS
  '获准的渠道在售价来源种类：channel_api / official_export / manual_import。'
  'ERP 档案建议价不在这个词表里，它不是渠道在售价。';

-- ---------------------------------------------------------------------------
-- 2) 快照明细：一条链接（× 平台 SKU）在当前口径下的标价
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.listing_snapshot_items (
  snapshot_id        text NOT NULL,
  shop_id            text NOT NULL,
  namespace          text NOT NULL,
  listing_id         text NOT NULL,
  platform_sku_id    text NOT NULL DEFAULT '',
  -- ERP 侧身份只有来源自己声明过才写：不拿名称去猜一个 SKU 出来。
  erp_sku_id         text NOT NULL DEFAULT '',
  erp_product_id     text NOT NULL DEFAULT '',
  list_amount        numeric(18, 4),
  campaign_amount    numeric(18, 4),
  currency           text NOT NULL DEFAULT 'CNY',
  on_sale            boolean NOT NULL DEFAULT true,
  captured_at        timestamptz NOT NULL,
  CONSTRAINT listing_snapshot_items_pk PRIMARY KEY (namespace, shop_id, snapshot_id,
                                                    listing_id, platform_sku_id),
  CONSTRAINT listing_snapshot_items_snapshot_fk FOREIGN KEY (namespace, shop_id, snapshot_id)
    REFERENCES bi.listing_snapshots (namespace, shop_id, snapshot_id) ON DELETE CASCADE,
  -- 链接号是明细的身份之一：空号意味着"这条记录不代表任何链接"，那就该整条不写。
  CONSTRAINT listing_snapshot_items_listing_required CHECK (btrim(listing_id) <> ''),
  -- 金额要么没拿到（NULL），要么是个非负数：负标价不是"退款价"，是没核验。
  CONSTRAINT listing_snapshot_items_amount_non_negative CHECK (
    (list_amount IS NULL OR list_amount >= 0)
    AND (campaign_amount IS NULL OR campaign_amount >= 0)),
  -- 至少声明一种标价口径；一行什么都没说就不该占一个位置。
  CONSTRAINT listing_snapshot_items_declares_a_price CHECK (
    list_amount IS NOT NULL OR campaign_amount IS NOT NULL),
  CONSTRAINT listing_snapshot_items_currency CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE INDEX IF NOT EXISTS listing_snapshot_items_sku_idx
  ON bi.listing_snapshot_items (shop_id, erp_sku_id, snapshot_id);
CREATE INDEX IF NOT EXISTS listing_snapshot_items_product_idx
  ON bi.listing_snapshot_items (erp_product_id, snapshot_id);

COMMENT ON COLUMN bi.listing_snapshot_items.list_amount IS
  '该链接的当前标价（NULL = 来源未声明，不是 0）。活动价单列，不与标价互推。';
COMMENT ON COLUMN bi.listing_snapshot_items.erp_sku_id IS
  '来源自己声明的 ERP SKU 身份：不由名称或规格猜出。为空时只能按 (链接, 平台 SKU) 匹配。';

-- ---------------------------------------------------------------------------
-- 3) 期望 roster：本轮要复核哪几格（目标上架全集）
-- ---------------------------------------------------------------------------
-- 分母来自这里，而不是来自"抓到了哪些链接"：缺一家必须还能被数出来。
CREATE TABLE IF NOT EXISTS bi.expected_listing_rosters (
  run_id            uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  subject_id        text NOT NULL,
  shop_id           text NOT NULL REFERENCES bi.shops(shop_id),
  listing_ref       text NOT NULL DEFAULT '',
  sku_ref           text NOT NULL DEFAULT '',
  currency          text NOT NULL DEFAULT 'CNY',
  price_basis       text NOT NULL,
  built_from        text NOT NULL DEFAULT 'authorized_scope_x_sku',
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, shop_id, listing_ref, sku_ref),
  CONSTRAINT expected_roster_price_basis CHECK (price_basis IN ('list_price', 'campaign_price')),
  CONSTRAINT expected_roster_currency CHECK (currency ~ '^[A-Z]{3}$'),
  -- roster 只说明"这一格要复核"：它**不带价格列**。留一个可写的金额位就是一个没有
  -- 来源、没有凭据、也没人读的第二价格表（计划 Task 9：长期价格表不是首版前置）。
  CONSTRAINT expected_roster_built_from CHECK (built_from = 'authorized_scope_x_sku'),
  CONSTRAINT expected_roster_subject CHECK (btrim(subject_id) <> ''),
  CONSTRAINT expected_roster_listing_ref_form CHECK (
    listing_ref = '' OR listing_ref ~ '^lst-[0-9a-f]{12}$'),
  CONSTRAINT expected_roster_sku_ref_form CHECK (
    sku_ref = '' OR sku_ref ~ '^ent-[0-9a-z]{8}$')
);

-- ---------------------------------------------------------------------------
-- 4) 本轮目标价冻结：可追踪，但永远不是长期价格表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.price_audit_expectations (
  run_id           uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  subject_id       text NOT NULL,
  shop_id          text NOT NULL REFERENCES bi.shops(shop_id),
  sku_ref          text NOT NULL DEFAULT '',
  expected_amount  numeric(18, 4) NOT NULL,
  currency         text NOT NULL DEFAULT 'CNY',
  price_basis      text NOT NULL,
  applies_to       text NOT NULL,
  captured_from    text NOT NULL DEFAULT 'current_user_input',
  request_fingerprint text,
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, shop_id, sku_ref),
  -- 这一条 CHECK 就是「不继承上一轮」的存储层凭据：想写别的出处就写不进去。
  CONSTRAINT price_audit_expectations_current_turn_only CHECK (
    captured_from = 'current_user_input'),
  CONSTRAINT price_audit_expectations_amount_positive CHECK (expected_amount >= 0),
  CONSTRAINT price_audit_expectations_price_basis CHECK (
    price_basis IN ('list_price', 'campaign_price')),
  CONSTRAINT price_audit_expectations_applies_to CHECK (
    applies_to IN ('all_selected', 'sku', 'shop_sku')),
  CONSTRAINT price_audit_expectations_fingerprint_format CHECK (
    request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$'),
  CONSTRAINT price_audit_expectations_subject CHECK (btrim(subject_id) <> '')
);

COMMENT ON TABLE bi.price_audit_expectations IS
  '本次审计的依据快照：用户在当前这一轮明确指定的目标价（spec §5.4）。'
  'captured_from 被 CHECK 钉死为 current_user_input；本表不作为复核输入被读取，'
  '也不构成长期价格表 —— 长期价格表不是首版前置任务。';

CREATE INDEX IF NOT EXISTS price_audit_expectations_run_idx
  ON bi.price_audit_expectations (run_id);
CREATE INDEX IF NOT EXISTS expected_listing_rosters_run_idx
  ON bi.expected_listing_rosters (run_id);

-- ---------------------------------------------------------------------------
-- 5) 读取视图与授权
-- ---------------------------------------------------------------------------
-- 复核图以 bi_app 身份连接：读快照走视图，写 roster / expectations 走上面的授权。
-- 明细里的渠道链接号与平台 SKU 号**会**进视图（图要靠它们把一格对到一条记录上），
-- 但它们不越过进程边界：公开行里只有 `lst-` 单向句柄（见 `listing_audit/rules.py`）。
-- 取证凭据 `evidence` 不在这两条视图里，也不在任何投影里。
CREATE OR REPLACE VIEW reporting.v_listing_snapshots AS
SELECT snapshot_id, namespace, platform, shop_id, source, captured_at,
       enumeration_complete, batch_id, schema_version
FROM bi.listing_snapshots;

CREATE OR REPLACE VIEW reporting.v_listing_snapshot_items AS
SELECT snapshot_id, namespace, shop_id, listing_id, platform_sku_id,
       erp_sku_id, erp_product_id, list_amount, campaign_amount, currency, on_sale,
       captured_at
FROM bi.listing_snapshot_items;

COMMENT ON COLUMN reporting.v_listing_snapshots.source IS
  '视图刻意不含 evidence：取证凭据是内部标识，不进展示与模型路径。';

-- 快照由同步 / 导入身份写入，应用身份只读；roster 与目标价由应用身份按运行写入。
-- 两张审计表只给 `SELECT, INSERT`，**不给 UPDATE**：这不是一时手紧，而是"本轮依据
-- 一次写入后不可改写"的存储层形状——也是 `record_price_audit_expectations` 必须用
-- 普通 INSERT（而不是 `ON CONFLICT DO UPDATE`）的原因：DO UPDATE 会向应用身份要到
-- 它不该有的列写权限，而重写一次已经发布的标准价本来就是错事。
REVOKE ALL ON bi.listing_snapshots, bi.listing_snapshot_items FROM PUBLIC;
REVOKE ALL ON bi.listing_snapshots, bi.listing_snapshot_items FROM bi_app;
GRANT SELECT, INSERT, UPDATE ON bi.listing_snapshots, bi.listing_snapshot_items
  TO bi_sync;
GRANT SELECT ON reporting.v_listing_snapshots, reporting.v_listing_snapshot_items
  TO bi_reader, bi_app;

REVOKE ALL ON bi.expected_listing_rosters, bi.price_audit_expectations FROM PUBLIC;
GRANT SELECT, INSERT ON bi.expected_listing_rosters, bi.price_audit_expectations
  TO bi_app;
-- 审计依据可被核对（对账 / 复盘），但不给读侧角色扩大范围：只授 bi_app 经过程序读取。
GRANT SELECT ON bi.expected_listing_rosters, bi.price_audit_expectations TO bi_sync;

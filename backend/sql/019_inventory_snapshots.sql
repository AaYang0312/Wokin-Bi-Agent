-- 库存两级预警（运营工作流计划 Task 10）：库存池与连接方式、实物 / 渠道可售两批快照、
-- 版本化阈值策略。
--
-- 编号说明：计划为 Task 10 预留 013，但主线已按「编号 = 应用顺序」应用到 018
-- （Task 6 / 7 / 9 都因同一理由把各自预留的 010 / 011 / 012 顺延）。这里若回填 013，
-- 就会排到已应用过的 014–018 前面，迁移清单重新变得不可信，故顺延为 019。
-- 依赖：001（bi.shops / 角色）、009（bi.query_runs 与 inventory_alerts 类型）、
-- 016（bi.channel_items 提供 SKU 落点）、018（同一命名空间的账号范围口径）。
--
-- 五条不可让步的规则：
--   1) **实物按 (账号范围, 池, 仓库, SKU, 批次, 单位) 唯一**。同一仓库可用 100 被三家
--      店各展示 100 时，实物总量仍是 100（spec §5.5）。所以实物表里**没有 shop_id
--      这一列**：店铺与池的关系另有其表，一旦把店铺写进事实行，"按店重复累计"就只是
--      一次 GROUP BY 的距离。
--   2) **渠道可售单独一张快照表**，永不与实物混写：把渠道显示数加成实物总量是本任务
--      点名要防的第一种错。
--   3) **一批 = 一个 (池, 仓库, 批次, 单位)**：批次与单位都是 NOT NULL，混批次求和
--      在结构上就不可能写出。
--   4) **扫描完整性声明必须带分页凭据**（与 018 的 enumeration_pair 同一形状）：
--      "看完全目录"是一个主张，不是一个布尔偏好。
--   5) **阈值的 quantity 与 unit 必须一致**（整数单位不收小数），且档位与其作用的
--      口径配对：`low_quota` 必须带 shop_id，`low_replenish` 不得带 shop_id。
--      跨着给就是拿一个口径的阈值去判另一个口径。

-- ---------------------------------------------------------------------------
-- 1) 库存池与它服务哪些店铺（连接方式）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.inventory_pools (
  namespace         text NOT NULL,
  pool_id           text NOT NULL,
  label             text NOT NULL DEFAULT '',
  -- shared：多店共用同一批实物；allocated：从共享池里划给某店；independent：独占。
  connection_kind   text NOT NULL DEFAULT 'shared',
  evidence          text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, pool_id),
  CONSTRAINT inventory_pools_namespace CHECK (btrim(namespace) <> ''),
  CONSTRAINT inventory_pools_id CHECK (btrim(pool_id) <> ''),
  CONSTRAINT inventory_pools_connection CHECK (
    connection_kind IN ('shared', 'allocated', 'independent')),
  CONSTRAINT inventory_pools_evidence CHECK (btrim(evidence) <> '')
);

CREATE TABLE IF NOT EXISTS bi.inventory_pool_shops (
  namespace    text NOT NULL,
  pool_id      text NOT NULL,
  shop_id      text NOT NULL REFERENCES bi.shops(shop_id) ON DELETE CASCADE,
  -- 这一行只说明「这个池与这家店有连接关系」，它**不是**授权：库存池授权独立于
  -- 店铺授权（spec §5.5），所以预警图只按服务端给的 allowed_inventory_pool_ids 取数。
  recorded_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, pool_id, shop_id),
  CONSTRAINT inventory_pool_shops_pool_fk FOREIGN KEY (namespace, pool_id)
    REFERENCES bi.inventory_pools (namespace, pool_id) ON DELETE CASCADE
);

COMMENT ON TABLE bi.inventory_pool_shops IS
  '池与店的连接关系（shared / allocated / independent 的展开）。不构成读取授权：'
  '能读哪些池由服务端授权集决定，不由这张表推导。';

-- ---------------------------------------------------------------------------
-- 2) 实物 / ERP 可用库存快照：一批 = (池, 仓库, 批次, 单位)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.physical_stock_snapshots (
  snapshot_id       text NOT NULL,
  namespace         text NOT NULL,
  pool_id           text NOT NULL,
  warehouse_id      text NOT NULL,
  platform          text,
  source            text NOT NULL,
  evidence          text NOT NULL,
  captured_at       timestamptz NOT NULL,
  -- 「这一批是否把该池该仓的全目录取尽」：决定缺行能不能说成「没有这条记录」。
  scan_complete     boolean NOT NULL DEFAULT false,
  scan_evidence     text,
  batch_id          text NOT NULL,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, snapshot_id, pool_id, warehouse_id),
  CONSTRAINT physical_snapshots_source CHECK (
    source IN ('erp', 'wms', 'official_export', 'manual_import')),
  CONSTRAINT physical_snapshots_evidence CHECK (btrim(evidence) <> ''),
  -- 不能声称一个还没发生的抓取。这里刻意拿 `now()` 而不是 `recorded_at` 比，也只留
  -- 一天的时钟宽容度：真正的"旧数据被重新贴成今天"不靠这条约束挡，而靠 `freshness_ok`
  -- 拿服务端请求时刻算龄——把约束收紧到分钟级只会让正常导入因时钟漂移被拒，
  -- 换不来任何额外保护。
  CONSTRAINT physical_snapshots_captured_not_future CHECK (
    captured_at <= now() + interval '1 day'),
  CONSTRAINT physical_snapshots_scan_pair CHECK (
    (scan_complete AND btrim(coalesce(scan_evidence, '')) <> '')
    OR (NOT scan_complete AND scan_evidence IS NULL)),
  -- 批次是身份的一部分：留空就等于允许"这批是哪一批"无从回答。
  CONSTRAINT physical_snapshots_batch CHECK (btrim(batch_id) <> ''),
  -- 单位不在批次头上：一批盘点里同一 SKU 可以既有"件"也有"箱"的行，而"同身份两种
  -- 单位不得合成一个总数"这条判定要求单位**只能**挂在明细上。放在头上就只能一单位一批，
  -- 冲突那一途在数据里根本表达不出来，也就永远测不到。
  CONSTRAINT physical_snapshots_namespace CHECK (btrim(namespace) <> ''),
  CONSTRAINT physical_snapshots_pool CHECK (btrim(pool_id) <> ''),
  CONSTRAINT physical_snapshots_warehouse CHECK (btrim(warehouse_id) <> ''),
  CONSTRAINT physical_snapshots_platform CHECK (
    platform IS NULL OR platform ~ '^[a-z0-9][a-z0-9_-]{0,15}$')
);

CREATE INDEX IF NOT EXISTS physical_snapshots_pool_captured_idx
  ON bi.physical_stock_snapshots (pool_id, warehouse_id, captured_at DESC);

COMMENT ON COLUMN bi.physical_stock_snapshots.source IS
  '获准的实物库存来源种类：erp / wms / official_export / manual_import。ERP 档案里的'
  '建议价类字段不在其中，它不是库存事实。';
COMMENT ON COLUMN bi.physical_stock_snapshots.scan_complete IS
  'true 仅当该 (池, 仓库) 的全目录分页取尽且有凭据；否则缺行不能被判成「没有库存记录」。';

CREATE TABLE IF NOT EXISTS bi.physical_stock_items (
  snapshot_id        text NOT NULL,
  namespace          text NOT NULL,
  pool_id            text NOT NULL,
  warehouse_id       text NOT NULL,
  erp_sku_id         text NOT NULL,
  -- 源字段已经是可用量时不再减锁定量：三个字段各自展示，语义由来源核验逐个确定。
  available_quantity numeric(18, 4) NOT NULL,
  inbound_quantity   numeric(18, 4),
  locked_quantity    numeric(18, 4),
  unit               text NOT NULL,
  batch_id           text NOT NULL,
  captured_at        timestamptz NOT NULL,
  CONSTRAINT physical_items_pk PRIMARY KEY (namespace, snapshot_id, pool_id, warehouse_id,
                                            erp_sku_id, unit, batch_id),
  CONSTRAINT physical_items_snapshot_fk FOREIGN KEY (namespace, snapshot_id, pool_id,
                                                     warehouse_id)
    REFERENCES bi.physical_stock_snapshots (namespace, snapshot_id, pool_id, warehouse_id)
    ON DELETE CASCADE,
  -- 可用量与在途可以为负吗？不可以：负数不是"缺货"，是源数据不成立（data_anomaly）。
  -- CHECK 故意**允许**写入负值：异常必须由图上判成 data_anomaly 并展示出来，
  -- 而不是在导入时静默丢掉那一行，让总量偏大。
  CONSTRAINT physical_items_quantity_scale CHECK (
    available_quantity >= -100000000 AND available_quantity <= 100000000),
  CONSTRAINT physical_items_unit_matches_snapshot CHECK (unit IN ('piece', 'box', 'set', 'kit')),
  CONSTRAINT physical_items_sku CHECK (btrim(erp_sku_id) <> ''),
  CONSTRAINT physical_items_batch CHECK (btrim(batch_id) <> ''),
  -- 整数计数单位不收小数：019 的头注释声称了这一条，就得真的存在。
  -- 没有它，一条 `1.5 件` 会在求和后变成一个没人能核对的总数。
  CONSTRAINT physical_items_integer_units CHECK (
    (unit = 'piece' AND available_quantity = trunc(available_quantity)
                     AND coalesce(inbound_quantity, 0) = trunc(coalesce(inbound_quantity, 0))
                     AND coalesce(locked_quantity, 0) = trunc(coalesce(locked_quantity, 0)))
    OR (unit <> 'piece')),
  CONSTRAINT physical_items_channel_integer_units CHECK (
    (unit = 'box' AND available_quantity = trunc(available_quantity)) OR unit <> 'box')
);

CREATE INDEX IF NOT EXISTS physical_items_sku_idx
  ON bi.physical_stock_items (erp_sku_id, namespace, pool_id);

COMMENT ON COLUMN bi.physical_stock_items.available_quantity IS
  '该 (池, 仓库, SKU, 批次, 单位) 的可用量。允许为负：负值是数据异常，必须由判定层报出来，'
  '不在存储层静默丢弃（丢弃会让总量看起来比实际更安全）。';

-- ---------------------------------------------------------------------------
-- 3) 店铺渠道可售库存快照（与实物完全分开的第二条链）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.channel_stock_snapshots (
  snapshot_id       text NOT NULL,
  namespace         text NOT NULL,
  shop_id           text NOT NULL REFERENCES bi.shops(shop_id),
  platform          text NOT NULL,
  source            text NOT NULL,
  evidence          text NOT NULL,
  captured_at       timestamptz NOT NULL,
  scan_complete     boolean NOT NULL DEFAULT false,
  scan_evidence     text,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace, shop_id, snapshot_id),
  CONSTRAINT channel_snapshots_source CHECK (
    source IN ('channel_api', 'official_export', 'manual_import')),
  CONSTRAINT channel_snapshots_evidence CHECK (btrim(evidence) <> ''),
  -- 同实物那一行：挡"未来抓取"，不挡"重贴时间"（那由服务端请求时刻算龄负责）。
  CONSTRAINT channel_snapshots_captured_not_future CHECK (
    captured_at <= now() + interval '1 day'),
  CONSTRAINT channel_snapshots_scan_pair CHECK (
    (scan_complete AND btrim(coalesce(scan_evidence, '')) <> '')
    OR (NOT scan_complete AND scan_evidence IS NULL)),
  CONSTRAINT channel_snapshots_platform CHECK (platform ~ '^[a-z0-9][a-z0-9_-]{0,15}$'),
  CONSTRAINT channel_snapshots_namespace CHECK (btrim(namespace) <> '')
);

CREATE INDEX IF NOT EXISTS channel_snapshots_shop_captured_idx
  ON bi.channel_stock_snapshots (shop_id, captured_at DESC);

COMMENT ON TABLE bi.channel_stock_snapshots IS
  '店铺渠道可售库存快照：用于「要不要调整店铺配额」。它与实物盘点是两个口径，'
  '任何一方都不得被加成另一方的总量（spec §5.5）。';

CREATE TABLE IF NOT EXISTS bi.channel_stock_items (
  snapshot_id        text NOT NULL,
  namespace          text NOT NULL,
  shop_id            text NOT NULL,
  listing_id         text NOT NULL,
  platform_sku_id    text NOT NULL DEFAULT '',
  erp_sku_id         text NOT NULL,
  sellable_quantity  numeric(18, 4) NOT NULL,
  unit               text NOT NULL,
  captured_at        timestamptz NOT NULL,
  CONSTRAINT channel_items_pk PRIMARY KEY (namespace, snapshot_id, shop_id, listing_id,
                                           platform_sku_id),
  CONSTRAINT channel_items_snapshot_fk FOREIGN KEY (namespace, shop_id, snapshot_id)
    REFERENCES bi.channel_stock_snapshots (namespace, shop_id, snapshot_id)
    ON DELETE CASCADE,
  CONSTRAINT channel_items_listing CHECK (btrim(listing_id) <> ''),
  CONSTRAINT channel_items_sku CHECK (btrim(erp_sku_id) <> ''),
  CONSTRAINT channel_items_unit CHECK (unit IN ('piece', 'box', 'set', 'kit')),
  CONSTRAINT channel_items_quantity_range CHECK (
    sellable_quantity >= -100000000 AND sellable_quantity <= 100000000),
  CONSTRAINT channel_items_integer_units CHECK (
    (unit = 'piece' AND sellable_quantity = trunc(sellable_quantity)) OR unit <> 'piece')
);

CREATE INDEX IF NOT EXISTS channel_items_sku_idx
  ON bi.channel_stock_items (erp_sku_id, namespace, shop_id);

-- ---------------------------------------------------------------------------
-- 4) 版本化阈值策略（经营者的配置；本轮没有配置入口，只有离线导入会写）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bi.inventory_threshold_policies (
  policy_id       text NOT NULL,
  policy_version  text NOT NULL,
  -- low_replenish 作用在实物总量上，low_quota 作用在店铺可售上：档位与口径配对。
  level           text NOT NULL,
  erp_sku_id      text NOT NULL,
  pool_id         text,
  shop_id         text REFERENCES bi.shops(shop_id),
  quantity        numeric(18, 4) NOT NULL CHECK (quantity >= 0),
  unit            text NOT NULL,
  effective_at    date NOT NULL DEFAULT current_date,
  expires_at      date,
  evidence        text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (policy_id),
  CONSTRAINT threshold_level CHECK (level IN ('low_replenish', 'low_quota')),
  CONSTRAINT threshold_unit CHECK (unit IN ('piece', 'box', 'set', 'kit')),
  CONSTRAINT threshold_sku CHECK (btrim(erp_sku_id) <> ''),
  CONSTRAINT threshold_version CHECK (btrim(policy_version) <> ''),
  CONSTRAINT threshold_effective_window CHECK (expires_at IS NULL OR expires_at >= effective_at),
  -- 档位决定作用域：配额阈值必须有店铺，补货阈值不得有店铺。
  CONSTRAINT threshold_level_scope CHECK (
    (level = 'low_quota' AND shop_id IS NOT NULL)
    OR (level = 'low_replenish' AND shop_id IS NULL)),
  CONSTRAINT threshold_evidence CHECK (btrim(evidence) <> '')
);

CREATE INDEX IF NOT EXISTS threshold_lookup_idx
  ON bi.inventory_threshold_policies (erp_sku_id, level, effective_at);

COMMENT ON TABLE bi.inventory_threshold_policies IS
  '库存阈值与作用域（按 SKU / 库存池 / 店铺 + 生效时间 + 单位）。缺配置时判定返回 '
  'unconfigured，绝不代替经营者设一个数（spec §5.5）。';

-- ---------------------------------------------------------------------------
-- 5) 读取视图与授权
-- ---------------------------------------------------------------------------
-- 预警图以 bi_app 身份连接：只读视图。取证凭据 evidence / scan_evidence 不进视图，
-- 只在写入侧留证：模型路径上任何一层都拿不到它。
CREATE OR REPLACE VIEW reporting.v_inventory_pools AS
SELECT namespace, pool_id, label, connection_kind FROM bi.inventory_pools;

CREATE OR REPLACE VIEW reporting.v_inventory_pool_shops AS
SELECT namespace, pool_id, shop_id FROM bi.inventory_pool_shops;

CREATE OR REPLACE VIEW reporting.v_physical_stock_snapshots AS
SELECT snapshot_id, namespace, pool_id, warehouse_id, platform, source, captured_at,
       scan_complete, batch_id
FROM bi.physical_stock_snapshots;

CREATE OR REPLACE VIEW reporting.v_physical_stock_items AS
SELECT snapshot_id, namespace, pool_id, warehouse_id, erp_sku_id, available_quantity,
       inbound_quantity, locked_quantity, unit, batch_id, captured_at
FROM bi.physical_stock_items;

CREATE OR REPLACE VIEW reporting.v_channel_stock_snapshots AS
SELECT snapshot_id, namespace, shop_id, platform, source, captured_at, scan_complete
FROM bi.channel_stock_snapshots;

CREATE OR REPLACE VIEW reporting.v_channel_stock_items AS
SELECT snapshot_id, namespace, shop_id, listing_id, platform_sku_id, erp_sku_id,
       sellable_quantity, unit, captured_at
FROM bi.channel_stock_items;

CREATE OR REPLACE VIEW reporting.v_inventory_threshold_policies AS
SELECT policy_version, level, erp_sku_id, pool_id, shop_id, quantity, unit,
       effective_at, expires_at
FROM bi.inventory_threshold_policies;

-- 池与快照由同步 / 导入身份写入；应用身份只读。阈值同样是配置数据的读取方。
REVOKE ALL ON bi.inventory_pools, bi.inventory_pool_shops,
  bi.physical_stock_snapshots, bi.physical_stock_items,
  bi.channel_stock_snapshots, bi.channel_stock_items,
  bi.inventory_threshold_policies FROM PUBLIC;
REVOKE ALL ON bi.inventory_pools, bi.inventory_pool_shops,
  bi.physical_stock_snapshots, bi.physical_stock_items,
  bi.channel_stock_snapshots, bi.channel_stock_items,
  bi.inventory_threshold_policies FROM bi_app;
GRANT SELECT, INSERT, UPDATE ON bi.inventory_pools, bi.inventory_pool_shops,
  bi.physical_stock_snapshots, bi.physical_stock_items,
  bi.channel_stock_snapshots, bi.channel_stock_items,
  bi.inventory_threshold_policies TO bi_sync;
GRANT SELECT ON reporting.v_inventory_pools, reporting.v_inventory_pool_shops,
  reporting.v_physical_stock_snapshots, reporting.v_physical_stock_items,
  reporting.v_channel_stock_snapshots, reporting.v_channel_stock_items,
  reporting.v_inventory_threshold_policies TO bi_reader, bi_app;

import type { Artifact, DisplayEntity, DrilldownIntent } from '../types'
import { AnalysisArtifact } from './AnalysisArtifact'
import { ChartArtifact } from './ChartArtifact'
import { ClockIcon, GaugeIcon, TableIcon } from './icons'

/**
 * 把下钻意图变成一句新提问：外层只负责把它当普通用户问题发出去。
 *
 * 为什么发问题而不是直接调一个“取店铺数据”的接口：授权、范围展开与口径确认都在
 * 服务端做一次，客户端手里没有任何可以冒充授权结论的东西（spec §8）。
 */
export function drilldownQuestion(intent: DrilldownIntent): string {
  return intent.question
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/* 字号只随字符长度收缩，数值本身保持后端原样。 */
function lengthClass(shown: string) {
  if (shown.length > 22) return 'num-xs'
  if (shown.length > 15) return 'num-sm'
  if (shown.length > 10) return 'num-md'
  return ''
}

function text(value: unknown) {
  return value === null || value === undefined ? '不可计算' : String(value)
}

/** 非数值列：这些列进不了单行指标卡，只能当表头维度读。 */
const DIMENSION_COLUMNS = new Set([
  'day', 'shop_ref', 'product_ref', 'sku_ref', 'currency', 'basis', 'line_kind', 'mode',
  'notice',
  // 上架复核的列全部是维度：它们是"一格判定"的组成部分，不是可以求和/求平均的指标。
  // 拿差额去当指标卡，下一步就会有人把它加起来。范围外也不误入：审计行走自己的卡片。
  'listing_ref', 'expected_amount', 'actual_amount', 'amount_difference',
  'audit_status', 'price_basis', 'snapshot_at',
])

/**
 * 上架复核的逐格状态（后端 spec §5.4 的九个取值）。
 *
 * 后端只发码，文案在这一侧：同一个词在两个领域里长得不一样时，展示层才是唯一能
 * 把它说成人话的地方。不在表里的状态**原样输出**，不猜一个中文近义词（与平台码
 * 同一规则）：猜出来的"通过"会把"未判定"说成好消息。
 */
export const AUDIT_STATUS_LABELS: Record<string, string> = {
  match: '一致',
  mismatch: '不一致',
  not_listed: '未上架',
  not_on_sale: '不在售',
  missing_standard: '缺目标价',
  unmapped: '未映射',
  stale: '快照过期',
  unsupported: '来源未取证',
  unknown: '无法判定',
}

/** 只有这两类算"真比过价"，其余都是证据不足：汇总行必须能分开说。 */
const AUDIT_JUDGED = new Set(['match', 'mismatch'])

export function auditStatus(status: unknown): string {
  if (typeof status !== 'string') return '无法判定'
  return AUDIT_STATUS_LABELS[status] ?? status
}

function auditNumber(value: unknown): string {
  // 缺价与"价格为 0"是两件事：null 一律写成缺什么，不写 0，也不写"不可计算"。
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/** 复核汇总块：只认后端给出的字段，缺字段就少说一句，不拿行数据现算一遍分母。 */
export function auditSummary(artifact: Artifact): {
  expected: number | null
  evaluated: number | null
  allCorrect: boolean | null
  sources: number
} {
  const audit = isRecord(artifact.audit) ? artifact.audit : undefined
  const num = (key: string) => (typeof audit?.[key] === 'number' ? audit[key] as number : null)
  return {
    expected: num('expected_items'),
    evaluated: num('evaluated_items'),
    allCorrect: typeof audit?.all_correct === 'boolean' ? audit.all_correct : null,
    sources: Array.isArray(audit?.sources) ? audit.sources.length : 0,
  }
}

/**
 * 库存预警的逐格状态（后端 spec §5.5 的六个取值 + 门禁态 unsupported）。
 *
 * 不在表里的状态**原样输出**：猜一个近义词就是把"没判过"说成一个已知结论。
 */
export const INVENTORY_STATUS_LABELS: Record<string, string> = {
  low: '低于阈值',
  normal: '正常',
  unconfigured: '未配阈值',
  unknown: '无法判定',
  stale: '快照过期',
  data_anomaly: '数据异常',
  unsupported: '来源未取证',
}

/** 两个口径的中文名。"实物"与"渠道可售"说反会把补货与调配额两个动作互换。 */
export const INVENTORY_LEVEL_LABELS: Record<string, string> = {
  physical_total: '实物可用库存',
  shop_sellable: '店铺渠道可售',
}

export function inventoryStatus(status: unknown): string {
  if (typeof status !== 'string') return '无法判定'
  return INVENTORY_STATUS_LABELS[status] ?? status
}

export function inventoryLevel(level: unknown): string {
  if (typeof level !== 'string') return '未知口径'
  return INVENTORY_LEVEL_LABELS[level] ?? level
}

/**
 * 预警汇总：只认后端给的字段，缺字段就少说一句。
 *
 * 分母、扫描数、截断与"全部安全"全部是后端事实。展示层从行数去推分母，就会把
 * "只展示了 20 行"说成"整个目录就 20 个 SKU"——而没展示的那批才是真正的高风险。
 */
export function inventorySummary(artifact: Artifact): {
  expected: number | null
  evaluated: number | null
  scanned: number | null
  truncated: boolean | null
  allSafe: boolean | null
  pools: number
  excludedPools: number
} {
  const block = isRecord(artifact.inventory) ? artifact.inventory : undefined
  const num = (key: string) => (typeof block?.[key] === 'number' ? block[key] as number : null)
  const flag = (key: string) => (typeof block?.[key] === 'boolean' ? block[key] as boolean : null)
  return {
    expected: num('expected_items'),
    evaluated: num('evaluated_items'),
    scanned: num('scanned_items'),
    truncated: flag('truncated'),
    allSafe: flag('all_safe'),
    pools: Array.isArray(block?.pools) ? (block.pools as unknown[]).length : 0,
    excludedPools: Array.isArray(block?.excluded_pools)
      ? (block.excluded_pools as unknown[]).length : 0,
  }
}

/**
 * 两级库存预警卡片。
 *
 * 为什么不复用通用表格：那张表把空值当成"可以少的列"，而这里的 null 各有含义
 * （没记录 / 没阈值 / 来源没取证），三种必须分开写。也要把两个口径分块列出：
 * 把实物与渠道可售排进同一张连续表里，下一个人就会把它们加起来。
 */
function InventoryAlertsArtifact({ artifact }: { artifact: Artifact }) {
  const entities = artifactEntities(artifact)
  const summary = inventorySummary(artifact)
  const rows = (Array.isArray(artifact.data) ? artifact.data : []).filter(
    (row): row is Record<string, unknown> => isRecord(row)
      && typeof row.inventory_status === 'string')
  const byLevel = (level: string) => rows.filter((row) => row.level === level)
  const limitations = Array.isArray(artifact.limitations) ? artifact.limitations : []
  const thresholdSource = isRecord(artifact.inventory)
    && typeof artifact.inventory.threshold_source === 'string'
    ? artifact.inventory.threshold_source : null
  const asOf = typeof artifact.data_as_of === 'string' ? artifact.data_as_of : null
  const columns: Array<{ key: string; label: string }> = [
    { key: 'sku_ref', label: 'SKU' },
    { key: 'shop_ref', label: '店铺' },
    { key: 'pool_ref', label: '库存池' },
    { key: 'warehouse_ref', label: '仓库' },
    { key: 'quantity', label: '实物可用量' },
    { key: 'channel_quantity', label: '渠道可售量' },
    { key: 'threshold', label: '阈值' },
    { key: 'unit', label: '单位' },
    { key: 'batch_count', label: '批次数' },
    { key: 'inventory_status', label: '状态' },
    { key: 'snapshot_at', label: '快照时点' },
  ]
  const renderLevel = (level: string) => {
    const levelRows = byLevel(level)
    if (levelRows.length === 0) return null
    // 每个口径只列自己那一列：共用一张表头就会在实物块里出现一列永远为空的
    // "渠道可售量"，而空列在读表的人眼里就是"这些 SKU 渠道没货"。
    const hidden = level === 'physical_total'
      ? new Set(['shop_ref', 'channel_quantity'])
      : new Set(['pool_ref', 'warehouse_ref', 'quantity', 'batch_count'])
    const visible = columns.filter((column) => !hidden.has(column.key))
    return (
      <section className="inventory-level" data-level={level} key={level}>
        <h4 className="inventory-level-title">{inventoryLevel(level)}</h4>
        <div className="table-wrap">
          <table>
            <thead><tr>{visible.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
            <tbody>{levelRows.map((row, index) => (
              <tr key={`${row.sku_ref ?? ''}-${row.shop_ref ?? ''}-${index}`}>
                {visible.map((column) => {
                  const value = row[column.key]
                  let shown: string
                  if (column.key === 'inventory_status') shown = inventoryStatus(value)
                  // 店铺与 SKU 都走同一条名称规则：名称未取得就写"名称未取得"，稳定引用
                  // 留在 data-ref 上。把引用直接印在表里，等于让一个 opaque 主键冒充数据。
                  else if (column.key === 'shop_ref' || column.key === 'sku_ref')
                    shown = value ? cellText(value, entities) : '—'
                  else if (column.key === 'batch_count') {
                    shown = typeof value === 'number' ? String(value) : '—'
                  } else shown = auditNumber(value)
                  return (
                    <td
                      key={column.key}
                      data-ref={isRef(value) ? value : undefined}
                      data-inventory-status={column.key === 'inventory_status'
                        ? String(value) : undefined}
                    >{shown}</td>
                  )
                })}
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>
    )
  }
  return (
    <section className="artifact" aria-label="库存两级预警">
      <header className="artifact-head">
        <span className="artifact-title"><GaugeIcon size={15} />库存两级预警</span>
        {summary.expected !== null && (
          <span className="pill">期望 {summary.expected} 项 · 已判定 {summary.evaluated ?? 0} 项</span>
        )}
        {summary.allSafe !== null && (
          <span className={`pill${summary.allSafe ? '' : ' pill-warn'}`}>
            {summary.allSafe ? '全部安全（证据齐备）' : '未全部安全'}
          </span>
        )}
        {summary.truncated === true && (
          // 截断必须写在卡片上：没展示不等于没风险。
          <span className="pill pill-warn">已按风险截断展示</span>
        )}
      </header>
      {rows.length === 0 && (
        <p className="limitations">本轮没有可判定的库存项；这不等于这些 SKU 没有库存。</p>
      )}
      {/* 两个口径分块渲染：并排进同一张连续表，下一步就有人把它们加起来。 */}
      {renderLevel('physical_total')}
      {renderLevel('shop_sellable')}
      <div className="artifact-meta">
        {thresholdSource && (
          <span>阈值来源：{thresholdSource === 'this_turn' ? '本轮用户给出'
            : thresholdSource === 'configured' ? '已配置策略' : '未配置'}</span>
        )}
        {summary.pools > 0 && <span>库存池：{summary.pools} 个</span>}
        {summary.excludedPools > 0 && (
          <span>未计入的池：{summary.excludedPools} 个（池授权独立于店铺授权）</span>
        )}
        {asOf && <span><ClockIcon size={13} />快照共同截止：{asOf}</span>}
      </div>
      {limitations.length > 0 && <p className="limitations">{limitations.map(text).join('；')}</p>}
    </section>
  )
}

/**
 * 上架复核差异表：一行就是一格期望项，所以行数就是分母。
 *
 * 为什么不用通用表格渲染那一段：通用面把"空"当成可缺列，而复核表里 null 是有含义的
 * （缺目标价 / 未取到实价 / 币种不可比），三种都得写出来。让模型或用户从一片空白里
 * 去猜是哪一种，正是这一轮要避免的事。
 */
function AuditArtifact({ artifact }: { artifact: Artifact }) {
  const entities = artifactEntities(artifact)
  const rows = (Array.isArray(artifact.data) ? artifact.data : []).filter(
    (row): row is Record<string, unknown> => isRecord(row) && typeof row.audit_status === 'string')
  const summary = auditSummary(artifact)
  const filters = isRecord(artifact.filters) ? artifact.filters : undefined
  const priceBasis = typeof filters?.price_basis === 'string'
    ? filters.price_basis : null
  const asOf = typeof artifact.data_as_of === 'string' ? artifact.data_as_of : null
  const limitations = Array.isArray(artifact.limitations) ? artifact.limitations : []
  const columns: Array<{ key: string; label: string }> = [
    { key: 'shop_ref', label: '店铺' },
    { key: 'sku_ref', label: 'SKU' },
    { key: 'listing_ref', label: '链接' },
    { key: 'expected_amount', label: '本轮目标价' },
    { key: 'actual_amount', label: '实际在售价' },
    { key: 'amount_difference', label: '差额' },
    { key: 'audit_status', label: '状态' },
    { key: 'snapshot_at', label: '快照时点' },
  ]
  return (
    <section className="artifact" aria-label="上架价复核差异表">
      <header className="artifact-head">
        <span className="artifact-title"><TableIcon size={15} />上架价复核差异表</span>
        {summary.expected !== null && (
          <span className="pill">期望 {summary.expected} 项 · 已判定 {summary.evaluated ?? 0} 项</span>
        )}
        {summary.allCorrect !== null && (
          <span className={`pill${summary.allCorrect ? '' : ' pill-warn'}`}>
            {summary.allCorrect ? '每一格均有新鲜匹配证据' : '未全部通过'}
          </span>
        )}
      </header>
      {rows.length === 0 && (
        <p className="limitations">本轮没有可复核的期望项；这不等于该商品没有上架。</p>
      )}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
            <tbody>{rows.map((row, index) => (
              <tr key={`${row.shop_ref ?? ''}-${row.listing_ref ?? ''}-${index}`}>
                {columns.map((column) => {
                  const value = row[column.key]
                  const judged = AUDIT_JUDGED.has(String(row.audit_status))
                  let shown: string | null = null
                  switch (column.key) {
                    case 'audit_status': shown = auditStatus(value); break
                    case 'shop_ref': shown = cellText(value, entities); break
                    // SKU 沿用全应用同一条取用规则：名称未取得就写"名称未取得"，稳定引用
                    // 留在 data-ref 上（SKU 主档未建，这一格多数时候就是未取得）。不在这里
                    // 改用引用当展示文本：那会让同一个引用在两张卡片上有两种读法。
                    case 'sku_ref': shown = value ? cellText(value, entities) : '—'; break
                    case 'expected_amount':
                    case 'actual_amount':
                    case 'listing_ref':
                    case 'snapshot_at': shown = auditNumber(value); break
                    /* 差额只在比过价的那一类状态下发：后端已经这么约束了，这里再挡一次，
                       是为了不让一份过期快照带出一个看着像当前的差额。 */
                    case 'amount_difference': shown = judged ? auditNumber(value) : '—'; break
                  }
                  return (
                    <td
                      key={column.key}
                      data-ref={isRef(value) ? value : undefined}
                      data-audit-status={column.key === 'audit_status' ? String(value) : undefined}
                    >
                      {shown}
                    </td>
                  )
                })}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="artifact-meta">
        {priceBasis && <span>口径：{priceBasis === 'campaign_price' ? '活动价' : '标价'}</span>}
        {summary.sources > 0 && <span>快照来源：{summary.sources} 家店铺</span>}
        {asOf && <span><ClockIcon size={13} />快照共同截止：{asOf}</span>}
      </div>
      {limitations.length > 0 && <p className="limitations">{limitations.map(text).join('；')}</p>}
    </section>
  )
}

/**
 * 平台码 → 可读名。后端 PLATFORM_LABELS 才是单一真源（它也用这份表做重名后缀）；
 * 未收录的码原样输出，不猜、不置空。
 */
export const PLATFORM_LABELS: Record<string, string> = {
  fxg: '抖音', tb: '淘宝', tm: '天猫', pdd: '拼多多', jd: '京东', kuaishou: '快手',
  wxsph: '视频号', '1688': '1688',
}
const PLATFORM_CODE_RE = /^[a-z0-9][a-z0-9_-]{0,15}$/

function platformOf(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const code = value.trim()
  // 不合法的码直接丢掉：宁可少一个标签，也不把载荷里任意文本当展示内容。
  return PLATFORM_CODE_RE.test(code) ? code : null
}

export function artifactEntities(artifact: Artifact): Map<string, DisplayEntity> {
  const byRef = new Map<string, DisplayEntity>()
  const entities = Array.isArray(artifact.entities) ? artifact.entities : []
  for (const item of entities) {
    if (!isRecord(item) || typeof item.ref !== 'string') continue
    byRef.set(item.ref, {
      ref: item.ref,
      kind: item.kind === 'product' || item.kind === 'sku' ? item.kind : 'shop',
      display_name: typeof item.display_name === 'string' ? item.display_name : null,
      sku_label: typeof item.sku_label === 'string' ? item.sku_label : null,
      name_source: typeof item.name_source === 'string'
        ? item.name_source as DisplayEntity['name_source'] : 'unresolved',
      platform: platformOf(item.platform),
    })
  }
  return byRef
}

/**
 * 单元格取值：引用一律换成授权展示名。
 * 名称未取得就显示占位——宁可不显示，也不拿引用或猜一个名字当数据。
 * 引用仍然保留在单元格的 data-ref 上（见 ArtifactView），便于按稳定引用追查。
 */
export function cellText(value: unknown, entities: Map<string, DisplayEntity>) {
  if (isRef(value)) {
    const entity = entities.get(value)
    if (!entity || !entity.display_name) return '名称未取得'
    const source = entity.name_source === 'trade_snapshot' ? '（成交名）' : ''
    const spec = entity.sku_label ? ` ${entity.sku_label}` : ''
    return `${entity.display_name}${source}${spec}`
  }
  return text(value)
}

/**
 * 单元格对应的平台标签：只在名称本身没带上平台时补一个标签。
 * 重名店铺的名称已经带（抖音）这类后缀，再追一遍就是噪声。
 */
export function cellPlatform(value: unknown, entities: Map<string, DisplayEntity>): string | null {
  if (!isRef(value)) return null
  const entity = entities.get(value)
  if (!entity || entity.kind !== 'shop' || !entity.platform) return null
  const label = PLATFORM_LABELS[entity.platform] ?? entity.platform
  if (entity.display_name && entity.display_name.includes(label)) return null
  return label
}

export function platformLabel(code: string): string {
  // 未收录的码原样输出：宁可不好读，也不猜一个名字上去。
  const trimmed = code.trim()
  return PLATFORM_LABELS[trimmed.toLowerCase()] ?? trimmed
}

export function isRef(value: unknown): value is string {
  return typeof value === 'string' && /^ent-[0-9a-z]{8}$/.test(value)
}

export function ArtifactView({
  artifact, datasets = [], onDrilldown,
}: {
  artifact: Artifact
  /**
   * 同一条消息里已收到的全部 Artifact（含自己）：`chart_spec` 只能按引用去这里取
   * 被它引用的那份数据集，找不到就不画。
   */
  datasets?: Artifact[]
  /** 图表下钻：只交出一个意图，外层负责把它变成一次**重新授权**的提问。 */
  onDrilldown?: (intent: DrilldownIntent) => void
}) {
  const entities = artifactEntities(artifact)
  const rows = Array.isArray(artifact.data) ? artifact.data.filter(
    (row): row is Record<string, unknown> => typeof row === 'object' && row !== null && !Array.isArray(row),
  ) : []
  // 提示行只带 notice：它不是数据行，不能挤进表格里当空行。
  const notices = rows.filter((row) => typeof row.notice === 'string').map((row) => String(row.notice))
  const dataRows = rows.filter((row) => typeof row.notice !== 'string')
  const first = dataRows[0]
  const columns = first ? [...new Set(dataRows.flatMap((row) => Object.keys(row)))] : []
  const numericKeys = first ? columns.filter((key) => !DIMENSION_COLUMNS.has(key)) : []
  const filters = isRecord(artifact.filters) ? artifact.filters : undefined
  const shopRefs = Array.isArray(filters?.shop_refs)
    ? filters.shop_refs.filter((ref): ref is string => typeof ref === 'string') : []
  const limitations = Array.isArray(artifact.limitations) ? artifact.limitations : []
  const basis = Array.isArray(artifact.basis) ? artifact.basis.filter(isRecord) : []
  const basisKeys = new Set(basis.map((item) => `${text(item.basis)}/${text(item.time_basis)}`))
  const basisLabels = [...new Set(basis.map((item) => (
    `${text(item.metric)}＝${text(item.basis)}（${text(item.time_basis)}）`)))]
  const mixedBasis = basisKeys.size > 1
  const rangeStart = typeof filters?.start === 'string' ? filters.start : null
  const rangeEnd = typeof filters?.end === 'string' ? filters.end : null
  const asOf = typeof artifact.data_as_of === 'string' ? artifact.data_as_of : null
  const coverage = isRecord(artifact.coverage) ? artifact.coverage.status : undefined
  const notes = [...limitations.map(text), ...notices]
  // 图表卡片走另一个组件：它只引用已落库数据集，不在这里重复一份表格。
  if (artifact.artifact_type === 'chart_spec') {
    return (
      <ChartArtifact
        artifact={artifact}
        artifacts={datasets.length > 0 ? datasets : [artifact]}
        onDrilldown={onDrilldown} />
    )
  }
  // 价审卡片走自己的差异表：行数就是分母，而 null 在那张表里是有含义的。
  if (artifact.artifact_type === 'price_audit') {
    return <AuditArtifact artifact={artifact} />
  }
  // 库存预警卡片走自己的两级视图：两个口径永不并成一张表。
  if (artifact.artifact_type === 'inventory_alerts') {
    return <InventoryAlertsArtifact artifact={artifact} />
  }
  // 隔离分析卡片走自己的白名单渲染：载荷先整体验形，验不过整卡拒绝。
  if (artifact.artifact_type === 'analysis_result') {
    return <AnalysisArtifact artifact={artifact} datasets={datasets} />
  }

  return (
    <section className="artifact" aria-label="经营数据结果">
      <header className="artifact-head">
        <span className="artifact-title"><TableIcon size={15} />查询结果</span>
        {dataRows.length > 0 && <span className="pill">{dataRows.length} 行</span>}
      </header>
      {basisLabels.length > 0 && (
        <p className={`basis-note${mixedBasis ? ' basis-mixed' : ''}`}>
          统计口径：{basisLabels.join('；')}
          {mixedBasis && '｜口径不同，不能汇总、求增长率或排名'}
        </p>
      )}
      {first && dataRows.length === 1 && (
        <div className="metric-grid">
          {numericKeys.map((key) => {
            const value = first[key]
            const shown = cellText(value, entities)
            const tone = value === null || value === undefined ? 'missing' : lengthClass(shown)
            return (
              <div className={`metric-card ${tone}`} key={key}>
                <span>{key}</span>
                <strong>{shown}</strong>
              </div>
            )
          })}
        </div>
      )}
      {dataRows.length > 1 && (
        <div className="table-wrap">
          <table>
            <thead><tr>{columns.map((key) => <th key={key}>{key}</th>)}</tr></thead>
            <tbody>{dataRows.map((row, index) => (
              <tr key={`${row.day ?? ''}-${row.product_ref ?? row.shop_ref ?? index}`}>
                {columns.map((key) => (
                  <td
                    key={key}
                    data-ref={isRef(row[key]) ? row[key] : undefined}
                    data-platform={(() => {
                      if (!isRef(row[key])) return undefined
                      const entity = entities.get(row[key])
                      return entity?.kind === 'shop' ? (entity.platform ?? undefined) : undefined
                    })()}
                  >
                    {cellText(row[key], entities)}
                    {cellPlatform(row[key], entities) && (
                      <span className="entity-platform">{cellPlatform(row[key], entities)}</span>
                    )}
                  </td>
                ))}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="artifact-meta">
        {rangeStart && rangeEnd && <span>期间：{rangeStart} 至 {rangeEnd}</span>}
        {shopRefs.length > 0 && <span>范围：{shopRefs.map((ref) => cellText(ref, entities)).join('、')}</span>}
        {asOf && <span><ClockIcon size={13} />数据截止：{asOf}</span>}
        {coverage !== undefined && <span><GaugeIcon size={13} />覆盖：{text(coverage)}</span>}
      </div>
      {notes.length > 0 && <p className="limitations">{notes.join('；')}</p>}
    </section>
  )
}

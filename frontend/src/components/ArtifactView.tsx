import type { Artifact, DisplayEntity, DrilldownIntent } from '../types'
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
])

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

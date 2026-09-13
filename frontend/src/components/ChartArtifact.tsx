import type { Artifact, DrilldownIntent } from '../types'
import { artifactEntities, cellText, isRef, platformLabel } from './ArtifactView'

/**
 * 已落库数据集的图表渲染（计划 Task 8）。手写 SVG + CSS，不引入图表库。
 *
 * 四条硬规则，都来自 spec §8：
 *
 * 1. **只渲染，不重算**：数值、单位、口径与覆盖全部来自后端那份 `chart_spec` 与它
 *    引用到的那份数据集 Artifact。这里唯一的"算术"是把字符串换成像素位置；财务口径
 *    一律不在浏览器里重新推导（两份"权威数字"迟早对不上）。
 * 2. **引用不上就不画**：按 `dataset_ref` 在同一条消息的 Artifact 里找那份数据集，
 *    并要求 `dataset_type` 相同、`dataset_data_as_of` 指向**同一时刻**（按时间戳比，
 *    不比字符串：同一瞬间有两种合法写法）。找不到就说找不到，不拿"看着像"的凑图。
 * 3. **`series` 决定有几条线**：分组趋势数据集的行是"逐分组 × 逐日"，x 轴只有日期。
 *    不按 `series` 分列就会把甲组最后一天连到乙组第一天，画出一条数据里不存在的线。
 * 4. **下钻是一次新的授权请求**：点平台柱只交出一个 `DrilldownIntent`（平台码、窗口、
 *    口径、指标），由外层把它变成一次新的提问。组件本身没有任何店铺级数据，也没有
 *    "把平台数字拆成各店"的推算 —— 客户端没有可以冒充授权结果的东西。
 *
 * 图 / 表切换是纯 CSS 的单选切换：两份视图同时在 DOM 里，同一批数、同一种写法，
 * 切换只改可见性。这样"图与表不一致"是一件会被测试抓住的事，而不是运行时巧合。
 */

const NUMBER_RE = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/
const PLATFORM_CODE_RE = /^[a-z0-9][a-z0-9_-]{0,15}$/
const PLOT = { width: 480, height: 220, left: 56, right: 12, top: 18, bottom: 52 }
const BASELINE = PLOT.height - PLOT.bottom
/** 一条线最多画这几个系列：再多就只画前几个并在图例里说明，不静默丢数。 */
const MAX_SERIES = 6

type Resolved =
  | { kind: 'ok'; dataset: Artifact }
  | { kind: 'missing' }
  | { kind: 'mismatch'; reason: string }

export type Point = {
  key: string
  label: string
  entityRef?: string
  text: string
  num: number | null
  /** 系列键：`series` 与 x 不同列时是该列的原始值，否则是空串（单系列）。 */
  seriesKey: string
  seriesLabel: string
  seriesRef?: string
}

export type Series = { key: string; label: string; entityRef?: string }

/**
 * 按引用取回数据集。`artifact_id` 由服务端展示层投影补上（见后端
 * `business_query.tool.artifact_event_payload`），不是前端自己编号。
 */
export function resolveChartDataset(artifacts: Artifact[], spec: Artifact): Resolved {
  const ref = typeof spec.dataset_ref === 'string' ? spec.dataset_ref : null
  if (!ref) return { kind: 'missing' }
  const matches = artifacts.filter((item) => item.artifact_id === ref)
  if (matches.length !== 1) return { kind: 'missing' }
  const dataset = matches[0]
  if (typeof spec.dataset_type === 'string' && dataset.artifact_type !== spec.dataset_type) {
    // 类型不合就是拿错了对象：宁可不说，也不说错。
    return { kind: 'mismatch', reason: '数据集类型与图表声明不一致，未渲染' }
  }
  if (typeof spec.dataset_data_as_of === 'string'
    && !sameInstant(spec.dataset_data_as_of, dataset.data_as_of)) {
    return { kind: 'mismatch', reason: '数据集版本与图表声明不一致，未渲染' }
  }
  return { kind: 'ok', dataset }
}

/**
 * 同一瞬间的两种写法。
 *
 * 数据集那份 `data_as_of` 是 pydantic 序列化出来的，图表那份是后端 `isoformat()` 写进
 * 载荷的：UTC 时刻前者是 `...Z`、后者是 `...+00:00`。逐字比字符串会让每张图都静默
 * 变成"版本不合，未渲染"，所以这里按时间戳比，并同时要求两边都能被解析。
 */
export function sameInstant(declared: string, actual: unknown): boolean {
  if (typeof actual !== 'string') return false
  const left = Date.parse(declared)
  const right = Date.parse(actual)
  if (Number.isNaN(left) || Number.isNaN(right)) return false
  return left === right
}

function decimalOf(value: unknown): { text: string; num: number | null } {
  if (typeof value === 'string' && NUMBER_RE.test(value)) {
    return { text: value, num: Number(value) }
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return { text: String(value), num: value }
  }
  // null 或看不懂的形状都是"不可计算"：原样保留，绝不当成 0。
  return { text: '不可计算', num: null }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function rowsOf(dataset: Artifact): Record<string, unknown>[] {
  const rows = Array.isArray(dataset.data) ? dataset.data : []
  return rows.filter(isRecord)
}

/**
 * 参与画图的行：必须带 x 轴列。
 * 合计行不带分组键 —— 它属于表格，不属于比较轴；把它当一根柱子画进去，就是把
 * "合计"跟某个平台并列比较。提示行（只带 notice）同理不是数据。
 *
 * 系列列缺失时不丢行：`series` 声明的列在本份数据里一处都没有，那就按单系列画
 * （按声明把每一行都过滤掉会让图变成空的，那是把"我没看懂数据"伪装成"没有数据"）。
 * 个别行缺系列列时，那些行归入无标签系列，仍然照画照列，不静默消失。
 */
function plottedRows(dataset: Artifact, x: string): Record<string, unknown>[] {
  return rowsOf(dataset).filter((row) => {
    if (typeof row.notice === 'string') return false
    return row[x] !== undefined && row[x] !== null
  })
}

/** 分组轴上出现的列名由后端给；与 x 同列（店铺柱状图的 series=('shop_ref',)）就是单系列。 */
export function seriesColumnOf(spec: Artifact, x: string): string | null {
  const listed = Array.isArray(spec.series)
    ? spec.series.filter((item): item is string => typeof item === 'string') : []
  for (const column of listed) {
    if (column !== x) return column
  }
  return null
}

function labelFor(value: unknown, column: string, entities: Map<string, import('../types').DisplayEntity>) {
  const raw = String(value)
  if (column === 'platform') return { label: platformLabel(raw), entityRef: undefined }
  return { label: cellText(value, entities), entityRef: isRef(value) ? raw : undefined }
}

function pointsOf(spec: Artifact, dataset: Artifact, x: string, y: string): Point[] {
  let series = seriesColumnOf(spec, x)
  const rows = plottedRows(dataset, x)
  if (series !== null && !rows.some((row) => row[series as string] !== undefined
    && row[series as string] !== null)) {
    series = null
  }
  const entities = artifactEntities(dataset)
  return rows.map((row) => {
    const axis = labelFor(row[x], x, entities)
    const value = decimalOf(row[y])
    const seriesValue = series === null ? undefined : row[series]
    const named = seriesValue === undefined || seriesValue === null
      ? { key: '', label: '', entityRef: undefined }
      : { key: String(seriesValue), ...labelFor(seriesValue, series as string, entities) }
    return {
      key: axisLabelKey(row[x]),
      label: axis.label,
      entityRef: axis.entityRef,
      text: value.text,
      num: value.num,
      seriesKey: named.key,
      seriesLabel: named.label,
      seriesRef: named.entityRef,
    }
  })
}

/** x 轴刻度：按行序去重。分组趋势的行是"逐分组 × 逐日"，日期因此只能出现一次。 */
function axisTicks(points: Point[]): string[] {
  const seen: string[] = []
  for (const point of points) {
    if (!seen.includes(point.key)) seen.push(point.key)
  }
  return seen
}

function seriesList(points: Point[]): Series[] {
  const out: Series[] = []
  for (const point of points) {
    if (!out.some((item) => item.key === point.seriesKey)) {
      out.push({ key: point.seriesKey, label: point.seriesLabel, entityRef: point.seriesRef })
    }
  }
  return out
}

function axisLabelKey(value: unknown): string {
  return typeof value === 'string' ? value : String(value)
}

type Scale = { min: number; span: number; zeroY: number; top: string; bottom: string }

function scaleOf(points: Point[]): Scale {
  // 轴的两端只用**行里原样出现过**的数：自己四舍五入出一个"好看的顶"，就是
  // 在造一个谁的账上都数都不存在的数。
  // 负值必须自己占一段轴：商品毛利参考可以是真实的负数（卖得比成本高），
  // 只按正值缩放会把亏损画成贴着基线的 1px，看着像"几乎为 0"。
  let max = 0
  let min = 0
  let top = '0'
  let bottom = '0'
  for (const point of points) {
    if (point.num === null) continue
    if (point.num > max) {
      max = point.num
      top = point.text
    }
    if (point.num < min) {
      min = point.num
      bottom = point.text
    }
  }
  const span = max - min > 0 ? max - min : 1
  const usable = BASELINE - PLOT.top
  return {
    min,
    span,
    zeroY: BASELINE - ((0 - min) / span) * usable,
    top,
    bottom,
  }
}

function barSlot(index: number, count: number, seriesIndex: number, seriesCount: number) {
  const span = (PLOT.width - PLOT.left - PLOT.right) / Math.max(count, 1)
  const groupWidth = span * 0.7
  const width = Math.max(groupWidth / Math.max(seriesCount, 1) - 2, 4)
  const left = PLOT.left + span * index + (groupWidth - width * seriesCount) / 2
    + width * seriesIndex
  return { centre: left + width / 2, width, left }
}

function lineX(index: number, count: number) {
  const span = PLOT.width - PLOT.left - PLOT.right
  return PLOT.left + (count <= 1 ? span / 2 : (span * index) / (count - 1))
}

function valueY(value: number, scale: Scale) {
  const usable = BASELINE - PLOT.top
  return BASELINE - ((value - scale.min) / scale.span) * usable
}

/**
 * 平台柱上的下钻意图：只带"该重新问什么"。
 *
 * 问句里同时给出**已登记平台码**与人读名：模型上下文里只有码（服务端口径从不把
 * `PLATFORM_LABELS` 投给它），只发中文名的话 `normalize_platform` 只能 fail closed，
 * 一次下钻就会退化成一句"查询参数无效"。指标与口径也按签名原样写：中文"销售额"
 * 会命中主层的口径澄清，把本轮已确认的口径丢掉。
 */
export function drilldownIntent(
  spec: Artifact, dataset: Artifact, point: Point,
): DrilldownIntent | null {
  if (spec.kind !== 'bar' || spec.x !== 'platform') return null
  const code = point.key.trim().toLowerCase()
  if (!PLATFORM_CODE_RE.test(code)) return null
  const filters = isRecord(dataset.filters) ? dataset.filters : null
  const start = typeof filters?.start === 'string' ? filters.start : null
  const end = typeof filters?.end === 'string' ? filters.end : null
  if (!start || !end) return null
  const metric = typeof spec.y === 'string' ? spec.y : ''
  const basis = typeof spec.metric_basis === 'string' ? spec.metric_basis : ''
  if (!metric) return null
  return {
    platform: code,
    groupBy: 'shop',
    start,
    end,
    metric,
    metricBasis: basis,
    question: `${platformLabel(code)}（platform=${code}）各店铺对比：${start} 至 ${end}，`
      + `指标 ${metric}（口径 ${basis || '未标注'}）`,
  }
}

function axisTitle(spec: Artifact) {
  return `${String(spec.y ?? '')}（${String(spec.unit ?? '')}）｜`
    + `${String(spec.metric_basis ?? '')}`
}

export function ChartArtifact({
  artifact, artifacts, onDrilldown,
}: {
  artifact: Artifact
  /** 同一条消息里已收到的全部 Artifact（含自己）：图表按引用去这里取数据集。 */
  artifacts: Artifact[]
  /** 下钻：外层必须把它变成一次**重新授权**的新提问，而不是本地展开。 */
  onDrilldown?: (intent: DrilldownIntent) => void
}) {
  const resolved = resolveChartDataset(artifacts, artifact)
  const kind = typeof artifact.kind === 'string' ? artifact.kind : '未标注'
  const axis = axisTitle(artifact)

  if (resolved.kind !== 'ok') {
    return (
      <section className="artifact chart-artifact" aria-label="图表数据集未取得">
        <header className="artifact-head">
          <span className="artifact-title">图表（{kind}）</span>
        </header>
        <p className="limitations">
          {resolved.kind === 'missing'
            ? '图表引用的数据集未随本轮返回，未渲染：图上不补任何推算。'
            : resolved.reason}
        </p>
      </section>
    )
  }

  const spec = artifact
  const dataset = resolved.dataset
  const x = String(spec.x ?? '')
  const y = String(spec.y ?? '')
  const points = pointsOf(spec, dataset, x, y)
  const ticks = axisTicks(points)
  const series = seriesList(points).filter((item) => item.key !== '')
  const drawn = series.length <= MAX_SERIES
    ? points
    : points.filter((point) => series.slice(0, MAX_SERIES).some((item) => item.key === point.seriesKey))
  // 轴只按**画得出来的那些点**缩放：让一个没被画的系列决定轴顶，画出来的柱子就全成了
  // 一小截，读者会以为那些分组都很小。未画的系列在图例与下方列表/表格里照实列出。
  const scale = scaleOf(drawn)
  const truncated = series.length > MAX_SERIES
  const gaps = Array.isArray(spec.coverage_gaps)
    ? spec.coverage_gaps.filter((gap): gap is string => typeof gap === 'string') : []
  const switchName = `chart-view-${String(spec.artifact_id ?? spec.dataset_ref)}`
  const drillable = x === 'platform' && onDrilldown !== undefined
  const axisRange = scale.min < 0 ? `${scale.bottom} 到 ${scale.top}` : `0 到 ${scale.top}`

  return (
    <section className="artifact chart-artifact" aria-label={axis}>
      <header className="artifact-head">
        <span className="artifact-title">{axis}</span>
        <span className="pill">{spec.kind === 'line' ? '折线' : '柱状'}</span>
        <span className="pill">{spec.baseline === 'zero' ? '零基线' : '自适应轴'}</span>
      </header>

      <div className="chart-switch" role="radiogroup" aria-label="图表或表格">
        <input type="radio" id={`${switchName}-plot`} name={switchName} defaultChecked />
        <label htmlFor={`${switchName}-plot`}>图</label>
        <input type="radio" id={`${switchName}-table`} name={switchName} />
        <label htmlFor={`${switchName}-table`}>表</label>

        <div className="chart-view chart-view-plot">
          <div className="chart-svg-wrap">
            <svg
              viewBox={`0 0 ${PLOT.width} ${PLOT.height}`}
              role="img"
              aria-label={`${axis}：${ticks.length} 个${spec.kind === 'line' ? '日期' : '分组'}`
                + `${series.length > 0
                  ? ` × ${series.length} 个系列${truncated ? `（画前 ${MAX_SERIES} 个）` : ''}`
                  : ''}，纵轴 ${axisRange}`
                + `${gaps.length ? `，${gaps.length} 段未覆盖` : ''}`}
              data-chart-kind={spec.kind}
              data-chart-baseline={spec.baseline}
              data-chart-nulls={spec.null_values}
              data-chart-unit={spec.unit}
              data-chart-x={x}
              data-chart-y={y}
              data-chart-series={series.map((item) => item.key).join(',')}
              data-chart-ticks={ticks.length}>
              {/* 零基线：柱形都从这条线起画（spec §8）；有负值时它落在轴中间。 */}
              <line className="chart-axis" x1={PLOT.left} x2={PLOT.width - PLOT.right}
                y1={scale.zeroY} y2={scale.zeroY} />
              <text className="chart-tick" x={4} y={scale.zeroY + 4}>0</text>
              <text className="chart-tick" x={4} y={PLOT.top + 4}>{scale.top}</text>
              {scale.min < 0 && (
                <text className="chart-tick" x={4} y={BASELINE + 4}>{scale.bottom}</text>
              )}
              {/* 两个系列渲染器只拿 drawn 这批点，与轴缩放用的是同一份：超过
                  MAX_SERIES 的系列已经在图例里声明"未画"，再把它的点画进图里
                  （哪怕只是一个圆点）就是自相矛盾，何况它还是按不含它的那份缩放定位。 */}
              {spec.kind === 'line'
                ? <LineSeries points={drawn} ticks={ticks} series={series} scale={scale} />
                : <BarSeries points={drawn} ticks={ticks} series={series} scale={scale}
                    spec={spec} dataset={dataset} drillable={drillable}
                    onDrilldown={onDrilldown} />}
              {ticks.map((tick, index) => (
                <text
                  key={`label-${x}-${tick}`}
                  className="chart-label"
                  x={spec.kind === 'line'
                    ? lineX(index, ticks.length)
                    : barSlot(index, ticks.length, 0, Math.max(series.length, 1)).centre}
                  y={PLOT.height - 30}>
                  {points.find((point) => point.key === tick)?.label ?? tick}
                </text>
              ))}
            </svg>
          </div>
          {series.length > 0 && (
            <ul className="chart-legend" aria-label="系列图例">
              {series.slice(0, MAX_SERIES).map((item, index) => (
                <li key={`legend-${item.key}`} data-series-index={index}
                  data-ref={item.entityRef}>
                  <span className={`chart-swatch chart-swatch-${index}`}>{item.label}</span>
                </li>
              ))}
              {truncated && (
                <li key="legend-more">
                  另有 {series.length - MAX_SERIES} 个系列未画：完整数值见下方列表与表格
                </li>
              )}
            </ul>
          )}
          {/* 数值逐条列出：柱子与折线的高度要能按字核对，而且系列必须写在同一行里。 */}
          <ul className="chart-values">
            {points.map((point, index) => (
              <li key={`value-${index}-${point.seriesKey}-${point.key}`}
                data-ref={point.entityRef} data-series={point.seriesKey || undefined}>
                <span>
                  {point.seriesLabel ? `${point.seriesLabel} · ` : ''}{point.label}
                </span>
                <strong className={point.num === null ? 'missing' : ''}
                  data-value={point.text}>{point.text}</strong>
              </li>
            ))}
          </ul>
        </div>

        <div className="chart-view chart-view-table">
          <div className="table-wrap">
            <table>
              <caption className="chart-caption">{axis}</caption>
              <thead>
                <tr>
                  {series.length > 0 && <th scope="col">系列</th>}
                  <th scope="col">{x}</th>
                  <th scope="col">{y}</th>
                </tr>
              </thead>
              <tbody>
                {points.map((point, index) => (
                  <tr key={`row-${index}-${point.seriesKey}-${point.key}`}>
                    {series.length > 0 && (
                      <td data-series={point.seriesKey} data-ref={point.seriesRef}>
                        {point.seriesLabel}
                      </td>
                    )}
                    <td data-ref={point.entityRef}>{point.label}</td>
                    <td className={point.num === null ? 'missing' : ''}
                      data-value={point.text}>{point.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="artifact-meta">
        <span>分组：{x}</span>
        <span>系列：{Array.isArray(spec.series) ? spec.series.join('、') : '—'}</span>
        <span>覆盖：{String(spec.coverage_status ?? '未知')}
          {gaps.length > 0 && `（${gaps.join('；')}）`}</span>
        <span>引用数据集：{String(spec.dataset_type ?? '—')}</span>
      </div>
      {x === 'platform' && !drillable && (
        <p className="limitations">平台下钻需要重新授权：本轮未接入重新提问的入口。</p>
      )}
    </section>
  )
}

function BarSeries({ points, ticks, series, scale, spec, dataset, drillable, onDrilldown }: {
  points: Point[]
  ticks: string[]
  series: Series[]
  scale: Scale
  spec: Artifact
  dataset: Artifact
  drillable: boolean
  onDrilldown?: (intent: DrilldownIntent) => void
}) {
  const seriesList: Series[] = series.length > 0
    ? series.slice(0, MAX_SERIES)
    : [{ key: '', label: '', entityRef: undefined }]
  return (
    <g>
      {ticks.map((tick, tickIndex) => seriesList.map((line, seriesIndex) => {
        const point = points.find((item) => item.key === tick
          && item.seriesKey === line.key)
        if (point === undefined) return null
        const slot = barSlot(tickIndex, ticks.length, seriesIndex, seriesList.length)
        const shape = point.num === null
          // 缺值是"没算出来"，不是 0：留一个虚线空位，既不画柱子也不留空白，
          // 否则读者分不清"没数"与"这根柱子很矮"。
          ? <rect
              className="chart-bar-gap" x={slot.left} y={PLOT.top} width={slot.width}
              height={Math.max(BASELINE - PLOT.top, 1)} />
          : <rect
              className="chart-bar" x={slot.left} width={slot.width}
              y={Math.min(point.num >= 0 ? valueY(point.num, scale) : scale.zeroY,
                scale.zeroY)}
              height={Math.max(Math.abs(scale.zeroY - valueY(point.num, scale)), 1)}
              data-value={point.text} data-group={point.key}
              data-series={point.seriesKey || undefined} />
        const intent = drillable ? drilldownIntent(spec, dataset, point) : null
        const groupKey = `bar-${line.key}-${tick}`
        if (intent === null) return <g key={groupKey}>{shape}</g>
        return (
          <g
            key={groupKey}
            className="chart-drill"
            role="button"
            tabIndex={0}
            aria-label={`按店铺对比 ${point.label}`}
            onClick={() => onDrilldown?.(intent)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onDrilldown?.(intent)
              }
            }}>
            {shape}
            <title>{`按店铺对比 ${point.label}：将重新发起一次已授权的对比请求`}</title>
          </g>
        )
      }))}
    </g>
  )
}

function LineSeries({ points, ticks, series, scale }: {
  points: Point[]
  ticks: string[]
  series: Series[]
  scale: Scale
}) {
  const seriesList: Series[] = series.length > 0
    ? series.slice(0, MAX_SERIES)
    : [{ key: '', label: '', entityRef: undefined }]
  return (
    <g>
      {seriesList.map((line, seriesIndex) => {
        // 缺值断开：把两个可算点直接连过去，会被读成"那天落在两点之间"，那是编的。
        const segments: Point[][] = []
        let current: Point[] = []
        for (const tick of ticks) {
          const point = points.find((item) => item.key === tick
            && item.seriesKey === line.key)
          if (point === undefined || point.num === null) {
            if (current.length) segments.push(current)
            current = []
            continue
          }
          current.push(point)
        }
        if (current.length) segments.push(current)
        return (
          <g key={`series-${line.key}`} className="chart-series" data-series={line.key || undefined}>
            {segments.map((segment, index) => (
              <polyline
                key={`segment-${line.key}-${index}`}
                className={`chart-line chart-series-${seriesIndex}`}
                fill="none"
                points={segment
                  .map((point) => `${lineX(ticks.indexOf(point.key), ticks.length)},`
                    + `${valueY(point.num as number, scale)}`)
                  .join(' ')} />
            ))}
          </g>
        )
      })}
      {/* points 已由调用方限定为"画得出来的那些系列"，因此轴、折线与圆点同一批数。 */}
      {points.map((point, index) => (
        <circle
          key={`point-${index}-${point.seriesKey}-${point.key}`}
          className={point.num === null
            ? 'chart-point is-missing'
            : `chart-point chart-series-${Math.max(seriesList.findIndex(
                (item) => item.key === point.seriesKey), 0)}`}
          cx={lineX(ticks.indexOf(point.key), ticks.length)}
          cy={point.num === null ? PLOT.top : valueY(point.num, scale)}
          r={3.5}
          data-missing={point.num === null ? 'true' : undefined}
          data-value={point.text}
          data-group={point.key}
          data-series={point.seriesKey || undefined} />
      ))}
    </g>
  )
}

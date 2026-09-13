import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ChartArtifact, drilldownIntent, resolveChartDataset } from './ChartArtifact'
import { ArtifactView } from './ArtifactView'
import type { Artifact } from '../types'

const SHOP_A = 'ent-59f71124'
const SHOP_B = 'ent-7ab11c02'
const UNKNOWN_REF = 'ent-00000000'
const TABLE_ID = '6f1f2b3c-4d5e-4f60-8a71-8293a4b5c6d7'
const CHART_ID = '1a2b3c4d-5e6f-4a0b-9c8d-7e6f5a4b3c2d'
const AS_OF = '2026-09-08T09:00:00+08:00'
/** 与 ChartArtifact 里的 PLOT 同源：220 - 52。零基线就在这一条线上。 */
const BASELINE = 168

const dataset: Artifact = {
  artifact_id: TABLE_ID,
  artifact_type: 'comparison_table',
  status: 'partial',
  data_as_of: AS_OF,
  coverage: { status: 'complete', start: '2026-09-01', end: '2026-09-08', gaps: [] },
  filters: { start: '2026-09-01', end: '2026-09-08', group_by: 'platform',
    sales_basis: 'erp_effective_parent', shop_refs: [SHOP_A, SHOP_B] },
  metric_definition: { sales_amount: '有效非赠品销售父项的行级分摊支付金额合计' },
  data: [
    { platform: 'fxg', sales_amount: '900' },
    { platform: 'jd', sales_amount: '100' },
    { platform: 'kuaishou', sales_amount: null },
    { sales_amount: '1000' },
  ],
  entities: [
    { ref: SHOP_A, kind: 'shop', display_name: '元发钉枪(抖音)', name_source: 'shop_profile',
      platform: 'fxg' },
    { ref: SHOP_B, kind: 'shop', name_source: 'unresolved', platform: 'jd' },
  ],
  limitations: ['1 家获准店铺未列入本次合计，原因见 excluded_scope'],
}

const barSpec: Artifact = {
  artifact_id: CHART_ID,
  artifact_type: 'chart_spec',
  chart_version: 1,
  spec_version: 'chart-spec/1',
  kind: 'bar',
  x: 'platform',
  y: 'sales_amount',
  series: ['platform'],
  unit: 'CNY',
  currency: 'CNY',
  metric_basis: 'sales_amount|platform_payment/v1|pay_time',
  dataset_ref: TABLE_ID,
  dataset_type: 'comparison_table',
  dataset_data_as_of: AS_OF,
  coverage_ref: TABLE_ID,
  coverage_status: 'complete',
  coverage_start: '2026-09-01',
  coverage_end: '2026-09-08',
  coverage_gaps: [],
  baseline: 'zero',
  null_values: 'blank',
}

const siblings = [dataset, barSpec]

function chart(artifact: Artifact = barSpec, list: Artifact[] = siblings,
  onDrilldown?: unknown) {
  return renderToStaticMarkup(
    <ChartArtifact artifact={artifact} artifacts={list} onDrilldown={onDrilldown as never} />,
  )
}

/** 只取"带 data-value 且后面紧跟文本"的节点：图与表用同一种写法列同一批数。 */
function valuesOf(markup: string) {
  return [...markup.matchAll(/data-value="([^"]*)">([^<]*)</g)]
    .map(([, attribute, shown]) => `${attribute}=${shown}`)
}

function section(markup: string, selector: string) {
  if (selector === 'plot') {
    return /class="chart-view chart-view-plot"[\s\S]*?(?=<div class="chart-view chart-view-table")/
      .exec(markup)?.[0] ?? ''
  }
  return /class="chart-view chart-view-table"[\s\S]*?<\/table>/.exec(markup)?.[0] ?? ''
}

function point(key: string, label: string, text: string, num: number | null) {
  return { key, label, text, num, seriesKey: '', seriesLabel: '' }
}

describe('图表卡片：只渲染已落库数据集', () => {
  it('一根轴只放一个指标，另一张图用它自己的单位与口径', () => {
    const markup = chart()
    expect(markup).toContain('sales_amount（CNY）｜sales_amount|platform_payment/v1|pay_time')
    expect(markup).toContain('data-chart-kind="bar"')
    expect(markup).toContain('data-chart-y="sales_amount"')
    const quantity = chart({
      ...barSpec,
      artifact_id: '9a2b3c4d-5e6f-4a0b-9c8d-7e6f5a4b3c2d',
      y: 'sold_quantity', unit: 'piece', currency: undefined,
      metric_basis: 'sold_quantity|platform_payment/v1|pay_time',
    })
    expect(quantity).toContain('sold_quantity（piece）')
    expect(markup).not.toContain('sold_quantity（piece）')
    expect(quantity).not.toContain('sales_amount（CNY）')
  })

  it('条形图从 0 基线起画；缺值留虚线空位，不当成 0', () => {
    const markup = chart()
    expect(markup).toContain('data-chart-baseline="zero"')
    expect(markup).toContain(`y1="${BASELINE}"`)
    const bars = [...markup.matchAll(/<rect class="chart-bar"([\s\S]*?)><\/rect>/g)]
    expect(bars).toHaveLength(2)
    for (const [, attributes] of bars) {
      const y = Number(/y="([0-9.]+)"/.exec(attributes)?.[1])
      const height = Number(/height="([0-9.]+)"/.exec(attributes)?.[1])
      // 柱底正好落在零基线上：不从 0 起画的柱形是在拿轴长编故事。
      expect(Number((y + height).toFixed(1))).toBeCloseTo(BASELINE, 1)
    }
    const gaps = [...markup.matchAll(/<rect class="chart-bar-gap"/g)]
    expect(gaps).toHaveLength(1)
    expect(markup).toContain('data-value="不可计算"')
  })

  it('图与表逐项相等：同一批数，两种读法', () => {
    const markup = chart()
    const plot = section(markup, 'plot')
    const table = section(markup, 'table')
    expect(valuesOf(plot)).toEqual([
      '900=900', '100=100', '不可计算=不可计算',
    ])
    expect(valuesOf(table)).toEqual(valuesOf(plot))
    expect(table).toContain('抖音')
    // 合计行不带分组键：它属于表格，不属于比较轴。
    expect(plot).not.toContain('data-value="1000"')
  })

  it('缺值把折线断开，真实 0 仍落在基线上', () => {
    const trend: Artifact = {
      artifact_id: '7b1f2b3c-4d5e-4f60-8a71-8293a4b5c6d7',
      artifact_type: 'trend_series',
      status: 'partial',
      data_as_of: AS_OF,
      filters: { start: '2026-09-05', end: '2026-09-08' },
      data: [
        { platform: 'jd', day: '2026-09-01', sales_amount: null },
        { platform: 'jd', day: '2026-09-02', sales_amount: '100' },
        { platform: 'jd', day: '2026-09-03', sales_amount: null },
        { platform: 'jd', day: '2026-09-04', sales_amount: '0' },
      ],
    }
    const spec: Artifact = {
      ...barSpec,
      dataset_ref: trend.artifact_id,
      coverage_ref: trend.artifact_id,
      dataset_type: 'trend_series',
      kind: 'line',
      x: 'day',
      series: ['platform'],
      baseline: 'auto',
      null_values: 'break',
      coverage_status: 'partial',
      coverage_gaps: ['2026-09-03~2026-09-04'],
    }
    const markup = chart(spec, [trend, spec])
    expect(markup).toContain('data-chart-nulls="break"')
    // 两段折线：中间那个 null 把线断开，而不是把两点连过去。
    expect([...markup.matchAll(/<polyline/g)]).toHaveLength(2)
    expect(markup).toContain('data-missing="true"')
    // 真实 0 不是缺失：点落在零基线上。
    expect(markup).toMatch(new RegExp(`<circle[^>]*cy="${BASELINE}"[^>]*data-value="0"`))
    expect(markup).toContain('2026-09-03~2026-09-04')
  })

  it('引用不到同版本数据集时拒绝渲染，不自己拼一份数', () => {
    const missing = chart(barSpec, [barSpec])
    expect(missing).toContain('图表引用的数据集未随本轮返回')
    expect(missing).not.toContain('data-chart-kind')

    const stale = chart({ ...barSpec, dataset_data_as_of: '2026-09-07T09:00:00+08:00' },
      siblings)
    expect(stale).toContain('数据集版本与图表声明不一致，未渲染')
    expect(stale).not.toContain('data-chart-kind')

    const wrongType = chart({ ...barSpec, dataset_type: 'trend_series' }, siblings)
    expect(wrongType).toContain('数据集类型与图表声明不一致，未渲染')
    expect(wrongType).not.toContain('data-chart-kind')

    const resolved = resolveChartDataset(siblings, barSpec)
    expect(resolved.kind).toBe('ok')
  })

  it('平台码与店铺引用都换成可读标签', () => {
    const markup = chart()
    // 平台轴上写"抖音"而不是"fxg"：这张码表在 ArtifactView 一处，不另抄一份。
    expect(markup).toContain('>抖音<')
    expect(markup).not.toContain('>fxg<')

    const shopChart = chart({ ...barSpec, x: 'shop_ref' }, [
      { ...dataset, data: [{ shop_ref: SHOP_A, sales_amount: '900' },
        { shop_ref: UNKNOWN_REF, sales_amount: '10' }] },
      barSpec,
    ])
    expect(shopChart).toContain('元发钉枪(抖音)')
    expect(shopChart).toContain('名称未取得')
    // 引用只留在 data-ref 上，不进可见文本。
    expect(shopChart).not.toMatch(/>(?:ent-[0-9a-z]{8})</)
  })

  it('载荷里的文本一律当文本，不当标记', () => {
    const injected = chart(barSpec, [
      { ...dataset,
        entities: [{ ref: SHOP_A, kind: 'shop', name_source: 'shop_profile',
          display_name: '<script>alert(1)</script>' }],
        data: [{ platform: 'jd', sales_amount: '900' }] },
      barSpec,
    ])
    expect(injected).not.toContain('<script>alert')
    expect(injected).not.toContain('onerror=')
  })
})


describe('多系列趋势与轴的两端', () => {
  const trendTwo: Artifact = {
    artifact_id: '7b1f2b3c-4d5e-4f60-8a71-8293a4b5c6d7',
    artifact_type: 'trend_series',
    status: 'partial',
    data_as_of: AS_OF,
    filters: { start: '2026-09-05', end: '2026-09-07', group_by: 'platform' },
    // 后端逐分组 × 逐日发行：同一日期会出现两次，分属不同平台。
    data: [
      { platform: 'jd', day: '2026-09-01', sales_amount: '10' },
      { platform: 'jd', day: '2026-09-02', sales_amount: '20' },
      { platform: 'jd', day: '2026-09-03', sales_amount: null },
      { platform: 'jd', day: '2026-09-04', sales_amount: '40' },
      { platform: 'kuaishou', day: '2026-09-01', sales_amount: '100' },
      { platform: 'kuaishou', day: '2026-09-02', sales_amount: '200' },
      { platform: 'kuaishou', day: '2026-09-03', sales_amount: '300' },
      { platform: 'kuaishou', day: '2026-09-04', sales_amount: null },
    ],
  }
  const lineSpec: Artifact = {
    ...barSpec,
    artifact_id: '2a2b3c4d-5e6f-4a0b-9c8d-7e6f5a4b3c2d',
    dataset_ref: trendTwo.artifact_id,
    coverage_ref: trendTwo.artifact_id,
    dataset_type: 'trend_series',
    kind: 'line',
    x: 'day',
    y: 'sales_amount',
    series: ['platform'],
    baseline: 'auto',
    null_values: 'break',
    coverage_status: 'partial',
    coverage_gaps: ['2026-09-03~2026-09-04'],
  }
  const list = [trendTwo, lineSpec]

  it('series 决定线条数：两个平台就是两条线，不连成一条假线', () => {
    const markup = chart(lineSpec, list)
    const groups = [...markup.matchAll(/<g class="chart-series" data-series="([a-z]+)">/g)]
      .map(([, group]) => group)
    expect(groups).toEqual(['jd', 'kuaishou'])
    // 每组的断口各自成立：jd 断在 09-03，kuaishou 断在 09-04。
    const segments = [...markup.matchAll(/<polyline/g)]
    expect(segments).toHaveLength(3)
    // 甲组最后一天连到乙组第一天就是编出来的线：日期必须只在轴上出现一次。
    const axisLabels = [...markup.matchAll(/class="chart-label"[^>]*>([0-9-]{10})</g)]
      .map(([, shown]) => shown)
    expect(axisLabels).toEqual(['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04'])
    expect(markup).toContain('data-chart-series="jd,kuaishou"')
    expect(markup).toContain('data-chart-ticks="4"')
  })

  it('七个系列只画前六个：未画的系列不进 polyline 也不进圆点，但数照列', () => {
    // 后端仍会把 7 个分组都发出来（每个分组都是完整且同口径的）；上限只是渲染密度。
    const codes = ['jd', 'kuaishou', 'wxsph', 'fxg', 'tb', 'tm', 'wsxc']
    const trendSeven: Artifact = {
      ...trendTwo,
      artifact_id: '8c1f2b3c-4d5e-4f60-8a71-8293a4b5c6d7',
      data: codes.flatMap((platform, index) => [
        { platform, day: '2026-09-01', sales_amount: String((index + 1) * 100) },
        { platform, day: '2026-09-02', sales_amount: String((index + 1) * 100) },
      ]),
    }
    const spec: Artifact = {
      ...lineSpec,
      artifact_id: '3a2b3c4d-5e6f-4a0b-9c8d-7e6f5a4b3c2d',
      dataset_ref: trendSeven.artifact_id,
      coverage_ref: trendSeven.artifact_id,
    }
    const markup = chart(spec, [trendSeven, spec])
    const omitted = codes[codes.length - 1]
    const svg = /<svg[\s\S]*?<\/svg>/.exec(markup)?.[0] ?? ''

    // 折线组与圆点都只有前 6 个系列：第 7 个一个像素都不占。
    const groups = [...svg.matchAll(/<g class="chart-series" data-series="([a-z]+)">/g)]
      .map(([, group]) => group)
    expect(groups).toEqual(codes.slice(0, 6))
    expect(groups).not.toContain(omitted)
    expect([...svg.matchAll(/<polyline/g)]).toHaveLength(6)
    expect([...svg.matchAll(/<circle/g)]).toHaveLength(12)
    expect(svg).not.toContain(`data-series="${omitted}"`)
    // 轴也只按画得出来的点缩放：未被画的那个大数不能决定轴顶。
    expect(svg).toContain('>600<')
    expect(svg).not.toContain('>700<')
    // 未画不是消失：图例说明、数值列表与表格都仍有它那一行。
    expect(markup).toContain('另有 1 个系列未画：完整数值见下方列表与表格')
    expect(markup).toContain('7 个系列（画前 6 个）')
    expect(valuesOf(section(markup, 'plot'))).toHaveLength(14)
    const table = section(markup, 'table')
    expect(valuesOf(table)).toEqual(valuesOf(section(markup, 'plot')))
    expect(table).toContain(`data-series="${omitted}"`)
    expect(table).toContain('700')
  })

  it('系列身份跟着数走：图例、数值列表与表格都能分清同一天', () => {
    const markup = chart(lineSpec, list)
    expect(markup).toContain('class="chart-legend"')
    expect(markup).toContain('京东')
    expect(markup).toContain('快手')
    const plot = section(markup, 'plot')
    expect(valuesOf(plot)).toEqual([
      '10=10', '20=20', '不可计算=不可计算', '40=40',
      '100=100', '200=200', '300=300', '不可计算=不可计算',
    ])
    expect([...plot.matchAll(/<span>([^<]*)<\/span>/g)].map(([, shown]) => shown))
      .toEqual(['京东 · 2026-09-01', '京东 · 2026-09-02', '京东 · 2026-09-03',
        '京东 · 2026-09-04', '快手 · 2026-09-01', '快手 · 2026-09-02',
        '快手 · 2026-09-03', '快手 · 2026-09-04'])
    const table = section(markup, 'table')
    expect(table).toContain('<th scope="col">系列</th>')
    expect([...table.matchAll(/<tr>/g)].length - 1).toBe(8)
    expect(valuesOf(table)).toEqual(valuesOf(plot))
  })

  it('亏损不被画成接近 0：负值自己占一段轴，柱形从基线向下', () => {
    const loss: Artifact = {
      ...dataset,
      data: [
        { platform: 'fxg', sales_amount: '900' },
        { platform: 'jd', sales_amount: '-300' },
      ],
    }
    const markup = chart(barSpec, [loss, barSpec])
    expect(markup).toContain('纵轴 -300 到 900')
    const bars = [...markup.matchAll(/<rect class="chart-bar"([\s\S]*?)><\/rect>/g)]
      .map(([, attributes]) => ({
        y: Number(/y="(-?[0-9.]+)"/.exec(attributes)?.[1]),
        height: Number(/height="(-?[0-9.]+)"/.exec(attributes)?.[1]),
        value: /data-value="(-?[0-9]+)"/.exec(attributes)?.[1],
      }))
    // 轴：min=-300、max=900 ⇒ 零线在 168 - 300/1200×150 = 130.5，不是画在轴底。
    const zero = 130.5
    expect(markup).toContain(`y1="${zero}"`)
    const positive = bars.find((bar) => bar.value === '900')
    const negative = bars.find((bar) => bar.value === '-300')
    expect(Number(((positive?.y ?? 0) + (positive?.height ?? 0)).toFixed(1)))
      .toBeCloseTo(zero, 1)                     // 正值从基线向上长
    expect(Number((negative?.y ?? 0).toFixed(1))).toBeCloseTo(zero, 1)
    expect(Number((negative?.height ?? 0).toFixed(1))).toBeCloseTo(BASELINE - zero, 1)
    expect((negative?.height ?? 0)).toBeGreaterThan(1)   // 不是贴着轴底的一像素
  })

  it('数据集版本按时刻比，不按序列化写法比', () => {
    // 同一瞬间的两种合法写法：pydantic 投 'Z'，载荷里是 '+00:00'。
    const utcDataset: Artifact = { ...dataset, data_as_of: '2026-09-08T01:00:00.000Z' }
    const utcSpec: Artifact = { ...barSpec, dataset_data_as_of: '2026-09-08T01:00:00+00:00' }
    const markup = chart(utcSpec, [utcDataset, utcSpec])
    expect(markup).toContain('data-chart-kind="bar"')
    expect(markup).not.toContain('数据集版本与图表声明不一致')

    const later: Artifact = { ...barSpec, dataset_data_as_of: '2026-09-08T02:00:00+00:00' }
    const refused = chart(later, [dataset, later])
    expect(refused).toContain('数据集版本与图表声明不一致，未渲染')
    expect(refused).not.toContain('<svg')
  })
})

describe('平台下钻：只交出重新提问的意图', () => {
  it('交出窗口、口径与指标，交不出任何店铺级数字', () => {
    const intent = drilldownIntent(barSpec, dataset, point('fxg', '抖音', '900', 900))
    expect(intent).not.toBeNull()
    expect(intent?.platform).toBe('fxg')
    expect(intent?.groupBy).toBe('shop')
    // 窗口与口径原样带过去：下钻不许顺手把七天改成三十天，也不许换口径。
    expect(intent?.start).toBe('2026-09-01')
    expect(intent?.end).toBe('2026-09-08')
    expect(intent?.metric).toBe('sales_amount')
    expect(intent?.metricBasis).toBe('sales_amount|platform_payment/v1|pay_time')
    expect(intent?.question).toContain('抖音（platform=fxg）各店铺对比：2026-09-01 至 2026-09-08')
    // 只给人读名会让下钻死在平台码上：模型上下文里没有码<->名对照，normalize_platform
    // 只能 fail closed，一次点击就退化成"查询参数无效"。
    expect(intent?.question).toContain('platform=fxg')
    // 意图里只有平台码、日期、指标名与口径签名：没有金额，也没有店铺引用。
    // 只有平台码、日期、指标名与口径签名：金额一个都不带。
    for (const amount of ['900', '100', '1000']) {
      expect(intent?.question).not.toContain(amount)
    }
    expect(intent?.question).not.toContain(SHOP_A)
    // 店铺柱与折线不下钻：前者已是最细一层，后者不是分组轴。
    expect(drilldownIntent({ ...barSpec, x: 'shop_ref' }, dataset,
      point(SHOP_A, '甲店', '900', 900))).toBeNull()
    expect(drilldownIntent({ ...barSpec, kind: 'line' }, dataset,
      point('fxg', '抖音', '900', 900))).toBeNull()
  })

  it('接入下钻入口时平台柱才是按钮，没接入时明说要重新授权', () => {
    const withDrill = chart(barSpec, siblings, () => undefined)
    expect(withDrill).toContain('role="button"')
    expect(withDrill).toContain('aria-label="按店铺对比 抖音"')
    expect(withDrill).toContain('aria-label="按店铺对比 京东"')
    expect(withDrill).not.toContain('本轮未接入重新提问的入口')

    const without = chart(barSpec, siblings, undefined)
    expect(without).toContain('本轮未接入重新提问的入口')
    // 没有回调时连按钮都不给：给一个"点了什么也不会发生"的下钻更坏。
    expect(without).not.toContain('role="button"')
    // 组件自己不渲染任何店铺级行：下钻的数只能来自一次新的已授权请求。
    expect(without).not.toContain('元发钉枪')
    expect(without).not.toContain(SHOP_A)
  })
})

describe('ArtifactView 分流', () => {
  it('数据集仍是表格，图表卡片走 ChartArtifact', () => {
    const table = renderToStaticMarkup(
      <ArtifactView artifact={dataset} datasets={siblings} />)
    expect(table).toContain('<table')
    expect(table).not.toContain('data-chart-kind')
    const plot = renderToStaticMarkup(
      <ArtifactView artifact={barSpec} datasets={siblings} />)
    expect(plot).toContain('data-chart-kind="bar"')
    expect(plot).toContain('sales_amount（CNY）')
  })
})

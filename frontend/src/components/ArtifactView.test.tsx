import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ArtifactView, artifactEntities, auditSummary, auditStatus, cellText } from './ArtifactView'
import type { Artifact } from '../types'

const SHOP_REF = 'ent-59f71124'
const PRODUCT_REF = 'ent-a308043a'
const UNKNOWN_REF = 'ent-00000000'

const artifact: Artifact = {
  status: 'ok',
  data_as_of: '2026-09-08T09:00:00+08:00',
  coverage: { status: 'complete', start: '2026-09-01', end: '2026-09-08', gaps: [] },
  limitations: ['同批支付额为0或无支付，同批退款率不可计算'],
  filters: { start: '2026-09-01', end: '2026-09-08', shop_refs: [SHOP_REF], group_by: 'product' },
  data: [
    { shop_ref: SHOP_REF, product_ref: PRODUCT_REF, quantity: '12', paid_amount: '4465.03' },
    { shop_ref: SHOP_REF, product_ref: UNKNOWN_REF, quantity: '3', paid_amount: '120.00' },
    { notice: '仅返回Top 2，共57个商品' },
  ],
  entities: [
    { ref: SHOP_REF, kind: 'shop', display_name: '元发钉枪(抖音)', name_source: 'shop_profile',
      platform: 'fxg' },
    { ref: PRODUCT_REF, kind: 'product', display_name: '接头-元发', sku_label: '20PP',
      name_source: 'trade_snapshot' },
    { ref: UNKNOWN_REF, kind: 'product', name_source: 'unresolved' },
  ],
  catalog_version: 7,
}

describe('ArtifactView 名称投影', () => {
  const html = renderToStaticMarkup(<ArtifactView artifact={artifact} />)

  it('renders authorized display names instead of refs', () => {
    expect(html).toContain('元发钉枪(抖音)')
    expect(html).toContain('接头-元发')
    // 引用只能出现在 data-ref 审计属性上，不能迚入可见文本。
    expect(html.match(/ent-[0-9a-z]{8}(?!"|')/g)).toBeNull()
    expect(html).toContain(`data-ref="${SHOP_REF}"`)
  })

  it('marks snapshot names and appends the sku label', () => {
    expect(html).toContain('接头-元发（成交名） 20PP')
  })

  it('tags each shop with its platform', () => {
    // 平台属于哪一家店是经营者做跨平台判断的第一层信息。
    expect(html).toContain('抖音')
    expect(html).toContain('data-platform="fxg"')
  })

  it('falls back to the raw platform code when the label is unknown', () => {
    const withUnknown = {
      ...artifact,
      entities: [
        { ref: SHOP_REF, kind: 'shop', display_name: '某店', name_source: 'shop_profile',
          platform: 'wsxc' },
      ],
    }
    expect(renderToStaticMarkup(<ArtifactView artifact={withUnknown} />)).toContain('wsxc')
  })

  it('shows an unresolved name as a placeholder, never the raw ref', () => {
    expect(html).toContain('名称未取得')
    expect(html).not.toContain(`>${UNKNOWN_REF}<`)
  })

  it('keeps the TopN notice out of the table but visible to the reader', () => {
    expect(html).toContain('仅返回Top 2，共57个商品')
    expect(html).toContain('2 行')
    expect(html.match(/<tbody>[\s\S]*?<\/tbody>/)?.[0].match(/<tr>/g)).toHaveLength(2)
  })

  it('lists the queried shop scope in the meta line', () => {
    expect(html).toContain('范围：元发钉枪(抖音)')
  })
})

describe('名称解析纯函数', () => {
  it('ignores malformed entities instead of trusting the payload', () => {
    const entities = artifactEntities({
      entities: [{ kind: 'shop' }, { ref: SHOP_REF, kind: 'shop', display_name: '甲店',
        name_source: 'shop_profile' }],
    })
    expect(entities.size).toBe(1)
    expect(entities.get(SHOP_REF)?.display_name).toBe('甲店')
  })

  it('leaves non-ref values untouched', () => {
    expect(cellText('4465.03', new Map())).toBe('4465.03')
    expect(cellText(null, new Map())).toBe('不可计算')
    // 不认识的引用只能当“名称未取得”，不能拿另一个实体名顶上去。
    expect(cellText(SHOP_REF, new Map([[UNKNOWN_REF, {
      ref: UNKNOWN_REF, kind: 'shop', display_name: '乙店', name_source: 'shop_profile',
    }]]))).toBe('名称未取得')
  })
})

describe('口径凭证展示', () => {
  const single = renderToStaticMarkup(<ArtifactView artifact={{
    ...artifact,
    basis: [{ metric: 'paid_amount', basis: 'platform_payment/v1',
              time_basis: 'pay_time', shop_ref: SHOP_REF }],
  }} />)

  it('把口径与时间归属显示出来，不显示主键和接口方法名', () => {
    expect(single).toContain('paid_amount＝platform_payment/v1（pay_time）')
    expect(single).not.toContain('erp.trade.list.query')
  })

  const mixed = renderToStaticMarkup(<ArtifactView artifact={{
    ...artifact,
    basis: [
      { metric: 'erp_documents', basis: 'erp_document/v1', time_basis: 'pay_time',
        shop_ref: SHOP_REF },
      { metric: 'erp_documents', basis: 'erp_document/v1', time_basis: 'outstock_time',
        shop_ref: PRODUCT_REF },
    ],
  }} />)

  it('混口径时明确提示不能汇总比较', () => {
    expect(mixed).toContain('口径不同，不能汇总、求增长率或排名')
    expect(mixed).toContain('basis-mixed')
  })

  it('没有口径凭证的旧 Artifact 仍然可读', () => {
    expect(renderToStaticMarkup(<ArtifactView artifact={artifact} />)).not.toContain('统计口径')
  })
})

/**
 * 上架价复核差异表（运营工作流计划 Task 9）。
 *
 * 这张表要钉住的是"每一格都说什么"：缺目标价、快照过期、来源未取证三种 null 必须
 * 分得开，而且任何一格的展示都不能把"未判定"说成"通过"。
 */
describe('ArtifactView 上架价复核', () => {
  const SHOP_A = 'ent-59f71124'
  const SHOP_B = 'ent-00000001'
  const SKU_A = 'ent-1111aaaa'
  const LISTING_A = 'lst-0123456789ab'
  const LISTING_B = 'lst-998877665544'

  const auditArtifact: Artifact = {
    artifact_type: 'price_audit',
    status: 'partial',
    data_as_of: '2026-09-13T11:40:00+08:00',
    audit: {
      expected_items: 3, evaluated_items: 2, matched_items: 1, all_correct: false,
      counts: { match: 1, mismatch: 1, stale: 1 },
      sources: [
        { shop_ref: SHOP_A, source_kind: 'official_export', enumeration_complete: true,
          fresh: true, snapshot_at: '2026-09-13T11:40:00+08:00' },
        { shop_ref: SHOP_B, source_kind: 'manual_import', enumeration_complete: false,
          fresh: false },
      ],
      freshness_policy_seconds: 86400,
      rule_version: 'listing-rules/2026-09-13.1',
    },
    filters: {
      price_basis: 'list_price', as_of: 'latest', currency: 'CNY',
      expected_prices: [{ applies_to: 'all_selected', expected_amount: '19.90',
        currency: 'CNY', price_basis: 'list_price' }],
    },
    limitations: ['快照已超过该来源的时效策略，按过期披露，不判正确'],
    data: [
      { shop_ref: SHOP_A, sku_ref: SKU_A, listing_ref: LISTING_A, expected_amount: '19.90',
        actual_amount: '19.90', amount_difference: '0', audit_status: 'match',
        price_basis: 'list_price', currency: 'CNY',
        snapshot_at: '2026-09-13T11:40:00+08:00' },
      { shop_ref: SHOP_A, listing_ref: LISTING_B, expected_amount: null,
        actual_amount: '29.90', amount_difference: null, audit_status: 'missing_standard',
        price_basis: 'list_price', currency: 'CNY',
        snapshot_at: '2026-09-13T11:40:00+08:00' },
      { shop_ref: SHOP_B, listing_ref: LISTING_A, expected_amount: '19.90',
        actual_amount: '19.90', amount_difference: null, audit_status: 'stale',
        price_basis: 'list_price', currency: 'CNY',
        snapshot_at: '2026-08-14T09:00:00+08:00' },
    ],
    entities: [
      { ref: SHOP_A, kind: 'shop', display_name: '元发钉枪(淘宝)', name_source: 'shop_profile',
        platform: 'tb' },
      { ref: SHOP_B, kind: 'shop', display_name: '钉枪工厂店', name_source: 'shop_profile',
        platform: 'jd' },
      { ref: SKU_A, kind: 'sku', name_source: 'unresolved' },
    ],
    catalog_version: 7,
  }

  const html = renderToStaticMarkup(<ArtifactView artifact={auditArtifact} />)

  it('renders the audit card with its denominator instead of a generic result table', () => {
    expect(html).toContain('上架价复核差异表')
    // 分母是期望项，不是抓到的链接数：这两个数并排出现才能看出"还差一项"。
    expect(html).toContain('期望 3 项')
    expect(html).toContain('已判定 2 项')
    expect(html).not.toContain('全部正确')
    expect(html).toContain('未全部通过')
  })

  it('shows authorized shop names and keeps refs on data attributes only', () => {
    expect(html).toContain('元发钉枪(淘宝)')
    expect(html).toContain('data-ref="ent-59f71124"')
    // 店铺引用不进可见文本；链接句柄（lst-）是要给经营者对着后台看的，原样展示。
    expect(html).toContain(LISTING_B)
    expect(html.match(/ent-[0-9a-z]{8}(?!"|')/g)).toBeNull()
  })

  it('labels each of the nine statuses without inventing a friendly pass', () => {
    expect(auditStatus('match')).toBe('一致')
    expect(auditStatus('mismatch')).toBe('不一致')
    expect(auditStatus('not_listed')).toBe('未上架')
    expect(auditStatus('not_on_sale')).toBe('不在售')
    expect(auditStatus('missing_standard')).toBe('缺目标价')
    expect(auditStatus('unmapped')).toBe('未映射')
    expect(auditStatus('stale')).toBe('快照过期')
    expect(auditStatus('unsupported')).toBe('来源未取证')
    expect(auditStatus('unknown')).toBe('无法判定')
    // 未登记的状态原样输出：猜一个近义词就是把"没判过"说成一个已知结论。
    expect(auditStatus('looks_fine')).toBe('looks_fine')
    expect(html).toContain('快照过期')
  })

  it('renders each kind of null in its own column, never as a zero', () => {
    // 按列位置断言，不用整页 toContain：差额、SKU、目标价三列都会渲染破折号，
    // 只查"页面里有没有 —"的话，目标价列被写成 0 也照样通过。
    const cellsOf = (row: string) => row.split('</td>').map((cell) => {
      const open = cell.indexOf('>')
      return open < 0 ? '' : cell.slice(open + 1).replace(/<[^>]*>/g, '').trim()
    })
    const rows = html.split('<tr>').slice(2)
    const judged = cellsOf(rows.find((row) => row.includes('data-audit-status="match"'))!)
    // 列序：店铺、SKU、链接、目标价、实价、差额、状态、时点
    expect(judged[3]).toBe('19.90')
    expect(judged[4]).toBe('19.90')
    expect(judged[5]).toBe('0')
    const missing = cellsOf(rows.find(
      (row) => row.includes('data-audit-status="missing_standard"'))!)
    expect(missing[3]).toBe('—')       // 本轮没给目标价：不是 0，也不是空白
    expect(missing[4]).toBe('29.90')    // 已采集到的实价照发（spec §6）
    expect(missing[5]).toBe('—')
    expect(missing[6]).toBe('缺目标价')
    const stale = cellsOf(rows.find((row) => row.includes('data-audit-status="stale"'))!)
    expect(stale[4]).toBe('19.90')
    // 过期那一格不得给出看着像当前的差额
    expect(stale[5]).toBe('—')
    // 名称未取得的 SKU 走全应用同一条规则：写"名称未取得"，引用留在 data-ref。
    expect(judged[1]).toBe('名称未取得')
    expect(judged[1]).not.toMatch(/ent-[0-9a-z]{8}/)
  })

  it('says every-cell-matching only when the payload claims it', () => {
    const clean = renderToStaticMarkup(<ArtifactView
      artifact={{ ...auditArtifact, status: 'ok',
        audit: { ...(auditArtifact.audit as object), expected_items: 3,
          evaluated_items: 3, matched_items: 3, all_correct: true,
          counts: { match: 3 } } }} />)
    expect(clean).toContain('每一格均有新鲜匹配证据')
    expect(clean).not.toContain('未全部通过')
    // 缺汇总块时不猜分母：宁可不显示这一行，也不从行数"算"一个期望项数出来。
    const bare = renderToStaticMarkup(<ArtifactView
      artifact={{ artifact_type: 'price_audit', status: 'ok', data: [] }} />)
    expect(bare).not.toContain('期望 ')
    expect(bare).not.toContain('全部正确')
  })

  it('does not silently drop a row that lost its status', () => {
    // 后端契约要求每行都有 audit_status；真出现缺状态的行，卡片必须还能被读出来，
    // 而且不能把"少了一行"演成"期望项就是这么多"。
    const leaked = renderToStaticMarkup(<ArtifactView
      artifact={{ ...auditArtifact,
        data: [...(auditArtifact.data as object[]), { shop_ref: SHOP_A }] }} />)
    expect(leaked).toContain('期望 3 项')
    expect(leaked).toContain('上架价复核差异表')
  })

  it('states the price basis and the snapshot provenance, not a data window', () => {
    expect(html).toContain('口径：标价')
    expect(html).toContain('快照来源：2 家店铺')
    expect(html).toContain('快照共同截止：2026-09-13T11:40:00+08:00')
    // 复核没有"期间"可言：拿日期区间冒充一次快照的时点会误导时效判断。
    expect(html).not.toContain('期间：')
    expect(html).toContain('快照已超过该来源的时效策略')
  })

  it('summarises the audit block without recomputing any denominator', () => {
    const summary = auditSummary(auditArtifact)
    expect(summary).toEqual({ expected: 3, evaluated: 2, allCorrect: false, sources: 2 })
    // 载荷缺块时宁可少说一句，也不从行数据里"算"一个分母出来。
    expect(auditSummary({})).toEqual({ expected: null, evaluated: null, allCorrect: null,
      sources: 0 })
    expect(() => renderToStaticMarkup(
      <ArtifactView artifact={{ artifact_type: 'price_audit', status: 'ok', data: [] }} />),
    ).not.toThrow()
  })

  it('says an empty roster is not the same as nothing being listed', () => {
    const empty = renderToStaticMarkup(<ArtifactView
      artifact={{ ...auditArtifact, data: [],
        audit: { ...(auditArtifact.audit as object), expected_items: 0, evaluated_items: 0,
          matched_items: 0, all_correct: false, counts: {}, sources: [] } }} />)
    expect(empty).toContain('本轮没有可复核的期望项')
    expect(empty).not.toContain('全部正确')
  })
})

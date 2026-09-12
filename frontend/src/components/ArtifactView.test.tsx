import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ArtifactView, artifactEntities, cellText } from './ArtifactView'
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

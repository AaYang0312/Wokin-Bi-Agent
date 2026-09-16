import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ArtifactView } from './ArtifactView'
import { AnalysisArtifact, parseAnalysisArtifact, resolveAnalysisSource } from './AnalysisArtifact'
import type { Artifact } from '../types'

const SOURCE_REF = '00000000-0000-4000-8000-000000000001'
const OWN_REF = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
const FINGERPRINT = 'a'.repeat(64)
const VERSION = 'isolated-analysis/2026-09-14.1'
const FAIL_MARK = '未通过展示侧校验'

/** 后端 `_finding` 用 sha256 前 24 位派生 ref；测试用同形状的合成值。 */
function finding(
  kind: string, metric: string, rowRefs: string[], values: Record<string, string>,
  statement: string, ref = `finding-${'a'.repeat(24)}`,
): Record<string, unknown> {
  return { finding_ref: ref, kind, metric, row_refs: rowRefs, values, statement_code: statement }
}

function analysisArtifact(overrides: Record<string, unknown> = {}): Artifact {
  return {
    artifact_id: OWN_REF,
    artifact_type: 'analysis_result',
    source_artifact_ref: SOURCE_REF,
    source_fingerprint: FINGERPRINT,
    analysis_version: VERSION,
    findings: [
      finding('contribution', 'paid_amount', ['row-001'],
        { value: '30.00', contribution: '0.300000' }, 'contribution_share'),
      finding('change_decomposition', 'paid_amount', ['row-001'],
        { current: '30.00', previous: '20.00', change: '10.00', change_rate: '0.500000' },
        'change_vs_previous', `finding-${'b'.repeat(24)}`),
    ],
    narrative: [],
    hypotheses: [],
    unsupported_claims: [],
    limitations: [],
    ...overrides,
  }
}

const sourceDataset: Artifact = {
  artifact_id: SOURCE_REF,
  artifact_type: 'metric_result',
  data_as_of: '2026-09-08T09:00:00+08:00',
  data: [{ shop_ref: 'ent-59f71124', paid_amount: '30.00' }],
}

describe('AnalysisArtifact 确定性结论与来源链接', () => {
  const html = renderToStaticMarkup(
    <ArtifactView artifact={analysisArtifact()} datasets={[sourceDataset, analysisArtifact()]} />)

  it('renders deterministic values and percent shares without recomputation', () => {
    expect(html).toContain('隔离分析结果')
    // 占比是后端量化好的 6 位小数，展示层只换算成百分号写法，不重算业务值。
    expect(html).toContain('30.00%')
    // 数值本体按字符串原样出现。
    expect(html).toContain('>30.00<')
    expect(html).toContain('变化率')
    expect(html).toContain('50.00%')
  })

  it('renders through ArtifactView dispatch and links the matched source', () => {
    expect(html).not.toMatch(/disabled/)
    expect(html).toContain('查看来源数据')
    expect(html).toContain('来源：metric_result')
    expect(html).toContain('2026-09-08T09:00:00+08:00')
  })

  it('keeps findings grouped in the fixed kind order with payload order inside', () => {
    const mixed = analysisArtifact({
      findings: [
        finding('anomaly_candidates', 'paid_amount', ['row-009'],
          { method: 'median_absolute_deviation', score: '120.735500', value: '100.00',
            center: '10.50', mad: '0.50' }, 'mad_outlier_candidate', `finding-${'c'.repeat(24)}`),
        finding('contribution', 'paid_amount', ['row-002'],
          { value: '70.00', contribution: '0.700000' }, 'contribution_share', `finding-${'d'.repeat(24)}`),
        finding('contribution', 'paid_amount', ['row-001'],
          { value: '30.00', contribution: '0.300000' }, 'contribution_share', `finding-${'e'.repeat(24)}`),
      ],
    })
    const view = renderToStaticMarkup(<AnalysisArtifact artifact={mixed} datasets={[]} />)
    const contribution = view.indexOf('贡献拆解')
    const anomaly = view.indexOf('异常候选')
    expect(contribution).toBeGreaterThanOrEqual(0)
    expect(anomaly).toBeGreaterThan(contribution)
    // 组内保持载荷顺序（后端已确定性排序）：夹具里 row-002 先给出，就先渲染 row-002。
    expect(view.indexOf('row-002')).toBeLessThan(view.indexOf('row-001'))
  })

  it('renders an empty finding set without inventing rows', () => {
    const empty = analysisArtifact({ findings: [] })
    const view = renderToStaticMarkup(<AnalysisArtifact artifact={empty} datasets={[]} />)
    expect(view).toContain('0 条')
    expect(view).not.toContain('contribution_share')
  })
})

describe('AnalysisArtifact 假设、证据不足与限制', () => {
  const labeled = analysisArtifact({
    findings: [finding('contribution', 'paid_amount', ['row-001'],
      { value: '30.00', contribution: '0.300000' }, 'contribution_share')],
    narrative: [
      { text: 'paid_amount 的本期值为 30.00。', finding_refs: [`finding-${'a'.repeat(24)}`],
        claim_kind: 'fact' },
      { text: '广告或投放节奏可能影响该值。', finding_refs: [`finding-${'a'.repeat(24)}`],
        claim_kind: 'hypothesis' },
    ],
    hypotheses: ['若广告预算减半，paid_amount 可能下降'],
    unsupported_claims: ['paid_amount 下降因为广告投放'],
    limitations: ['上一期快照缺失，变化拆分按现有期间计算'],
  })
  const html = renderToStaticMarkup(<AnalysisArtifact artifact={labeled} datasets={[]} />)

  it('labels hypotheses as pending validation', () => {
    expect(html).toContain('假设（待验证）')
    expect(html).toContain('若广告预算减半，paid_amount 可能下降')
    expect(html).toContain('待验证')
  })

  it('keeps unsupported claims out of the findings section', () => {
    expect(html).toContain('证据不足的说法')
    expect(html).toContain('paid_amount 下降因为广告投放')
    const findingsRegion = html.match(/data-testid="findings"[\s\S]*?<\/div>/)?.[0] ?? ''
    expect(findingsRegion).not.toBe('')
    expect(findingsRegion).not.toContain('因为广告投放')
  })

  it('keeps the causal hypothesis claim labeled, never as fact', () => {
    // 叙述里的 hypothesis 徽标必须可见；fact 徽标只属于 fact。
    expect((html.match(/待验证/g) ?? []).length).toBeGreaterThanOrEqual(2)
  })

  it('shows limitations always visible, outside any collapsed region', () => {
    expect(html).toContain('上一期快照缺失，变化拆分按现有期间计算')
    for (const details of html.match(/<details[\s\S]*?<\/details>/g) ?? []) {
      expect(details).not.toContain('上一期快照缺失')
    }
  })

  it('does not dump the raw payload or expose the fingerprint as text', () => {
    expect(html).not.toContain(FINGERPRINT)
    expect(html).not.toContain('source_fingerprint')
    expect(html).not.toContain('source_artifact_ref')
  })
})

describe('AnalysisArtifact 来源解析', () => {
  it('disables the source button when the source artifact is missing', () => {
    const html = renderToStaticMarkup(
      <AnalysisArtifact artifact={analysisArtifact()} datasets={[analysisArtifact()]} />)
    expect(html).toMatch(/disabled/)
    expect(html).toContain('来源数据未随本轮返回')
  })

  it('rejects a same-id artifact whose type is not an allowed source', () => {
    const html = renderToStaticMarkup(<AnalysisArtifact artifact={analysisArtifact()} datasets={[
      { artifact_id: SOURCE_REF, artifact_type: 'chart_spec' },
    ]} />)
    expect(html).toMatch(/disabled/)
  })

  it('requires exactly one matching artifact, not several', () => {
    const resolved = resolveAnalysisSource(
      [sourceDataset, sourceDataset], analysisArtifact())
    expect(resolved.ok).toBe(false)
  })

  it('resolves identity without fetching or guessing from URLs', () => {
    const resolved = resolveAnalysisSource([sourceDataset], analysisArtifact())
    expect(resolved.ok).toBe(true)
    if (resolved.ok) {
      expect(resolved.source.artifact_id).toBe(SOURCE_REF)
      expect(resolved.source.artifact_type).toBe('metric_result')
    }
  })
})

describe('AnalysisArtifact 百分号换算（精确十进制串，不经二进制浮点）', () => {
  function shareHtml(raw: string): string {
    return renderToStaticMarkup(<AnalysisArtifact artifact={analysisArtifact({
      findings: [finding('contribution', 'paid_amount', ['row-001'],
        { value: '999', contribution: raw }, 'contribution_share')],
    })} datasets={[]} />)
  }

  it('keeps negative shares exact', () => {
    expect(shareHtml('-0.125000')).toContain('-12.50%')
  })

  it('handles leading zeros and trailing zeros deterministically', () => {
    expect(shareHtml('0.005')).toContain('0.50%')
    expect(shareHtml('0.050000')).toContain('5.00%')
    expect(shareHtml('0.500000')).toContain('50.00%')
  })

  it('rounds half-to-even exactly, where binary floats round the wrong way', () => {
    // 0.01005 × 100 = 1.005：末位保留 0（偶）→ 1.00%，半远离零会错成 1.01%。
    expect(shareHtml('0.01005')).toContain('1.00%')
    // 0.01015 × 100 = 1.015：末位保留 1（奇）→ 进位成 1.02%。
    expect(shareHtml('0.01015')).toContain('1.02%')
  })

  it('scales a very large ratio exactly where Number would drift', () => {
    // 12345678901234567.89 × 100 = 1234567890123456789；二进制浮点会舍入成
    // 12345678901234568 × 100 = …800，toFixed(2) 漂成 1234567890123456800.00%。
    expect(shareHtml('12345678901234567.890000')).toContain('1234567890123456789.00%')
  })
})

describe('AnalysisArtifact fail closed', () => {
  const bad: Array<[string, Artifact]> = [
    ['unknown top-level field', { ...analysisArtifact(), raw_rows: [] }],
    ['malformed source ref', analysisArtifact({ source_artifact_ref: 'not-a-ref' })],
    ['malformed fingerprint', analysisArtifact({ source_fingerprint: 'a'.repeat(63) })],
    ['malformed version', analysisArtifact({ analysis_version: 'v1' })],
    ['missing finding key', analysisArtifact({
      findings: [{ finding_ref: `finding-${'a'.repeat(24)}`, kind: 'contribution',
        metric: 'paid_amount', row_refs: ['row-001'], values: { value: '30.00' } }],
    })],
    ['extra finding key', analysisArtifact({
      findings: [{ ...finding('contribution', 'paid_amount', ['row-001'],
        { value: '30.00' }, 'contribution_share'), extra: 1 }],
    })],
    ['unknown kind', analysisArtifact({
      findings: [finding('correlation', 'paid_amount', ['row-001'],
        { value: '30.00' }, 'contribution_share')],
    })],
    ['forbidden values key', analysisArtifact({
      findings: [finding('contribution', 'paid_amount', ['row-001'],
        { sql: 'select 1' }, 'contribution_share')],
    })],
    ['empty row_refs', analysisArtifact({
      findings: [finding('contribution', 'paid_amount', [], { value: '30.00' },
        'contribution_share')],
    })],
    ['unknown narrative ref', analysisArtifact({
      narrative: [{ text: '叙述。', finding_refs: ['finding-unknown'], claim_kind: 'fact' }],
    })],
    ['invalid claim kind', analysisArtifact({
      narrative: [{ text: '叙述。', finding_refs: [`finding-${'a'.repeat(24)}`],
        claim_kind: 'rumor' }],
    })],
    ['non-string unsupported claim', analysisArtifact({ unsupported_claims: [42] })],
    ['untrimmed limitation', analysisArtifact({ limitations: ['  有空白  '] })],
    ['too many findings', analysisArtifact({
      findings: Array.from({ length: 501 }, (_, index) => finding(
        'contribution', 'paid_amount', [`row-${String(index).padStart(3, '0')}`],
        { value: '30.00' }, 'contribution_share', `finding-${'a'.repeat(23)}${'abcdefghjkp'[index % 14]}`,
      )),
    })],
  ]
  for (const [name, artifact] of bad) {
    it(`refuses ${name} without rendering payload content`, () => {
      const html = renderToStaticMarkup(<AnalysisArtifact artifact={artifact} datasets={[]} />)
      expect(html).toContain(FAIL_MARK)
      expect(html).not.toContain('contribution_share')
      expect(html).not.toContain('30.00')
    })
  }

  it('parseAnalysisArtifact reports the reason kind, not payload text', () => {
    expect(parseAnalysisArtifact(analysisArtifact()).ok).toBe(true)
    expect(parseAnalysisArtifact(analysisArtifact({ source_fingerprint: 'zz' })).ok).toBe(false)
  })
})

describe('AnalysisArtifact 文本安全', () => {
  it('renders hostile value text as inert text, never markup', () => {
    const hostile = analysisArtifact({
      findings: [finding('contribution', 'paid_amount', ['row-001'],
        { value: '<img src=x onerror=alert(1)>', contribution: '0.300000' },
        'contribution_share')],
    })
    const html = renderToStaticMarkup(<AnalysisArtifact artifact={hostile} datasets={[]} />)
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img')
  })

  it('escapes hostile unsupported-claim text inside the collapsed region', () => {
    const hostile = analysisArtifact({
      unsupported_claims: ['<script>alert(1)</script>'],
    })
    const html = renderToStaticMarkup(<AnalysisArtifact artifact={hostile} datasets={[]} />)
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })
})

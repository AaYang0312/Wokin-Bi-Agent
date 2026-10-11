import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ArtifactView } from './components/ArtifactView'
import {
  parsePartialAnswer, partialAnswerDisclosure,
} from './partialAnswer'
import type { Artifact } from './types'

const ANSWERED = [['2026-09-01', '2026-09-29']] as const
const MISSING = [['2026-09-29', '2026-10-01']] as const
const DISCLOSURE = '仅覆盖 2026-09-01~2026-09-29，2026-09-29 起缺口'

function block(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    answered_windows: ANSWERED.map((pair) => [...pair]),
    missing_windows: MISSING.map((pair) => [...pair]),
    metric_refs: ['ad_spend'],
    disclosure: DISCLOSURE,
    ...overrides,
  }
}

function artifact(partial: unknown): Artifact {
  return {
    artifact_type: 'metric_result',
    status: 'ok',
    data: [{ ad_spend: '90.00' }],
    partial_answer: partial,
  }
}

describe('partial answer payload mirror', () => {
  it('renders the server disclosure and rejects a rewritten sentence', () => {
    expect(partialAnswerDisclosure(ANSWERED, MISSING)).toBe(DISCLOSURE)
    expect(parsePartialAnswer(block()).ok).toBe(true)
    const html = renderToStaticMarkup(<ArtifactView artifact={artifact(block())} />)
    expect(html).toContain(DISCLOSURE)
    expect(html).toContain('data-answered-window="true"')
    expect(html).toContain('覆盖 2026-09-01 至 2026-09-29')
    expect(html).toContain('data-missing-window="true"')
    expect(html).toContain('缺口 2026-09-29 至 2026-10-01')
    expect(html).not.toContain('未通过展示侧校验')

    const rejected = renderToStaticMarkup(
      <ArtifactView artifact={artifact(block({ disclosure: '9月基本完整' }))} />)
    expect(rejected).toContain('未通过展示侧校验')
    expect(rejected).not.toContain('90.00')
  })

  it('rejects empty windows, reversed windows and unknown keys', () => {
    expect(parsePartialAnswer(block({ answered_windows: [] })).ok).toBe(false)
    expect(parsePartialAnswer(block({
      answered_windows: [['2026-09-29', '2026-09-01']],
    })).ok).toBe(false)
    expect(parsePartialAnswer(block({ note: '完整' })).ok).toBe(false)
    expect(parsePartialAnswer(block({ metric_refs: ['ad_spend', 'ad_spend'] })).ok).toBe(false)
  })

  it('lists refused metrics and rejects a malformed refusal', () => {
    const html = renderToStaticMarkup(
      <ArtifactView artifact={{
        ...artifact(undefined),
        partial_answer: undefined,
        refused: [{ metric: 'ad_spend', reason: 'capability_unavailable' }],
      }} />)
    expect(html).toContain('data-refused="true"')
    expect(html).toContain('ad_spend：指标能力未开通')
    expect(html).toContain('data-refusal-reason="capability_unavailable"')
    const rejected = renderToStaticMarkup(
      <ArtifactView artifact={{
        ...artifact(block()),
        refused: [{ metric: 'ad_spend', reason: 'please_upload' }],
      }} />)
    expect(rejected).toContain('未通过展示侧校验')
    expect(rejected).not.toContain('90.00')
  })

  it('labels every refused reason code in Chinese, never as raw text', () => {
    const html = renderToStaticMarkup(
      <ArtifactView artifact={{
        ...artifact(undefined),
        partial_answer: undefined,
        refused: [
          { metric: 'ad_spend', reason: 'source_not_onboarded' },
          { metric: 'roas', reason: 'coverage_incomplete' },
          { metric: 'visitors', reason: 'basis_incompatible' },
          { metric: 'cart_count', reason: 'data_as_of_unknown' },
        ],
      }} />)
    expect(html).toContain('ad_spend：来源未接入')
    expect(html).toContain('roas：覆盖不完整')
    expect(html).toContain('visitors：口径不兼容')
    expect(html).toContain('cart_count：数据截止未知')
    // 裸码只允许出现在 data 属性里供追查，不允许作为可见文本。
    expect(html).not.toContain('：source_not_onboarded')
    expect(html).not.toContain('：coverage_incomplete')
    expect(html).not.toContain('：basis_incompatible')
    expect(html).not.toContain('：data_as_of_unknown')
  })
})

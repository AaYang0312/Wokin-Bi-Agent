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
})

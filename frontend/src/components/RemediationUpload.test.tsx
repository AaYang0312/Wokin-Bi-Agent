import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import { ArtifactView } from './ArtifactView'
import { MessageView } from './MessageView'
import {
  UPLOAD_REASON_LABELS, originalQuestionOf, parseRemediation,
  parseUploadResult, uploadErrorText, uploadResultText,
} from './RemediationUpload'
import type { Artifact, ChatMessage } from '../types'

const FAIL_MARK = '未通过展示侧校验'
const SHOP_REF = 'ent-59f71124'
const UPLOAD_ID = '0f0e0d0c-0b0a-4999-8999-000000000001'
const QUESTION = '沃金最近30天全站推广花费与ROAS'

/** 后端 `_remediation`（M3）公开形状的合法样本：键集精确、covered_window 可空。 */
function remediationBlock(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: 'upload', file_kind: '店铺维度',
    shop_refs: [SHOP_REF],
    covered_window: ['2026-09-01', '2026-09-30'],
    missing_range: ['2026-10-01', '2026-10-05'],
    ...overrides,
  }
}

function withoutKey(value: Record<string, unknown>, key: string): Record<string, unknown> {
  const copy = { ...value }
  delete copy[key]
  return copy
}

/** metric_result 公开载荷的最小合法样本（tool.py `artifact_event_payload` 的公开键）。 */
function metricArtifact(overrides: Record<string, unknown> = {}): Artifact {
  return {
    artifact_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
    artifact_type: 'metric_result',
    status: 'missing_data',
    metric_definition: { metric: 'ad_spend' },
    coverage: { status: 'partial' },
    limitations: [],
    data_as_of: '2026-09-30T00:00:00+08:00',
    filters: { start: '2026-10-01', end: '2026-10-06' },
    data: [{ day: '2026-09-30', shop_ref: SHOP_REF, ad_spend: '12.00' }],
    basis: [],
    diagnostics: {},
    entities: [{
      ref: SHOP_REF, kind: 'shop', display_name: '沃金五金机电商城', name_source: 'archive',
    }],
    catalog_version: 'catalog/2026-09-30.1',
    remediation: remediationBlock(),
    ...overrides,
  }
}

function chat(id: string, role: ChatMessage['role'], content: string,
  artifacts: Artifact[] = []): ChatMessage {
  return {
    id, role, content, artifacts, status: 'complete',
    created_at: '2026-10-07T10:00:00+08:00',
  }
}

describe('RemediationUpload 结构化 remediation 白名单渲染', () => {
  const html = renderToStaticMarkup(<ArtifactView artifact={metricArtifact()} />)

  it('renders the upload button only from the structured block', () => {
    expect(html).toContain('上传缺失文件')
    expect(html).toMatch(/accept="\.xlsx"/)
  })

  it('states the missing window, covered window and shop names from the payload', () => {
    expect(html).toContain('缺失区间：2026-10-01 至 2026-10-05')
    expect(html).toContain('已覆盖：2026-09-01 至 2026-09-30')
    expect(html).toContain('沃金五金机电商城')
  })

  it('keeps the re-query button out of the idle render', () => {
    expect(html).not.toContain('重新查询')
  })

  it('renders ordinary cards without the button when no remediation key exists', () => {
    const plain = renderToStaticMarkup(
      <ArtifactView artifact={metricArtifact({ remediation: undefined })} />)
    expect(plain).toContain('查询结果')
    expect(plain).toContain('12.00')
    expect(plain).not.toContain('上传缺失文件')
  })

  it('omits the covered line instead of faking one when covered_window is null', () => {
    const uncovered = renderToStaticMarkup(
      <ArtifactView artifact={metricArtifact({ remediation: remediationBlock({ covered_window: null }) })} />)
    expect(uncovered).toContain('上传缺失文件')
    expect(uncovered).not.toContain('已覆盖')
  })
})

describe('RemediationUpload 载荷验形不合法整卡拒绝', () => {
  const badRemediations: Array<[string, unknown]> = [
    ['an absent required key', withoutKey(remediationBlock(), 'missing_range')],
    ['an absent kind', withoutKey(remediationBlock(), 'kind')],
    ['an unknown extra key', remediationBlock({ upload_url: 'https://attacker.example' })],
    ['an unknown kind', remediationBlock({ kind: 'resend' })],
    ['a file kind outside the vocabulary', remediationBlock({ file_kind: '商品维度' })],
    ['shop refs outside the ref shape', remediationBlock({ shop_refs: ['shop_1'] })],
    ['shop refs that are not a list', remediationBlock({ shop_refs: SHOP_REF })],
    ['a missing_range that is not a pair', remediationBlock({ missing_range: ['2026-10-01'] })],
    ['a missing_range outside ISO dates', remediationBlock({
      missing_range: ['2026/10/01', '2026-10-05'] })],
    ['a covered_window of the wrong shape', remediationBlock({ covered_window: '2026-09' })],
    ['covered_window dates outside ISO', remediationBlock({
      covered_window: ['2026-09-01', '明早'] })],
    ['a remediation that is model text', '请上传店铺维度手工导出文件'],
    ['a remediation that is a list', [remediationBlock()]],
  ]
  for (const [name, block] of badRemediations) {
    it(`refuses ${name} without rendering any of the card`, () => {
      const html = renderToStaticMarkup(
        <ArtifactView artifact={metricArtifact({ remediation: block })} />)
      expect(html).toContain(FAIL_MARK)
      expect(html).not.toContain('上传缺失文件')
      // 整卡拒绝：数据行也不能借道渲染。
      expect(html).not.toContain('12.00')
    })
  }

  it('reports a stable reason from the parser, never payload text', () => {
    expect(parseRemediation(remediationBlock()).ok).toBe(true)
    expect(parseRemediation('请上传')).toEqual({
      ok: false, reason: 'remediation_payload_invalid' })
  })

  it('accepts exactly the backend shape: covered_window nullable or absent, refs possibly empty', () => {
    expect(parseRemediation(remediationBlock({ covered_window: null })).ok).toBe(true)
    expect(parseRemediation(withoutKey(remediationBlock(), 'covered_window')).ok).toBe(true)
    expect(parseRemediation(remediationBlock({ shop_refs: [] })).ok).toBe(true)
  })
})

describe('RemediationUpload 按钮只认结构化块，不认模型文本', () => {
  it('does not render the button because the model text asks to upload', () => {
    const html = renderToStaticMarkup(<MessageView message={chat(
      'a-1', 'assistant', '请上传店铺维度手工导出文件，我再重新统计。',
      [metricArtifact({ remediation: undefined })])} />)
    expect(html).toContain('请上传')
    expect(html).not.toContain('上传缺失文件')
  })

  it('rejects the whole card when the text asks to upload and the block is malformed', () => {
    const html = renderToStaticMarkup(<MessageView message={chat(
      'a-2', 'assistant', '请上传文件。',
      [metricArtifact({ remediation: remediationBlock({ kind: 'resend' }) })])} />)
    expect(html).toContain(FAIL_MARK)
    expect(html).not.toContain('上传缺失文件')
  })
})

describe('上传结果文案（前端常量，覆盖后端七码）', () => {
  const accepted = parseUploadResult({
    upload_id: UPLOAD_ID, status: 'accepted', reason_code: null,
    rows_read: 61, rows_written: 61, shops: ['沃金五金机电商城'],
  })

  it('shows row counts and shops on acceptance', () => {
    expect(accepted).toEqual({
      ok: true, kind: 'accepted', rowsRead: 61, rowsWritten: 61,
      shops: ['沃金五金机电商城'],
    })
    expect(uploadResultText(accepted)).toContain('61')
    expect(uploadResultText(accepted)).toContain('沃金五金机电商城')
  })

  it('covers exactly the seven backend reason codes with human copy', () => {
    const codes = ['missing_columns', 'bad_date', 'shop_not_registered', 'row_overflow',
      'pii_columns_rejected', 'parse_failed', 'not_imported_this_round']
    expect(Object.keys(UPLOAD_REASON_LABELS).sort()).toEqual([...codes].sort())
    for (const code of codes) {
      const text = uploadResultText({ ok: true, kind: 'rejected', reasonCode: code })
      expect(text).toContain(UPLOAD_REASON_LABELS[code])
      // 拒绝文案是人话：已登记的码不再把原文码抛给用户。
      expect(text).not.toContain(code)
    }
  })

  it('shows unknown reason codes verbatim instead of guessing', () => {
    const text = uploadResultText({ ok: true, kind: 'rejected', reasonCode: 'future_code' })
    expect(text).toContain('future_code')
  })

  it('refuses result payloads that violate the contract shape', () => {
    const base: Record<string, unknown> = {
      upload_id: UPLOAD_ID, status: 'accepted', reason_code: null,
      rows_read: 1, rows_written: 1, shops: ['沃金五金机电商城'],
    }
    const bad: unknown[] = [
      { ...base, upload_id: 'not-a-uuid' },
      { ...base, extra: 1 },
      withoutKey(base, 'shops'),
      { ...base, status: 'ok' },
      { ...base, reason_code: 'missing_columns' },
      { ...base, status: 'rejected', reason_code: null },
      { ...base, rows_read: -1 },
      { ...base, rows_written: 1.5 },
      { ...base, shops: [42] },
    ]
    for (const payload of bad) {
      expect(parseUploadResult(payload)).toEqual({ ok: false })
    }
  })
})

describe('上传请求失败兜底文案（非 2xx）', () => {
  it('says the feature is off on 404', () => {
    expect(uploadErrorText(404)).toContain('未启用')
  })

  it('gives a bounded fallback for every other status, including network failure', () => {
    for (const status of [403, 422, 500, 503]) {
      expect(uploadErrorText(status)).toContain(String(status))
    }
    expect(uploadErrorText(null)).toContain('可重试')
  })
})

describe('重新查询重发原问题原文', () => {
  const question = chat('u-1', 'user', QUESTION)
  const answer = chat('a-1', 'assistant', '该窗口缺少手工导入数据。', [metricArtifact()])

  it('resolves the nearest preceding user question as the resend text', () => {
    expect(originalQuestionOf([question, answer], 1)).toBe(QUESTION)
  })

  it('walks past assistant messages to reach the question', () => {
    const filler = chat('a-0', 'assistant', '……')
    expect(originalQuestionOf([question, filler, answer], 2)).toBe(QUESTION)
  })

  it('has nothing to resend when no user question precedes', () => {
    expect(originalQuestionOf([answer], 0)).toBeNull()
  })

  it('never leaks the original question into the static markup', () => {
    const html = renderToStaticMarkup(
      <MessageView message={answer} questionText={QUESTION} />)
    expect(html).not.toContain(QUESTION)
    expect(html).toContain('上传缺失文件')
  })
})

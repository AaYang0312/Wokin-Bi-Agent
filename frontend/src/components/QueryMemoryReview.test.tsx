import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  ApiError, createQueryMemoryDraft, decideQueryMemoryDraft, probeReviewAccess,
} from '../api'
import App, { ReviewEntry } from '../App'
import type { QueryMemoryCandidate, QueryMemoryDraft } from '../types'
import {
  decisionFromState, draftRequest, initialReviewForm, QueryMemoryReview,
  QueryMemoryReviewSection, reviewBlocker, reviewFailure, reviewReducer,
  slotNamesFromTemplate,
} from './QueryMemoryReview'
import type { ReviewFormState } from './QueryMemoryReview'

// 审核面板（计划 2026-09-14-approved-query-memory.md Task 4 Step 4）。
// 仓库的组件测试跑在 node + renderToStaticMarkup 上（无 DOM/无 testing-library）：
// 交互契约（显式理由、替换选择）收口在纯函数 `decisionFromState` / `draftRequest` /
// `reviewBlocker` 里，组件只做接线——这里对两者都直接断言。

const RUN_REF = 'run-550e8400-e29b-41d4-a716-446655440000'

const VERSIONS = {
  schema_version: 'reporting/2026-09-14.1',
  semantic_catalog_version: 'semantic/2026-09-14.1',
  data_catalog_version: 7,
  metric_version: 'metrics/2026-09-12.1',
  policy_version: 'multi-source-policy/2026-09-12.1',
  source_registry_version: 'sources/2026-09-12.1',
  graph_version: 'business_query-graph/2026-09-11.1',
}

const draft: QueryMemoryDraft = {
  example_ref: 'mem-550e8400',
  domain: 'controlled_sql_exploration',
  intent_signature: 'cost-by-shop-and-window',
  question_template: '比较 {shop_scope} 在 {date_window} 的成本',
  slots: [
    { name: 'shop_scope', kind: 'entity_scope' },
    { name: 'date_window', kind: 'date_window' },
  ],
  normalized_request: { requested_metric_refs: ['metric-cost-total'] },
  expected_tool: 'explore_business_data',
  version_requirements: VERSIONS,
  status: 'draft',
  approval_revision: 0,
  source_run_ref: RUN_REF,
}

const candidate: QueryMemoryCandidate = {
  source_run_ref: RUN_REF,
  domain: 'controlled_sql_exploration',
  normalized_request: { requested_metric_refs: ['metric-cost-total'] },
  expected_tool: 'explore_business_data',
  version_requirements: VERSIONS,
  created_at: '2026-09-14T12:00:00+08:00',
}

function approved(ref: string, domain: string): QueryMemoryDraft {
  return { ...draft, example_ref: ref, domain, status: 'approved', approval_revision: 1 }
}

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init) as Response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('审核决定的显式理由门（decisionFromState）', () => {
  it('refuses to decide without an explicit reason', () => {
    const outcome = decisionFromState({
      ...initialReviewForm, target: 'mem-a', action: 'approve', reason: '   ',
    })
    expect(outcome.error).toBe('请填写审核理由')
    expect(outcome.decision).toBeNull()
  })

  it('supersede requires an approved replacement before it can be sent', () => {
    const outcome = decisionFromState({
      ...initialReviewForm, target: 'mem-a', action: 'supersede',
      reason: '口径已升级', replacementRef: '',
    })
    expect(outcome.error).toBe('请选择同领域已批准的替换样例')
    expect(outcome.decision).toBeNull()
  })

  it('builds the supersede payload only with reason and replacement', () => {
    const outcome = decisionFromState({
      ...initialReviewForm, target: 'mem-a', action: 'supersede',
      reason: '口径已升级', replacementRef: 'mem-b',
    })
    expect(outcome.error).toBeNull()
    expect(outcome.decision).toEqual({
      exampleRef: 'mem-a', action: 'supersede', reason: '口径已升级',
      replacementRef: 'mem-b',
    })
  })

  it('approve and revoke never carry a replacement', () => {
    for (const action of ['approve', 'revoke'] as const) {
      const outcome = decisionFromState({
        ...initialReviewForm, target: 'mem-a', action, reason: '血缘与模板复核通过',
      })
      expect(outcome.decision).toEqual({
        exampleRef: 'mem-a', action, reason: '血缘与模板复核通过', replacementRef: undefined,
      })
    }
  })

  it('reviewBlocker states the same rules on its own', () => {
    expect(reviewBlocker('approve', '')).toBe('请填写审核理由')
    expect(reviewBlocker('approve', '血缘与模板复核通过')).toBeNull()
    expect(reviewBlocker('supersede', '口径已升级')).toBe('请选择同领域已批准的替换样例')
    expect(reviewBlocker('supersede', '口径已升级', 'mem-b')).toBeNull()
  })
})

describe('草稿表单门（draftRequest）', () => {
  it('requires a selected run, a slotted template and a kind per slot', () => {
    expect(draftRequest(initialReviewForm).error).toBe('请选择来源运行')
    const picked = reviewReducer(initialReviewForm, { type: 'pick-run', runRef: RUN_REF })
    expect(draftRequest(picked).error).toBe('请填写槽位化问题模板')
    const templated = reviewReducer(picked, {
      type: 'set-template', template: '比较店铺的成本',
    })
    expect(draftRequest(templated).error).toBe('模板必须包含槽位占位符')
    const slotted = reviewReducer(picked, {
      type: 'set-template', template: '比较 {shop_scope} 在 {date_window} 的成本',
    })
    expect(draftRequest(slotted).error).toBeNull()
    expect(draftRequest(slotted).request).toEqual({
      source_run_ref: RUN_REF,
      question_template: '比较 {shop_scope} 在 {date_window} 的成本',
      slots: [
        { name: 'shop_scope', kind: 'entity_scope' },
        { name: 'date_window', kind: 'entity_scope' },
      ],
    })
  })

  it('tracks slot kinds through template edits and drops removed placeholders', () => {
    let state = reviewReducer(initialReviewForm, {
      type: 'set-template', template: '比较 {shop_scope} 在 {date_window} 的成本',
    })
    state = reviewReducer(state, { type: 'set-slot-kind', name: 'date_window', kind: 'date_window' })
    expect(state.slotKinds).toEqual({ shop_scope: 'entity_scope', date_window: 'date_window' })
    state = reviewReducer(state, { type: 'set-template', template: '看 {shop_scope} 的成本' })
    expect(state.slotKinds).toEqual({ shop_scope: 'entity_scope' })
  })

  it('dedupes repeated placeholders', () => {
    expect(slotNamesFromTemplate('{shop_scope} 与 {shop_scope} 与 {date_window}'))
      .toEqual(['shop_scope', 'date_window'])
  })

  it('clears the previous decision when switching review targets', () => {
    let state = reviewReducer(initialReviewForm, {
      type: 'decide', exampleRef: 'mem-a', action: 'supersede',
    })
    state = reviewReducer(state, { type: 'set-reason', reason: '口径已升级' })
    state = reviewReducer(state, { type: 'set-replacement', replacementRef: 'mem-b' })
    const switched = reviewReducer(state, {
      type: 'decide', exampleRef: 'mem-c', action: 'approve',
    })
    expect(switched.target).toBe('mem-c')
    expect(switched.reason).toBe('')
    expect(switched.replacementRef).toBe('')
  })
})

describe('QueryMemoryReview 渲染', () => {
  const html = renderToStaticMarkup(
    <QueryMemoryReview candidates={[candidate]} drafts={[draft]}
      onDecide={() => undefined} onCreateDraft={() => undefined} />,
  )

  it('renders the slotified template, slots, tool, domain and revision', () => {
    expect(html).toContain('比较 {shop_scope} 在 {date_window} 的成本')
    expect(html).toContain('date_window')
    expect(html).toContain('entity_scope')
    expect(html).toContain('explore_business_data')
    expect(html).toContain('controlled_sql_exploration')
    expect(html).toContain(RUN_REF)
  })

  it('renders slots but no bound business values', () => {
    expect(html).not.toContain('2026-09-01')
    expect(html).not.toContain('99.00')
  })

  it('renders no owner, authorization, reason, SQL or result fields', () => {
    for (const forbidden of ['sql_text', 'subject_id', 'shop_id', 'owner_subject_id',
      'authorization_refs', 'created_by', 'actor', 'error_code', 'chat_messages',
      'query_artifacts']) {
      expect(html).not.toContain(forbidden)
    }
  })

  it('offers approve and revoke on a draft; supersede only on approved records', () => {
    // 生命周期：draft → approve/revoke；supersede 只能从 approved 发起。
    expect(html).toContain('>批准</button>')
    expect(html).toContain('>撤销</button>')
    expect(html).not.toContain('>替换</button>')
  })

  it('hides approve on non-draft records but keeps revoke and supersede', () => {
    const approvedHtml = renderToStaticMarkup(
      <QueryMemoryReview candidates={[]} drafts={[approved('mem-a', 'controlled_sql_exploration')]}
        onDecide={() => undefined} onCreateDraft={() => undefined} />,
    )
    // 「已批准」状态标签里含「批准」二字，所以精确断言按钮本身。
    expect(approvedHtml).not.toContain('>批准</button>')
    expect(approvedHtml).toContain('>撤销</button>')
    expect(approvedHtml).toContain('>替换</button>')
  })

  it('shows only same-domain approved replacements for supersede', () => {
    const state: ReviewFormState = {
      ...initialReviewForm, target: 'mem-target', action: 'supersede',
    }
    const html = renderToStaticMarkup(
      <QueryMemoryReview
        candidates={[]}
        drafts={[
          approved('mem-target', 'controlled_sql_exploration'),
          approved('mem-same', 'controlled_sql_exploration'),
          approved('mem-other', 'business_query'),
          { ...approved('mem-revoked', 'controlled_sql_exploration'), status: 'revoked' },
        ]}
        onDecide={() => undefined} onCreateDraft={() => undefined}
        form={state} onDispatch={() => undefined} />,
    )
    expect(html).toContain('<option value="mem-same"')
    expect(html).not.toContain('<option value="mem-other"')
    expect(html).not.toContain('<option value="mem-revoked"')
    expect(html).not.toContain('<option value="mem-target"')
    expect(html).toContain('替换为（同领域已批准）')
  })

  it('does not render the replacement select for approve decisions', () => {
    const state: ReviewFormState = {
      ...initialReviewForm, target: 'mem-a', action: 'approve',
    }
    const html = renderToStaticMarkup(
      <QueryMemoryReview candidates={[]} drafts={[approved('mem-a', 'business_query')]}
        onDecide={() => undefined} onCreateDraft={() => undefined}
        form={state} onDispatch={() => undefined} />,
    )
    expect(html).toContain('确认批准')
    expect(html).not.toContain('替换为（同领域已批准）')
  })
})

describe('审核面板容器（404 隐藏 / 403 拒绝）', () => {
  it('maps errors: 404 hides, 403 denies, others pass through', () => {
    expect(reviewFailure(new ApiError(404, '功能未启用'))).toBe('hide')
    expect(reviewFailure(new ApiError(403, '无审核权限'))).toBe('denied')
    expect(reviewFailure(new Error('网络中断'))).toBe('网络中断')
    expect(reviewFailure('boom')).toBe('加载失败')
  })

  it('renders the denial notice instead of the panel on 403', () => {
    expect(renderToStaticMarkup(<QueryMemoryReviewSection />)).toContain('正在加载')
  })
})

describe('审核 API（api.ts）', () => {
  it('creates drafts through the web write boundary with only the three fields', async () => {
    const fetchMock = stubFetch((url, init) => {
      expect(url).toBe('/api/query-memory/drafts')
      expect(init?.method).toBe('POST')
      expect(init?.credentials).toBe('same-origin')
      expect(init?.headers).toMatchObject({
        'X-BI-Agent': 'web', 'Content-Type': 'application/json',
      })
      return { ok: true, json: async () => draft }
    })
    const created = await createQueryMemoryDraft({
      source_run_ref: RUN_REF,
      question_template: '比较 {shop_scope} 在 {date_window} 的成本',
      slots: [{ name: 'shop_scope', kind: 'entity_scope' }],
    })
    expect(created.example_ref).toBe('mem-550e8400')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      source_run_ref: RUN_REF,
      question_template: '比较 {shop_scope} 在 {date_window} 的成本',
      slots: [{ name: 'shop_scope', kind: 'entity_scope' }],
    })
  })

  it('sends supersede with replacement_ref and approve with reason only', async () => {
    const fetchMock = stubFetch(() => ({ ok: true, json: async () => draft }))
    await decideQueryMemoryDraft('mem-a', 'supersede', '口径已升级', 'mem-b')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/query-memory/drafts/mem-a/supersede')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)))
      .toEqual({ reason: '口径已升级', replacement_ref: 'mem-b' })
    await decideQueryMemoryDraft('mem-a', 'approve', '血缘与模板复核通过')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/query-memory/drafts/mem-a/approve')
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body)))
      .toEqual({ reason: '血缘与模板复核通过' })
    // 撤销同样只带显式理由：它是 Task 6 验收的主线，前端不得替审核者编理由。
    await decideQueryMemoryDraft('mem-a', 'revoke', '证据失效，立即撤销')
    expect(fetchMock.mock.calls[2][0]).toBe('/api/query-memory/drafts/mem-a/revoke')
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body)))
      .toEqual({ reason: '证据失效，立即撤销' })
  })

  it('throws ApiError with the server envelope on failures', async () => {
    stubFetch(() => ({
      ok: false, status: 409,
      json: async () => ({ code: 'memory_revision_conflict', message: '记录已被其他人更新' }),
    }))
    await expect(decideQueryMemoryDraft('mem-a', 'approve', '复核通过'))
      .rejects.toMatchObject({ status: 409, message: '记录已被其他人更新' })
  })

  it('maps the candidate probe to review access', async () => {
    stubFetch(() => ({ ok: true, json: async () => [candidate] }))
    expect(await probeReviewAccess()).toBe('available')
    stubFetch(() => ({
      ok: false, status: 403,
      json: async () => ({ code: 'forbidden', message: '无审核权限' }),
    }))
    expect(await probeReviewAccess()).toBe('forbidden')
    stubFetch(() => ({
      ok: false, status: 404,
      json: async () => ({ code: 'not_found', message: '功能未启用' }),
    }))
    expect(await probeReviewAccess()).toBe('off')
    stubFetch(() => {
      throw new TypeError('网络中断')
    })
    expect(await probeReviewAccess()).toBe('unavailable')
  })
})

describe('App 审核入口（只随后端能力出现）', () => {
  it('hides the entry on 404-off, 403 and unknown access', () => {
    for (const access of ['off', 'forbidden', 'unavailable', null] as const) {
      expect(renderToStaticMarkup(
        <ReviewEntry access={access} open={false} onToggle={() => undefined} />,
      )).toBe('')
    }
  })

  it('appears only when the backend confirmed review capability', () => {
    const html = renderToStaticMarkup(
      <ReviewEntry access="available" open={false} onToggle={() => undefined} />,
    )
    expect(html).toContain('审核记忆')
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('aria-controls="review-dock"')
  })

  it('keeps the chat surface free of any review entry by default', () => {
    const html = renderToStaticMarkup(<App />)
    expect(html).not.toContain('审核记忆')
    expect(html).not.toContain('review-dock')
    expect(html).not.toContain('review-toggle')
  })
})

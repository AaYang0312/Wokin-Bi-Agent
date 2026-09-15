import { useEffect, useReducer, useState } from 'react'

import {
  ApiError, createQueryMemoryDraft, decideQueryMemoryDraft,
  listQueryMemoryCandidates, listQueryMemoryDrafts,
} from '../api'
import type {
  ApprovalAction, QueryMemoryCandidate, QueryMemoryDraft, QueryMemorySlot,
  QueryMemorySlotKind,
} from '../types'

// 记忆审核面板（计划 2026-09-14-approved-query-memory.md Task 4 Step 5）。
//
// 这是审核者专属的独立表面：不进聊天消息流，也不是模型上下文。渲染的数据只有
// 后端投影过的安全字段（模板、槽位、工具、domain、版本、revision、opaque ref）。
// 交互契约收口在下面的纯函数里（无 DOM 也可测）：
//
// 1. 每个决定（批准/撤销/替换）都必须带显式的非空白理由；
// 2. supersede 还必须从**同领域已批准**记录里选一个替换目标；
// 3. 404（功能关）整个面板隐藏，403（无权限）显示拒绝。

export type DecisionRequest = {
  exampleRef: string
  action: ApprovalAction
  reason: string
  replacementRef?: string
}

export type DraftRequest = {
  source_run_ref: string
  question_template: string
  slots: QueryMemorySlot[]
}

export type ReviewFormState = {
  runRef: string | null
  template: string
  slotKinds: Partial<Record<string, QueryMemorySlotKind>>
  target: string | null
  action: ApprovalAction
  reason: string
  replacementRef: string
  notice: string | null
}

export const initialReviewForm: ReviewFormState = {
  runRef: null,
  template: '',
  slotKinds: {},
  target: null,
  action: 'approve',
  reason: '',
  replacementRef: '',
  notice: null,
}

export type ReviewFormEvent =
  | { type: 'pick-run'; runRef: string }
  | { type: 'set-template'; template: string }
  | { type: 'set-slot-kind'; name: string; kind: QueryMemorySlotKind }
  | { type: 'decide'; exampleRef: string; action: ApprovalAction }
  | { type: 'set-reason'; reason: string }
  | { type: 'set-replacement'; replacementRef: string }
  | { type: 'notice'; notice: string | null }

/** 每个决定都必须带显式理由；supersede 必须指名同领域已批准的替换样例。 */
export function reviewBlocker(
  action: ApprovalAction,
  reason: string,
  replacementRef?: string,
): string | null {
  if (!reason.trim()) return '请填写审核理由'
  if (action === 'supersede' && !(replacementRef ?? '').trim()) {
    return '请选择同领域已批准的替换样例'
  }
  return null
}

/** 状态 → 审核决定负载的唯一路径：理由/替换门在这里，组件只接线。 */
export function decisionFromState(
  state: Pick<ReviewFormState, 'target' | 'action' | 'reason' | 'replacementRef'>,
): { error: string | null; decision: DecisionRequest | null } {
  const replacementRef = state.action === 'supersede'
    ? state.replacementRef.trim() || undefined
    : undefined
  const problem = reviewBlocker(state.action, state.reason, replacementRef)
  if (problem || !state.target) {
    return { error: problem ?? '请选择要审核的记录', decision: null }
  }
  return {
    error: null,
    decision: {
      exampleRef: state.target, action: state.action,
      reason: state.reason.trim(), replacementRef,
    },
  }
}

const PLACEHOLDER_RE = /\{([a-z][a-z0-9_]{0,31})\}/g

export function slotNamesFromTemplate(template: string): string[] {
  const names: string[] = []
  for (const match of template.matchAll(PLACEHOLDER_RE)) {
    if (!names.includes(match[1])) names.push(match[1])
  }
  return names
}

/** 草稿表单 → 创建负载的唯一路径：必须有来源运行和槽位化模板。 */
export function draftRequest(
  state: Pick<ReviewFormState, 'runRef' | 'template' | 'slotKinds'>,
): { error: string | null; request: DraftRequest | null } {
  if (!state.runRef) return { error: '请选择来源运行', request: null }
  if (!state.template.trim()) return { error: '请填写槽位化问题模板', request: null }
  const names = slotNamesFromTemplate(state.template)
  if (names.length === 0) return { error: '模板必须包含槽位占位符', request: null }
  return {
    error: null,
    request: {
      source_run_ref: state.runRef,
      question_template: state.template,
      slots: names.map((name) => ({ name, kind: state.slotKinds[name] ?? 'entity_scope' })),
    },
  }
}

export function reviewReducer(state: ReviewFormState, event: ReviewFormEvent): ReviewFormState {
  switch (event.type) {
    case 'pick-run':
      return { ...state, runRef: event.runRef, notice: null }
    case 'set-template': {
      // 模板编辑时同步槽位：新占位符给默认类型，删掉的占位符连同类型一起清掉。
      const slotKinds: ReviewFormState['slotKinds'] = {}
      for (const name of slotNamesFromTemplate(event.template)) {
        slotKinds[name] = state.slotKinds[name] ?? 'entity_scope'
      }
      return { ...state, template: event.template, slotKinds, notice: null }
    }
    case 'set-slot-kind':
      return { ...state, slotKinds: { ...state.slotKinds, [event.name]: event.kind } }
    case 'decide':
      // 换审核对象时清掉上一次的理由与替换选择，避免串场。
      return {
        ...state, target: event.exampleRef, action: event.action,
        reason: '', replacementRef: '', notice: null,
      }
    case 'set-reason':
      return { ...state, reason: event.reason }
    case 'set-replacement':
      return { ...state, replacementRef: event.replacementRef }
    case 'notice':
      return { ...state, notice: event.notice }
  }
}

/** 请求失败 → 面板状态：404 整体隐藏，403 明确拒绝，其余作为提示文本。 */
export function reviewFailure(error: unknown): 'hide' | 'denied' | string {
  if (error instanceof ApiError) {
    if (error.status === 404) return 'hide'
    if (error.status === 403) return 'denied'
  }
  return error instanceof Error ? error.message : '加载失败'
}

const SLOT_KINDS: QueryMemorySlotKind[] = [
  'entity_scope', 'date_window', 'target_price', 'threshold', 'budget',
]
const ACTION_LABEL: Record<ApprovalAction, string> = {
  approve: '批准', revoke: '撤销', supersede: '替换',
}
const STATUS_LABEL: Record<QueryMemoryDraft['status'], string> = {
  draft: '草稿', approved: '已批准', superseded: '已替换', revoked: '已撤销',
}
const VERSION_LABEL: Record<string, string> = {
  schema_version: 'schema',
  semantic_catalog_version: '语义目录',
  data_catalog_version: '数据目录',
  metric_version: '指标',
  policy_version: '策略',
  source_registry_version: '来源',
  graph_version: '图',
}

function SlotTags({ slots }: { slots: QueryMemorySlot[] }) {
  return (
    <span className="review-meta">
      {slots.map((slot) => (
        <code className="review-slot" key={slot.name}>{slot.name}:{slot.kind}</code>
      ))}
    </span>
  )
}

function VersionTags({ versions }: { versions: QueryMemoryDraft['version_requirements'] }) {
  return (
    <span className="review-meta">
      {Object.entries(versions).map(([key, value]) => (
        <code className="review-slot" key={key}>
          {VERSION_LABEL[key] ?? key}:{String(value)}
        </code>
      ))}
    </span>
  )
}

export function QueryMemoryReview({
  candidates, drafts, onDecide, onCreateDraft, form, onDispatch,
}: {
  candidates: QueryMemoryCandidate[]
  drafts: QueryMemoryDraft[]
  onDecide: (decision: DecisionRequest) => void
  onCreateDraft: (request: DraftRequest) => void
  /** 可选的受控表单状态：测试与外层容器可以直接注入/接管。 */
  form?: ReviewFormState
  onDispatch?: (event: ReviewFormEvent) => void
}) {
  const [internalForm, internalDispatch] = useReducer(reviewReducer, initialReviewForm)
  const state = form ?? internalForm
  const dispatch = onDispatch ?? internalDispatch
  const slots = slotNamesFromTemplate(state.template)

  return (
    <>
      {state.notice && <p className="review-notice" role="alert">{state.notice}</p>}

      <h3 className="review-kicker">候选来源（成功运行）</h3>
      {candidates.length === 0 ? <p className="review-empty">暂无可审核的候选运行</p> : (
        <ul className="review-list">
          {candidates.map((item) => (
            <li className={`review-card${state.runRef === item.source_run_ref ? ' selected' : ''}`}
              key={item.source_run_ref}>
              <code className="review-ref">{item.source_run_ref}</code>
              <span className="review-meta">
                <span className="review-tag">{item.domain}</span>
                <span className="review-tag">{item.expected_tool}</span>
              </span>
              <span className="review-actions">
                <button type="button" className="review-btn"
                  onClick={() => dispatch({ type: 'pick-run', runRef: item.source_run_ref })}>
                  选为来源
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {state.runRef && (
        <form className="review-form" onSubmit={(event) => {
          event.preventDefault()
          const outcome = draftRequest(state)
          if (outcome.error || !outcome.request) {
            dispatch({ type: 'notice', notice: outcome.error ?? '草稿参数不完整' })
            return
          }
          onCreateDraft(outcome.request)
        }}>
          <p className="review-empty">为 {state.runRef} 填写去标识化的问题模板：</p>
          <label className="review-field">
            问题模板（业务值一律写成槽位）
            <textarea value={state.template}
              onChange={(event) => dispatch({ type: 'set-template', template: event.target.value })} />
          </label>
          {slots.map((name) => (
            <label className="review-field" key={name}>
              槽位 {name}
              <select value={state.slotKinds[name] ?? 'entity_scope'}
                onChange={(event) => dispatch({
                  type: 'set-slot-kind', name, kind: event.target.value as QueryMemorySlotKind,
                })}>
                {SLOT_KINDS.map((kind) => <option value={kind} key={kind}>{kind}</option>)}
              </select>
            </label>
          ))}
          <span className="review-actions">
            <button type="submit" className="review-btn primary">创建草稿</button>
          </span>
        </form>
      )}

      <h3 className="review-kicker">审核队列</h3>
      {drafts.length === 0 ? <p className="review-empty">暂无待审核记录</p> : (
        <ul className="review-list">
          {drafts.map((record) => {
            const deciding = state.target === record.example_ref
            // 替换选择只来自同领域、已批准、且不是它自己的记录。
            const choices = drafts.filter((choice) =>
              choice.status === 'approved' && choice.domain === record.domain
              && choice.example_ref !== record.example_ref)
            return (
              <li className={`review-card${deciding ? ' selected' : ''}`}
                key={record.example_ref}>
                <p className="review-template">{record.question_template}</p>
                <SlotTags slots={record.slots} />
                <VersionTags versions={record.version_requirements} />
                <span className="review-meta">
                  <span className="review-tag">{record.domain}</span>
                  <span className="review-tag">{record.expected_tool}</span>
                  <span className={`review-tag status-${record.status}`}>
                    {STATUS_LABEL[record.status]}
                  </span>
                  <span className="review-tag">rev {record.approval_revision}</span>
                </span>
                <code className="review-ref">
                  {record.example_ref} ← {record.source_run_ref}
                </code>
                {(record.status === 'draft' || record.status === 'approved') && (
                  <span className="review-actions">
                    {record.status === 'draft' && (
                      <button type="button" className="review-btn primary"
                        onClick={() => dispatch({
                          type: 'decide', exampleRef: record.example_ref, action: 'approve',
                        })}>
                        批准
                      </button>
                    )}
                    <button type="button" className="review-btn danger"
                      onClick={() => dispatch({
                        type: 'decide', exampleRef: record.example_ref, action: 'revoke',
                      })}>
                      撤销
                    </button>
                    {record.status === 'approved' && (
                      <button type="button" className="review-btn"
                        onClick={() => dispatch({
                          type: 'decide', exampleRef: record.example_ref, action: 'supersede',
                        })}>
                        替换
                      </button>
                    )}
                  </span>
                )}
                {deciding && (
                  <div className="review-form">
                    <label className="review-field">
                      审核理由（必填）
                      <textarea value={state.reason}
                        onChange={(event) => dispatch({ type: 'set-reason', reason: event.target.value })} />
                    </label>
                    {state.action === 'supersede' && (
                      <label className="review-field">
                        替换为（同领域已批准）
                        <select value={state.replacementRef}
                          onChange={(event) => dispatch({
                            type: 'set-replacement', replacementRef: event.target.value,
                          })}>
                          <option value="">请选择…</option>
                          {choices.map((choice) => (
                            <option value={choice.example_ref} key={choice.example_ref}>
                              {choice.example_ref}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                    <span className="review-actions">
                      <button type="button" className="review-btn primary" onClick={() => {
                        const outcome = decisionFromState(state)
                        if (outcome.error || !outcome.decision) {
                          dispatch({ type: 'notice', notice: outcome.error ?? '请选择要审核的记录' })
                          return
                        }
                        onDecide(outcome.decision)
                      }}>
                        确认{ACTION_LABEL[state.action]}
                      </button>
                    </span>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

export function QueryMemoryReviewSection({ onClose }: { onClose?: () => void }) {
  const [candidates, setCandidates] = useState<QueryMemoryCandidate[]>([])
  const [drafts, setDrafts] = useState<QueryMemoryDraft[]>([])
  const [failure, setFailure] = useState<'hide' | 'denied' | string | null>(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    try {
      const [nextCandidates, nextDrafts] = await Promise.all([
        listQueryMemoryCandidates(), listQueryMemoryDrafts(),
      ])
      setCandidates(nextCandidates)
      setDrafts(nextDrafts)
      setFailure(null)
    } catch (caught) {
      setFailure(reviewFailure(caught))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  async function handleDecide(decision: DecisionRequest) {
    try {
      await decideQueryMemoryDraft(
        decision.exampleRef, decision.action, decision.reason, decision.replacementRef)
      await load()
    } catch (caught) {
      setFailure(reviewFailure(caught))
    }
  }

  async function handleCreateDraft(request: DraftRequest) {
    try {
      await createQueryMemoryDraft(request)
      await load()
    } catch (caught) {
      setFailure(reviewFailure(caught))
    }
  }

  // 404：功能已关闭，整个面板隐藏（App 的入口随后也会随探测结果消失）。
  if (failure === 'hide') return null
  // 403：权限被拒（或在会话中被撤回），明确说明，而不是静默消失。
  if (failure === 'denied') return <p className="review-denied" role="alert">无审核权限</p>

  return (
    <section className="review-panel" aria-label="记忆审核">
      <header className="review-head">
        <h2 className="review-title">记忆审核</h2>
        {onClose && (
          <button type="button" className="review-btn" onClick={onClose}>关闭</button>
        )}
      </header>
      <div className="review-body">
        {failure && <p className="review-notice" role="alert">{failure}</p>}
        {loading ? <p className="review-empty">正在加载…</p> : (
          <QueryMemoryReview candidates={candidates} drafts={drafts}
            onDecide={(decision) => void handleDecide(decision)}
            onCreateDraft={(request) => void handleCreateDraft(request)} />
        )}
      </div>
    </section>
  )
}

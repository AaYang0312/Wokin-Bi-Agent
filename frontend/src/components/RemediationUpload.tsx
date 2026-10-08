import { useState } from 'react'

import { ApiError, uploadSycmFile } from '../api'
import type { ChatMessage, DisplayEntity } from '../types'
import { AlertIcon } from './icons'

/**
 * 缺数上传闭环（计划 M5，设计决策 D3/D4/D5）。
 *
 * 后端在覆盖不足时把服务端派生的 `remediation` 块挂进 metric_result 公开 Artifact
 * （`business_query/tool.py` 的公开投影；`runtime.models._remediation` 整形，模型可见
 * 载荷一律没有这一块）。展示侧沿用白名单纪律把载荷再验一遍：
 *
 * 1. **按钮只认结构化块**：载荷带形状合法的 `remediation` 才渲染"上传缺失文件"；
 *    消息文本里出现"请上传"之类字样永远不产生按钮。验形不合法 → 整卡拒绝。
 * 2. **上传是原始字节体**：POST /api/uploads/sycm?filename=<文件名>，
 *    Content-Type: application/octet-stream + X-BI-Agent: web（Origin 由浏览器附带）。
 *    响应形状在这里逐键验，验不过按契约违规兜底，不假装渲染出结果。
 * 3. **上传成功不自动重跑（D4）**：成功后给"重新查询"按钮，点击才把该条消息对应的
 *    原用户问题原文重发一遍（复用现有发消息通道，等价于图表下钻的那条路）。
 */

// 与后端 `runtime/models.py` 的 `_remediation` 同一形状（镜像，独立编译）：
// 展示层不 import 后端代码，两侧各验一遍，改一侧必须同步另一侧。
const REMEDIATION_KEYS = new Set([
  'kind', 'file_kind', 'shop_refs', 'covered_window', 'missing_range'])
const REMEDIATION_REQUIRED = new Set(['kind', 'file_kind', 'shop_refs', 'missing_range'])
const REMEDIATION_KINDS = new Set(['upload'])
// file_kind 是导入面的文件类别词表（后端 `_REMEDIATION_FILE_KINDS`），不是自由文本。
const REMEDIATION_FILE_KINDS = new Set(['店铺维度'])
// 与 catalog 同一形式（`catalog.models.REF_RE`）：不透明引用，不是 ERP 主键。
const SHOP_REF_RE = /^ent-[0-9a-z]{8}$/
// 与后端 `_DATE_RE` 同形：[start, end) 的两个 ISO 日期文本。
const ISO_DATE_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 键集恰好相等：多一个未知键、少一个必需键都算形状不对。 */
function hasExactKeys(value: object, keys: ReadonlySet<string>): boolean {
  const actual = Object.keys(value)
  if (actual.length !== keys.size) return false
  return actual.every((key) => keys.has(key))
}

/** 键集与后端 `_mapping(allowed, required)` 同一语义：未知键不许出现，required 四键
 * 一个不能少，covered_window 允许缺省（allowed 而非 required）。 */
function hasRemediationKeys(value: Record<string, unknown>): boolean {
  const actual = Object.keys(value)
  if (actual.length > REMEDIATION_KEYS.size) return false
  for (const key of actual) {
    if (!REMEDIATION_KEYS.has(key)) return false
  }
  for (const key of REMEDIATION_REQUIRED) {
    if (!(key in value)) return false
  }
  return true
}

function isDatePair(value: unknown): value is [string, string] {
  return Array.isArray(value) && value.length === 2
    && value.every((item) => typeof item === 'string' && ISO_DATE_RE.test(item))
}

export type RemediationView = {
  kind: 'upload'
  file_kind: string
  shop_refs: string[]
  covered_window: readonly [string, string] | null
  missing_range: readonly [string, string]
}

export type ParsedRemediation =
  | { ok: true; remediation: RemediationView }
  | { ok: false; reason: 'remediation_payload_invalid' }

const INVALID_REMEDIATION: ParsedRemediation = {
  ok: false, reason: 'remediation_payload_invalid' }

/**
 * 展示侧整体验形（`runtime.models._remediation` 的镜像）：allowed 五键，required
 * 四键，covered_window 允许缺省或 null（后端公开投影总是带键、值可为 null）。
 * 任何一步不过都指向同一个稳定原因，错误信息里没有载荷内容。
 */
export function parseRemediation(value: unknown): ParsedRemediation {
  if (!isRecord(value) || !hasRemediationKeys(value)) return INVALID_REMEDIATION
  if (typeof value.kind !== 'string' || !REMEDIATION_KINDS.has(value.kind)
    || typeof value.file_kind !== 'string' || !REMEDIATION_FILE_KINDS.has(value.file_kind)) {
    return INVALID_REMEDIATION
  }
  if (!Array.isArray(value.shop_refs)
    || !value.shop_refs.every((ref) => typeof ref === 'string' && SHOP_REF_RE.test(ref))) {
    return INVALID_REMEDIATION
  }
  let covered: readonly [string, string] | null = null
  if (value.covered_window !== undefined && value.covered_window !== null) {
    if (!isDatePair(value.covered_window)) return INVALID_REMEDIATION
    covered = value.covered_window
  }
  if (!isDatePair(value.missing_range)) return INVALID_REMEDIATION
  return { ok: true, remediation: {
    kind: 'upload',
    file_kind: value.file_kind,
    shop_refs: value.shop_refs as string[],
    covered_window: covered,
    missing_range: value.missing_range,
  } }
}

/**
 * 上传拒绝原因码对照文案（M5）：前端常量集中定义，覆盖后端 M2 校验管线的七个
 * 稳定原因码（`sycm_import.ReasonCode`）。不在表里的码原样上屏——猜一个近义词
 * 就是把"为什么被拒"说成另一个理由。
 */
export const UPLOAD_REASON_LABELS: Record<string, string> = {
  missing_columns: '文件缺少必需的数据列',
  bad_date: '统计日期无法解析或超出允许范围',
  shop_not_registered: '文件包含未登记导入的店铺，整文件拒收',
  row_overflow: '文件行数超出上限',
  pii_columns_rejected: '文件包含个体级隐私列，整文件拒收',
  parse_failed: '文件无法解析（损坏、超限或不是有效的 xlsx）',
  not_imported_this_round: '该文件类别本轮不导入',
}

/** 上传响应（后端 `SycmUploadResult`，extra="forbid"）的展示侧视图。 */
export type UploadOutcome =
  | { ok: true; kind: 'accepted'; rowsRead: number; rowsWritten: number; shops: string[] }
  | { ok: true; kind: 'rejected'; reasonCode: string }
  | { ok: false }

const UPLOAD_RESULT_KEYS = new Set([
  'upload_id', 'status', 'reason_code', 'rows_read', 'rows_written', 'shops'])
const UPLOAD_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isShopList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((shop) =>
    typeof shop === 'string' && shop !== '' && shop.length <= 100)
}

/**
 * 响应逐键验形：accepted ⇒ reason_code 必须是 null；rejected ⇒ 必须带原因码
 * （后端 ledger 的不变式，sycm_import.py 的两处 ImportOutcome 构造）。形状不过
 * 按"契约违规"兜底，绝不把一半可信的结果当成功渲染。
 */
export function parseUploadResult(value: unknown): UploadOutcome {
  if (!isRecord(value) || !hasExactKeys(value, UPLOAD_RESULT_KEYS)) return { ok: false }
  if (typeof value.upload_id !== 'string' || !UPLOAD_ID_RE.test(value.upload_id)) return { ok: false }
  if (!isCount(value.rows_read) || !isCount(value.rows_written)) return { ok: false }
  if (!isShopList(value.shops)) return { ok: false }
  if (value.status === 'accepted') {
    if (value.reason_code !== null) return { ok: false }
    return {
      ok: true, kind: 'accepted', rowsRead: value.rows_read,
      rowsWritten: value.rows_written, shops: value.shops,
    }
  }
  if (value.status === 'rejected') {
    if (typeof value.reason_code !== 'string' || value.reason_code === ''
      || value.reason_code.length > 60) {
      return { ok: false }
    }
    return { ok: true, kind: 'rejected', reasonCode: value.reason_code }
  }
  return { ok: false }
}

/** 结果行的文案：成功报行数与店铺，拒绝走对照表，验形不过明说未展示。 */
export function uploadResultText(outcome: UploadOutcome): string {
  if (!outcome.ok) return '上传响应不符合契约，未展示结果，可重试。'
  if (outcome.kind === 'accepted') {
    const shops = outcome.shops.length > 0 ? `（${outcome.shops.join('、')}）` : ''
    return `已导入：读取 ${outcome.rowsRead} 行、写入 ${outcome.rowsWritten} 行${shops}。`
  }
  const label = UPLOAD_REASON_LABELS[outcome.reasonCode]
  return label ? `上传被拒绝：${label}。` : `上传被拒绝（原因码：${outcome.reasonCode}）。`
}

/** 非 2xx 的兜底文案：404 是 D5 的"开关关闭"稳定语义，单独说；其余按状态码兜底。 */
export function uploadErrorText(status: number | null): string {
  if (status === 404) return '上传功能未启用。'
  if (status === null) return '上传失败：无法连接服务，可重试。'
  return `上传失败（HTTP ${status}），可重试。`
}

/**
 * 该条消息对应的原用户问题原文（D4"重新查询"的重发内容）：从助手消息往前找最近的
 * 用户消息。找不到就是 null——没有原文就不给重发按钮，不拿助手文本冒充问题。
 */
export function originalQuestionOf(
  messages: readonly ChatMessage[], index: number,
): string | null {
  for (let at = index - 1; at >= 0; at -= 1) {
    const prior = messages[at]
    if (prior.role === 'user') return prior.content
  }
  return null
}

type UploadPhase =
  | { phase: 'idle' }
  | { phase: 'uploading'; name: string }
  | { phase: 'done'; outcome: UploadOutcome }
  | { phase: 'failed'; status: number | null }

/**
 * remediation 卡的上传区块：按钮 → 进度 → 结果行；失败与拒绝都可重试（按钮常在）。
 * "重新查询"只在上传成功且手里有原问题文本时出现，点击经 onRequery 重发原文。
 */
export function RemediationUpload({
  remediation, entities, questionText, onRequery,
}: {
  remediation: RemediationView
  /** 同一条消息里的授权展示名：店铺引用只在这里换名字，换不出就写"名称未取得"。 */
  entities?: Map<string, DisplayEntity>
  /** 该条消息对应的原用户问题原文；为 null（如流式中的卡片）就不给重发按钮。 */
  questionText?: string | null
  onRequery?: (question: string) => void
}) {
  const [state, setState] = useState<UploadPhase>({ phase: 'idle' })

  async function upload(file: File) {
    setState({ phase: 'uploading', name: file.name })
    try {
      const outcome = parseUploadResult(await uploadSycmFile(file))
      setState({ phase: 'done', outcome })
    } catch (caught) {
      setState({
        phase: 'failed',
        status: caught instanceof ApiError ? caught.status : null,
      })
    }
  }

  const canRequery = state.phase === 'done'
    && state.outcome.ok && state.outcome.kind === 'accepted'
    && typeof questionText === 'string' && questionText !== '' && onRequery !== undefined

  return (
    <section className="remediation" aria-label="缺数上传">
      <p className="remediation-line">
        <AlertIcon size={13} />
        缺失区间：{remediation.missing_range[0]} 至 {remediation.missing_range[1]}
        （{remediation.file_kind}数据）
      </p>
      {remediation.covered_window && (
        <p className="remediation-line">
          已覆盖：{remediation.covered_window[0]} 至 {remediation.covered_window[1]}
        </p>
      )}
      {entities && remediation.shop_refs.length > 0 && (
        <p className="remediation-line">
          涉及店铺：{remediation.shop_refs.map((ref) => {
            const entity = entities.get(ref)
            if (!entity || !entity.display_name) return '名称未取得'
            return entity.display_name
          }).join('、')}
        </p>
      )}
      <div className="remediation-actions">
        {state.phase === 'uploading' ? (
          <span className="remediation-status" role="status">正在上传 {state.name}…</span>
        ) : (
          <label className="analysis-source-btn remediation-btn">
            上传缺失文件
            <input
              type="file"
              accept=".xlsx"
              className="remediation-file"
              onChange={(event) => {
                const file = event.currentTarget.files?.[0]
                event.currentTarget.value = ''
                if (file) void upload(file)
              }} />
          </label>
        )}
        {canRequery && (
          <button
            type="button"
            className="analysis-source-btn remediation-requery"
            onClick={() => onRequery?.(questionText ?? '')}>
            重新查询
          </button>
        )}
      </div>
      {state.phase === 'done' && (
        <p className={`remediation-result${state.outcome.ok && state.outcome.kind === 'accepted'
          ? '' : ' remediation-result-bad'}`}
          role="status">{uploadResultText(state.outcome)}</p>
      )}
      {state.phase === 'failed' && (
        <p className="remediation-result remediation-result-bad" role="alert">
          {uploadErrorText(state.status)}
        </p>
      )}
    </section>
  )
}

import { useState } from 'react'

import type {
  AnalysisClaimKind, AnalysisFindingView, AnalysisKind, AnalysisNarrativeView,
  AnalysisPayloadView, Artifact,
} from '../types'
import { AlertIcon, ClockIcon } from './icons'

/**
 * 隔离分析结果的白名单渲染（计划 Task 6 Step 3）。
 *
 * 后端在模型调用**之前**就完成了来源归属、授权、类型、版本、大小与 fingerprint
 * 校验，落库载荷再经 `runtime.models._analysis_payload` 整形；展示侧是最后一道：
 * 这里按同一纪律把载荷再验一遍，验不过整卡拒绝——不猜测形状、不补数字、不把
 * 认不出的字段当背景噪声。
 *
 * 三条硬规则：
 *
 * 1. **只渲染，不重算**：数值全部是后端量化好的十进制字符串，原样上屏；唯一例外是
 *    占比/变化率这两个“份额”键换算成百分号写法——那是单位写法（×100），不是重新
 *    计算业务指标（后者在浏览器里做就会造出第二份“权威数字”）。
 * 2. **证据不足的说法永远进折叠区**：`unsupported_claims` 单独收进 `<details>`，
 *    findings 区结构性拿不到它；假设（hypotheses 与 claim_kind=hypothesis 的叙述）
 *    一律带“待验证”标记，永不冒充事实。
 * 3. **来源链接只认本轮消息**：`source_artifact_ref` 必须能在**同一条消息**的
 *    Artifact 列表里按 `artifact_id` 找到唯一一份、且类型是 loader 允许的三种来源
 *    之一；不 fetch、不看 URL、不读任何缓存。找不到就明说找不到，按钮禁用。
 */

// 以下正则与后端 `runtime/models.py` 的 `_ANALYSIS_*` 词表同一形状（镜像，独立编译）：
// 展示层不 import 后端代码，两侧各验一遍，改一侧必须同步另一侧。
const SOURCE_REF_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const FINGERPRINT_RE = /^[0-9a-f]{64}$/
const VERSION_RE = /^[a-z0-9]+(-[a-z0-9]+)*\/[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$/
const FINDING_REF_RE = /^finding-[a-z0-9-]{1,60}$/
const ROW_REF_RE = /^row-[a-z0-9-]{1,60}$/
const KEY_RE = /^[a-z][a-z0-9_]{0,47}$/

const ANALYSIS_KINDS: readonly AnalysisKind[] = [
  'contribution', 'change_decomposition', 'anomaly_candidates', 'followups']
const CLAIM_KINDS: readonly AnalysisClaimKind[] = ['fact', 'observation', 'hypothesis']
// 与后端 `_ANALYSIS_FORBIDDEN_KEYS` 同一词表：values 的键也是键，查询通道词汇不收。
const FORBIDDEN_KEYS = new Set(['sql', 'prompt', 'tool_calls', 'raw_rows'])
// loader 只把三种数据集 Artifact 当分析来源（`analysis.loader.SOURCE_ARTIFACT_TYPES` 镜像）。
const SOURCE_TYPES = new Set(['metric_result', 'comparison_table', 'trend_series'])

const PAYLOAD_KEYS = new Set([
  'source_artifact_ref', 'source_fingerprint', 'analysis_version', 'findings',
  'narrative', 'hypotheses', 'unsupported_claims', 'limitations'])
/** 展示层完整对象 = 自身引用与类型（`artifact_event_payload` 补）+ 八个载荷键。 */
const DISPLAY_KEYS = new Set(['artifact_id', 'artifact_type', ...PAYLOAD_KEYS])

const LIMITS = { findings: 500, list: 20, narrativeRefs: 5, rowRefs: 500, values: 10, valueText: 200, text: 500 }

const KIND_LABELS: Record<AnalysisKind, string> = {
  contribution: '贡献拆解',
  change_decomposition: '变化拆分',
  anomaly_candidates: '异常候选（MAD）',
  followups: '待跟进',
}
/** 组间固定按这个顺序（后端 `_KIND_ORDER` 同序），组内保持载荷顺序。 */
const KIND_ORDER: readonly AnalysisKind[] = ANALYSIS_KINDS

const CLAIM_LABELS: Record<AnalysisClaimKind, string> = {
  fact: '事实',
  observation: '观察',
  hypothesis: '待验证',
}

/** 每个 kind 的列顺序：已知键按业务读法排，其余（合法但未登记的）键按名补在后面。 */
const KIND_COLUMNS: Record<AnalysisKind, readonly string[]> = {
  contribution: ['value', 'contribution'],
  change_decomposition: ['current', 'previous', 'change', 'change_rate'],
  anomaly_candidates: ['value', 'center', 'mad', 'score', 'method'],
  followups: ['followup'],
}

const VALUE_LABELS: Record<string, string> = {
  value: '数值', contribution: '占比', current: '本期', previous: '上期',
  change: '变化', change_rate: '变化率', method: '方法', score: '得分',
  center: '中位', mad: 'MAD', followup: '跟进项',
}

/** 只有“份额”性质的键换算百分号；其余数值原样，绝不在浏览器里重算指标。 */
const PERCENT_KEYS = new Set(['contribution', 'change_rate'])
const DECIMAL_RE = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/
const PERCENT_FRAC_DIGITS = 2

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isBoundedText(value: unknown, limit: number): value is string {
  return typeof value === 'string' && value !== ''
    && value === value.trim() && value.length <= limit
}

/** 键集恰好相等：多一个未知键、少一个必需键都算形状不对。 */
function hasExactKeys(value: object, keys: ReadonlySet<string>): boolean {
  const actual = Object.keys(value)
  if (actual.length !== keys.size) return false
  return actual.every((key) => keys.has(key))
}

/**
 * 份额的百分号写法：把后端量化好的十进制串精确 ×100（小数点右移两位），
 * 再按 ROUND_HALF_EVEN（与后端 `_share` 同一规则）量化到恰好 2 位小数。
 *
 * 为什么不用 Number × 100 / toFixed：那是二进制浮点换算，大比值会漂移
 * （12345678901234567.89 会变成 …800.00），且 toFixed 是“半远离零”而非后端的
 * 半偶舍入。字符串移位对任意长度的合法十进制串都精确；只做单位换算，
 * 不重算任何业务指标，原始串保留在单元格的 data-value 上供审计。
 */
function shareText(raw: string): string {
  if (!DECIMAL_RE.test(raw)) return raw
  const negative = raw.startsWith('-')
  const unsigned = negative ? raw.slice(1) : raw
  const point = unsigned.indexOf('.')
  const digits = point === -1 ? unsigned
    : unsigned.slice(0, point) + unsigned.slice(point + 1)
  // ×100 = 小数点右移 2 位；再保留 2 位小数 → 共保留 point+4 位数字。
  const keep = point + 2 + PERCENT_FRAC_DIGITS
  const kept = roundHalfEven(digits, keep)
  const whole = kept.slice(0, -PERCENT_FRAC_DIGITS).replace(/^0+(?=[0-9])/, '')
  const frac = kept.slice(-PERCENT_FRAC_DIGITS)
  const isZero = /^0*$/.test(whole) && /^0*$/.test(frac)
  return `${negative && !isZero ? '-' : ''}${whole || '0'}.${frac}%`
}

/** 定长十进制串的半偶舍入：保留前 keep 位，按第 keep+1 位及其后余量决定进位。 */
function roundHalfEven(digits: string, keep: number): string {
  if (digits.length <= keep) return digits + '0'.repeat(keep - digits.length)
  const rest = digits.slice(keep)
  const first = rest.charCodeAt(0) - 48
  const kept = digits.slice(0, keep)
  const roundUp = first > 5 || (first === 5
    && (/[1-9]/.test(rest.slice(1))
      || (kept.length > 0 && (kept.charCodeAt(kept.length - 1) - 48) % 2 === 1)))
  if (!roundUp) return kept
  const arr = kept.split('')
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] === '9') {
      arr[i] = '0'
      continue
    }
    arr[i] = String(Number(arr[i]) + 1)
    return arr.join('')
  }
  return `1${arr.join('')}`
}

export type ParsedAnalysis =
  | { ok: true; payload: AnalysisPayloadView }
  | { ok: false; reason: 'analysis_payload_invalid' }

/**
 * 展示侧整体验形。全部通过才返回载荷视图；任何一步不过都指向同一个稳定原因，
 * 不携带任何载荷内容（错误文本里没有数字，也没有业务词）。
 */
export function parseAnalysisArtifact(artifact: Artifact): ParsedAnalysis {
  if (!isRecord(artifact) || artifact.artifact_type !== 'analysis_result'
    || !hasExactKeys(artifact, DISPLAY_KEYS)) {
    return { ok: false, reason: 'analysis_payload_invalid' }
  }
  const { source_artifact_ref, source_fingerprint, analysis_version } = artifact
  if (typeof source_artifact_ref !== 'string' || !SOURCE_REF_RE.test(source_artifact_ref)
    || typeof source_fingerprint !== 'string' || !FINGERPRINT_RE.test(source_fingerprint)
    || typeof analysis_version !== 'string' || !VERSION_RE.test(analysis_version)) {
    return { ok: false, reason: 'analysis_payload_invalid' }
  }
  if (!Array.isArray(artifact.findings) || artifact.findings.length > LIMITS.findings) {
    return { ok: false, reason: 'analysis_payload_invalid' }
  }
  const findingRefs = new Set<string>()
  const findings: AnalysisFindingView[] = []
  for (const item of artifact.findings) {
    if (!isRecord(item) || !hasExactKeys(item, new Set([
      'finding_ref', 'kind', 'metric', 'row_refs', 'values', 'statement_code']))) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    const { finding_ref, kind, metric, row_refs, values, statement_code } = item
    if (typeof finding_ref !== 'string' || !FINDING_REF_RE.test(finding_ref)
      || !ANALYSIS_KINDS.includes(kind as AnalysisKind)
      || typeof metric !== 'string' || !KEY_RE.test(metric)
      || typeof statement_code !== 'string' || !KEY_RE.test(statement_code)) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    if (!Array.isArray(row_refs) || row_refs.length < 1 || row_refs.length > LIMITS.rowRefs
      || !row_refs.every((ref) => typeof ref === 'string' && ROW_REF_RE.test(ref))) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    if (!isRecord(values) || Object.keys(values).length > LIMITS.values) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    const cleanValues: Record<string, string> = {}
    for (const [key, value] of Object.entries(values)) {
      if (!KEY_RE.test(key) || FORBIDDEN_KEYS.has(key) || !isBoundedText(value, LIMITS.valueText)) {
        return { ok: false, reason: 'analysis_payload_invalid' }
      }
      cleanValues[key] = value
    }
    findingRefs.add(finding_ref)
    findings.push({
      finding_ref, kind: kind as AnalysisKind, metric,
      row_refs: row_refs as string[], values: cleanValues, statement_code,
    })
  }
  if (!Array.isArray(artifact.narrative) || artifact.narrative.length > LIMITS.list) {
    return { ok: false, reason: 'analysis_payload_invalid' }
  }
  const narrative: AnalysisNarrativeView[] = []
  for (const item of artifact.narrative) {
    if (!isRecord(item) || !hasExactKeys(item, new Set(['text', 'finding_refs', 'claim_kind']))) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    const { text, finding_refs, claim_kind } = item
    if (!isBoundedText(text, LIMITS.text)
      || !Array.isArray(finding_refs) || finding_refs.length < 1
      || finding_refs.length > LIMITS.narrativeRefs
      || !finding_refs.every((ref) => typeof ref === 'string' && FINDING_REF_RE.test(ref))
      || !CLAIM_KINDS.includes(claim_kind as AnalysisClaimKind)) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    // 叙述引用的 finding 必须真的在这份载荷里：悬空引用是形状不对，不是“少个链接”。
    if (!finding_refs.every((ref) => findingRefs.has(ref as string))) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    narrative.push({
      text, finding_refs: finding_refs as string[],
      claim_kind: claim_kind as AnalysisClaimKind,
    })
  }
  const lists: Pick<AnalysisPayloadView, 'hypotheses' | 'unsupported_claims' | 'limitations'> = {
    hypotheses: [], unsupported_claims: [], limitations: [] }
  for (const key of ['hypotheses', 'unsupported_claims', 'limitations'] as const) {
    const entries = artifact[key]
    if (!Array.isArray(entries) || entries.length > LIMITS.list
      || !entries.every((entry) => isBoundedText(entry, LIMITS.text))) {
      return { ok: false, reason: 'analysis_payload_invalid' }
    }
    lists[key] = entries as string[]
  }
  return { ok: true, payload: {
    source_artifact_ref, source_fingerprint, analysis_version,
    findings, narrative, ...lists } }
}

export type ResolvedAnalysisSource =
  | { ok: true; source: Artifact }
  | { ok: false; reason: 'missing' | 'ambiguous' | 'type' }

/**
 * 按引用在本条消息的 Artifact 列表里找来源。只认列表、只认唯一命中、只认三种
 * 数据集类型；URL、缓存与“看着像”一律不看。
 */
export function resolveAnalysisSource(
  datasets: Artifact[], analysis: Artifact,
): ResolvedAnalysisSource {
  const ref = typeof analysis.source_artifact_ref === 'string'
    ? analysis.source_artifact_ref : null
  if (!ref) return { ok: false, reason: 'missing' }
  const matches = datasets.filter((item) => item.artifact_id === ref)
  if (matches.length === 0) return { ok: false, reason: 'missing' }
  if (matches.length > 1) return { ok: false, reason: 'ambiguous' }
  const source = matches[0]
  if (typeof source.artifact_type !== 'string' || !SOURCE_TYPES.has(source.artifact_type)) {
    return { ok: false, reason: 'type' }
  }
  return { ok: true, source }
}

/** 一条 finding 的展示列：kind 的已知列序在前，未登记的合法键按名补后。 */
function columnsOf(finding: AnalysisFindingView): string[] {
  const known = KIND_COLUMNS[finding.kind].filter((key) => key in finding.values)
  const extra = Object.keys(finding.values)
    .filter((key) => !KIND_COLUMNS[finding.kind].includes(key)).sort()
  return [...known, ...extra]
}

function FindingRows({ findings }: { findings: AnalysisFindingView[] }) {
  const columnList = [...new Set(findings.flatMap(columnsOf))]
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">行</th>
          <th scope="col">指标</th>
          {columnList.map((key) => <th scope="col" key={key}>{VALUE_LABELS[key] ?? key}</th>)}
        </tr>
      </thead>
      <tbody>
        {findings.map((finding) => (
          <tr key={finding.finding_ref} data-finding-ref={finding.finding_ref}
            data-statement={finding.statement_code}>
            <td>{finding.row_refs.join('、')}</td>
            <td>{finding.metric}</td>
            {columnList.map((key) => {
              const raw = finding.values[key]
              if (raw === undefined) return <td key={key}>—</td>
              const shown = PERCENT_KEYS.has(key) ? shareText(raw) : raw
              return <td key={key} data-value={raw}>{shown}</td>
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function AnalysisArtifact({
  artifact, datasets = [],
}: {
  artifact: Artifact
  /** 同一条消息里已收到的全部 Artifact：来源按钮只在这里按引用找来源。 */
  datasets?: Artifact[]
}) {
  const parsed = parseAnalysisArtifact(artifact)
  const [showSource, setShowSource] = useState(false)
  if (!parsed.ok) {
    return (
      <section className="artifact" aria-label="分析结果未渲染">
        <header className="artifact-head">
          <span className="artifact-title"><AlertIcon size={15} />分析结果</span>
        </header>
        <p className="limitations">
          分析结果载荷未通过展示侧校验，未渲染：不猜测形状，也不补任何数字。
        </p>
      </section>
    )
  }
  const payload = parsed.payload
  const source = resolveAnalysisSource(datasets, artifact)
  const groups = KIND_ORDER
    .map((kind) => ({ kind, items: payload.findings.filter((item) => item.kind === kind) }))
    .filter((group) => group.items.length > 0)

  return (
    <section className="artifact" aria-label="隔离分析结果">
      <header className="artifact-head">
        <span className="artifact-title"><AlertIcon size={15} />隔离分析结果</span>
        <span className="pill">{payload.findings.length} 条确定性结论</span>
        <span className="pill">{payload.analysis_version}</span>
      </header>

      {groups.length > 0 && (
        <div className="analysis-findings" data-testid="findings">
          {groups.map((group) => (
            <section className="analysis-kind" key={group.kind}>
              <h4 className="analysis-kind-title">{KIND_LABELS[group.kind]}</h4>
              <FindingRows findings={group.items} />
            </section>
          ))}
        </div>
      )}

      {payload.narrative.length > 0 && (
        <section className="analysis-kind">
          <h4 className="analysis-kind-title">模型叙述（已过守卫）</h4>
          <ul className="analysis-narrative">
            {payload.narrative.map((item, index) => (
              <li key={index} data-claim-kind={item.claim_kind}>
                <span className={`claim-badge claim-${item.claim_kind}`}>
                  {CLAIM_LABELS[item.claim_kind]}
                </span>
                {item.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {payload.hypotheses.length > 0 && (
        <section className="analysis-kind">
          <h4 className="analysis-kind-title">假设（待验证）</h4>
          <ul className="analysis-claims">
            {payload.hypotheses.map((item, index) => <li key={index}>{item}</li>)}
          </ul>
        </section>
      )}

      {/* 证据不足的说法只住在这里：findings 区结构性拿不到它。 */}
      {payload.unsupported_claims.length > 0 && (
        <details className="analysis-unsupported">
          <summary>证据不足的说法（{payload.unsupported_claims.length}）</summary>
          <ul className="analysis-claims">
            {payload.unsupported_claims.map((item, index) => <li key={index}>{item}</li>)}
          </ul>
        </details>
      )}

      <div className="artifact-meta">
        {source.ok ? (
          <>
            <span>来源：{String(source.source.artifact_type)}</span>
            {typeof source.source.data_as_of === 'string' && (
              <span><ClockIcon size={13} />快照 {source.source.data_as_of}</span>
            )}
            <button
              type="button"
              className="analysis-source-btn"
              aria-expanded={showSource}
              onClick={() => setShowSource((value) => !value)}>
              查看来源数据
            </button>
          </>
        ) : (
          <>
            <button type="button" className="analysis-source-btn" disabled aria-expanded={false}>
              查看来源数据
            </button>
            <span>来源数据未随本轮返回，无法核对来源。</span>
          </>
        )}
      </div>
      {source.ok && showSource && (
        <p className="analysis-source-detail">来源 artifact_id：{String(source.source.artifact_id)}</p>
      )}

      {payload.limitations.length > 0 && (
        <p className="limitations">{payload.limitations.join('；')}</p>
      )}
    </section>
  )
}

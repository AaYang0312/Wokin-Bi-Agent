/**
 * 覆盖缺口部分答案的展示侧验形（计划 2026-10-08 T1）。
 *
 * 与后端 `runtime.models._partial_answer` 同一形状，两侧独立编译：
 * 四键齐全，窗口是非空的 [start, end)，披露句必须由窗口现算。
 * 模型改写后的句子、未知键和空窗口都整卡拒绝。
 */

const PARTIAL_ANSWER_KEYS = new Set([
  'answered_windows', 'missing_windows', 'metric_refs', 'disclosure'])
const ISO_DATE_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/
const METRIC_REF_RE = /^[a-z][a-z0-9_]*$/

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasExactKeys(value: object, keys: ReadonlySet<string>): boolean {
  const actual = Object.keys(value)
  if (actual.length !== keys.size) return false
  return actual.every((key) => keys.has(key))
}

function isWindow(value: unknown): value is [string, string] {
  return Array.isArray(value) && value.length === 2
    && value.every((item) => typeof item === 'string' && ISO_DATE_RE.test(item))
    && value[0] < value[1]
}

function isWindowList(value: unknown): value is [string, string][] {
  return Array.isArray(value) && value.length > 0 && value.every(isWindow)
}

/** 与 `metrics.partial_answer_disclosure` 同一句式。 */
export function partialAnswerDisclosure(
  answered: readonly (readonly [string, string])[],
  missing: readonly (readonly [string, string])[],
): string {
  const covered = answered.map(([start, end]) => `${start}~${end}`).join('、')
  const gap = missing.map(([start]) => start).join('、')
  return `仅覆盖 ${covered}，${gap} 起缺口`
}

export type PartialAnswerView = {
  answeredWindows: readonly (readonly [string, string])[]
  missingWindows: readonly (readonly [string, string])[]
  metricRefs: readonly string[]
  disclosure: string
}

export type ParsedPartialAnswer =
  | { ok: true; partialAnswer: PartialAnswerView }
  | { ok: false; reason: 'partial_answer_payload_invalid' }

const INVALID: ParsedPartialAnswer = {
  ok: false, reason: 'partial_answer_payload_invalid' }

export function parsePartialAnswer(value: unknown): ParsedPartialAnswer {
  if (!isRecord(value) || !hasExactKeys(value, PARTIAL_ANSWER_KEYS)) return INVALID
  if (!isWindowList(value.answered_windows) || !isWindowList(value.missing_windows)) {
    return INVALID
  }
  if (!Array.isArray(value.metric_refs) || value.metric_refs.length === 0
    || new Set(value.metric_refs).size !== value.metric_refs.length
    || !value.metric_refs.every((metric) => typeof metric === 'string' && METRIC_REF_RE.test(metric))) {
    return INVALID
  }
  const disclosure = partialAnswerDisclosure(value.answered_windows, value.missing_windows)
  if (value.disclosure !== disclosure) return INVALID
  return {
    ok: true,
    partialAnswer: {
      answeredWindows: value.answered_windows,
      missingWindows: value.missing_windows,
      metricRefs: value.metric_refs,
      disclosure,
    },
  }
}

const REFUSED_KEYS = new Set(['metric', 'reason', 'remediation'])
const REFUSED_REASONS = new Set([
  'capability_unavailable',
  'source_not_onboarded',
  'coverage_incomplete',
  'basis_incompatible',
  'data_as_of_unknown',
])

export type RefusedMetricView = {
  metric: string
  reason: string
}

export type ParsedRefused =
  | { ok: true; refused: readonly RefusedMetricView[] }
  | { ok: false; reason: 'refused_payload_invalid' }

/** 拒答子集：指标名与原因码必填，上传块若在场必须过 remediation 验形。 */
export function parseRefused(
  value: unknown,
  remediationOk: (item: unknown) => boolean,
): ParsedRefused {
  if (!Array.isArray(value) || value.length === 0) {
    return { ok: false, reason: 'refused_payload_invalid' }
  }
  const seen = new Set<string>()
  const refused: RefusedMetricView[] = []
  for (const item of value) {
    if (!isRecord(item)) return { ok: false, reason: 'refused_payload_invalid' }
    const keys = Object.keys(item)
    if (keys.some((key) => !REFUSED_KEYS.has(key))
      || !keys.includes('metric') || !keys.includes('reason')) {
      return { ok: false, reason: 'refused_payload_invalid' }
    }
    if (typeof item.metric !== 'string' || !METRIC_REF_RE.test(item.metric)
      || seen.has(item.metric)) {
      return { ok: false, reason: 'refused_payload_invalid' }
    }
    if (typeof item.reason !== 'string' || !REFUSED_REASONS.has(item.reason)) {
      return { ok: false, reason: 'refused_payload_invalid' }
    }
    if ('remediation' in item && !remediationOk(item.remediation)) {
      return { ok: false, reason: 'refused_payload_invalid' }
    }
    seen.add(item.metric)
    refused.push({ metric: item.metric, reason: item.reason })
  }
  return { ok: true, refused }
}

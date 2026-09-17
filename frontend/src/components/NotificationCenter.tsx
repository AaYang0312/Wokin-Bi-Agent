import type { InventoryNotification } from '../types'

// 库存通知中心（计划 2026-09-14-continuous-inventory-notifications.md Task 5）。
//
// 这是应用内通知的唯一消费面：后端只投影十四个安全字段（GET /api/notifications），
// owner subject、真实 ID、证据与完整来源载荷在后端就被收口。本模块三件事：
//
// 1. **运行时解析（`parseNotificationList`）**：类型声明不是信任凭据。整个列表
//    必须逐项通过键集恰好相等、ref/枚举/十进制/时间形状与文本边界的检查；任何
//    一项不合格，整份载荷拒绝（fail-closed），调用方进入错误态——绝不渲染认不
//    出的字段，绝不猜商品名，也绝不为缺来源的层级造"无法判断"占位卡（缺层级
//    的通知根本不会从 API 出现）。
// 2. **展示纪律**：`quantity` 为 null 表示"没读到"，与数量 0 永远是两种文案；
//    只有 event_kind=resolved 的卡片才允许出现"已恢复"；非恢复卡片常驻
//    "数据未满足解除条件"，acknowledge（确认收到）与已读是两个动作，确认收到
//    永远不表示恢复；scope/reason/unit/数据时点常显。
// 3. **UI 轮询（`createNotificationPoller`）**：这是 UI 刷新，不是业务调度——
//    每 60 秒且仅当页面可见时拉取（定时器在页面隐藏时整个停掉，回来时补一次
//    刷新再续上）；`stop()` 把定时器与可见性监听一并清理。重试语义只有这一条
//    UI 刷新路径，没有退避循环，也没有任何业务侧循环。
//
// 文本一律走 React 转义渲染，没有 dangerouslySetInnerHTML：hostile 文本只能
// 以惰性文本上屏。

const NOTIFICATION_REF_RE = /^ntf-[0-9a-z-]{1,60}$/
const ALERT_REF_RE = /^alert-[a-z0-9-]{1,60}$/
const REASON_CODE_RE = /^[a-z][a-z0-9_-]{0,31}$/
// 与后端 monitoring.models / 023 的数量文本同一形状：十进制文本，null = 没读到。
const DECIMAL_RE = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/

const EVENT_KINDS = ['triggered', 'retriggered', 'resolved'] as const
const ALERT_STATUSES = ['open', 'acknowledged', 'resolved', 'suppressed'] as const
const LEVELS = ['physical_total', 'shop_sellable'] as const

const NOTIFICATION_KEYS = new Set([
  'notification_ref', 'alert_ref', 'event_kind', 'status', 'level', 'sku_ref',
  'scope_ref', 'quantity', 'threshold', 'unit', 'data_as_of', 'reason_code',
  'read_at', 'created_at'])

const LIMITS = { list: 100, ref: 200, unit: 16, text: 200 }

/** UI 轮询间隔：只在页面可见时运行；不是业务调度器。 */
export const NOTIFICATION_POLL_INTERVAL_MS = 60_000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 键集恰好相等：多一个未知键、少一个必需键都算形状不对。 */
function hasExactKeys(value: object, keys: ReadonlySet<string>): boolean {
  const actual = Object.keys(value)
  if (actual.length !== keys.size) return false
  return actual.every((key) => keys.has(key))
}

function isBoundedText(value: unknown, limit: number): value is string {
  return typeof value === 'string' && value !== ''
    && value === value.trim() && value.length <= limit
}

function isDateTimeText(value: unknown): value is string {
  return typeof value === 'string' && value !== ''
    && !Number.isNaN(Date.parse(value))
}

function isDecimalText(value: unknown): value is string {
  return typeof value === 'string' && DECIMAL_RE.test(value)
}

function isOptionalText(value: unknown, limit: number): value is string | null {
  return value === null || isBoundedText(value, limit)
}

function isOptionalDecimal(value: unknown): value is string | null {
  return value === null || isDecimalText(value)
}

function inClosedSet<T extends string>(value: unknown,
                                      set: readonly T[]): value is T {
  return typeof value === 'string' && (set as readonly string[]).includes(value)
}

export type ParsedNotificationList =
  | { ok: true; notifications: InventoryNotification[] }
  | { ok: false; reason: 'notification_payload_invalid' }

/**
 * GET /api/notifications 响应的整体解析：全部通过才返回通知列表；任何一项
 * 不过都指向同一个稳定原因，错误文本里没有任何载荷内容。
 */
export function parseNotificationList(payload: unknown): ParsedNotificationList {
  if (!Array.isArray(payload) || payload.length > LIMITS.list) {
    return { ok: false, reason: 'notification_payload_invalid' }
  }
  const notifications: InventoryNotification[] = []
  for (const item of payload) {
    if (!isRecord(item) || !hasExactKeys(item, NOTIFICATION_KEYS)) {
      return { ok: false, reason: 'notification_payload_invalid' }
    }
    const {
      notification_ref, alert_ref, event_kind, status, level, sku_ref,
      scope_ref, quantity, threshold, unit, data_as_of, reason_code,
      read_at, created_at,
    } = item
    if (!isBoundedText(notification_ref, LIMITS.text)
      || !NOTIFICATION_REF_RE.test(notification_ref)
      || !isBoundedText(alert_ref, LIMITS.text)
      || !ALERT_REF_RE.test(alert_ref)
      || !inClosedSet(event_kind, EVENT_KINDS)
      || !inClosedSet(status, ALERT_STATUSES)
      || !inClosedSet(level, LEVELS)
      || !isBoundedText(sku_ref, LIMITS.ref)
      || !isBoundedText(scope_ref, LIMITS.ref)
      || !isOptionalDecimal(quantity)
      || !isOptionalDecimal(threshold)
      || !isBoundedText(unit, LIMITS.unit)
      || !isDateTimeText(data_as_of)
      || !isBoundedText(reason_code, 64)
      || !REASON_CODE_RE.test(reason_code)
      || !isOptionalText(read_at, LIMITS.text)
      || (read_at !== null && !isDateTimeText(read_at))
      || !isDateTimeText(created_at)) {
      return { ok: false, reason: 'notification_payload_invalid' }
    }
    notifications.push({
      notification_ref, alert_ref,
      event_kind, status, level,
      sku_ref, scope_ref,
      quantity, threshold, unit,
      data_as_of, reason_code,
      read_at, created_at,
    })
  }
  return { ok: true, notifications }
}

/** 一张卡片的可用动作：已读看 read_at，确认收到只属于 open 告警。 */
export function notificationActions(notification: InventoryNotification): {
  canRead: boolean
  canAcknowledge: boolean
} {
  return {
    canRead: notification.read_at === null,
    canAcknowledge: notification.status === 'open',
  }
}

/**
 * UI 轮询器：依赖全部注入（可见性、定时器、监听订阅），本模块不直接触碰
 * `document`/`window`，因此清理行为可以在 node 测试里逐项断言。
 */
export type NotificationPollerDeps = {
  refresh: () => Promise<void>
  /** 生产环境传 `document.visibilityState === 'visible'`。 */
  isVisible: () => boolean
  /** 生产环境传 `window.setInterval` 包装，返回清除函数。 */
  schedule: (tick: () => void, intervalMs: number) => () => void
  /** 生产环境传 `visibilitychange` 订阅，返回移除函数。 */
  onVisibilityChange: (handler: () => void) => () => void
}

export function createNotificationPoller(deps: NotificationPollerDeps): {
  start: () => void
  stop: () => void
} {
  let cancelTimer: (() => void) | null = null
  let removeListener: (() => void) | null = null

  function startTicking() {
    if (cancelTimer) return
    cancelTimer = deps.schedule(() => {
      void deps.refresh()
    }, NOTIFICATION_POLL_INTERVAL_MS)
  }

  function stopTicking() {
    cancelTimer?.()
    cancelTimer = null
  }

  function handleVisibility() {
    // 页面隐藏时连 UI 轮询也停（定时器不空转）；重新可见时先补一次刷新再续上。
    if (deps.isVisible()) {
      void deps.refresh()
      startTicking()
    } else {
      stopTicking()
    }
  }

  return {
    start() {
      removeListener = deps.onVisibilityChange(handleVisibility)
      if (deps.isVisible()) {
        void deps.refresh()
        startTicking()
      }
    },
    stop() {
      stopTicking()
      removeListener?.()
      removeListener = null
    },
  }
}

const KIND_HEADLINE: Record<InventoryNotification['event_kind'], string> = {
  triggered: '首次触发',
  retriggered: '持续异常',
  resolved: '已恢复',
}
const LEVEL_LABEL: Record<InventoryNotification['level'], string> = {
  physical_total: '实物库存',
  shop_sellable: '店铺可售',
}
const STATUS_LABEL: Record<InventoryNotification['status'], string> = {
  open: '待确认',
  acknowledged: '已确认收到',
  resolved: '已恢复',
  suppressed: '已停用',
}
/** 非恢复卡片的常驻口径：保持原状态/确认收到都不是库存恢复。 */
const GUARD_TEXT = '数据未满足解除条件'

function shortTime(value: string): string | null {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

/** null = 本层级没读到数量：与数量 0 永远两种文案，也永不互相替位。 */
function quantityText(quantity: string | null): string {
  return quantity === null ? '数量未读到' : `数量 ${quantity}`
}

function thresholdText(threshold: string | null): string {
  return threshold === null ? '阈值未配置' : `阈值 ${threshold}`
}

function NotificationCard({ notification, onRead, onAcknowledge }: {
  notification: InventoryNotification
  onRead: (notificationRef: string) => void
  onAcknowledge: (alertRef: string) => void
}) {
  const actions = notificationActions(notification)
  const snapshot = shortTime(notification.data_as_of)
  return (
    <li className={`notif-card${notification.read_at === null ? ' unread' : ''}`}
      data-notification-ref={notification.notification_ref}>
      <div className="notif-card-head">
        <span className={`notif-kind kind-${notification.event_kind}`}>
          {KIND_HEADLINE[notification.event_kind]}
        </span>
        <span className={`notif-tag status-${notification.status}`}>
          {STATUS_LABEL[notification.status]}
        </span>
        {notification.read_at !== null && <span className="notif-tag">已读</span>}
      </div>
      <p className="notif-line">
        {LEVEL_LABEL[notification.level]} · {quantityText(notification.quantity)}
        {' '}· {thresholdText(notification.threshold)} · 单位 {notification.unit}
      </p>
      <p className="notif-meta">
        <span>SKU {notification.sku_ref}</span>
        <span>范围 {notification.scope_ref}</span>
        <span>原因 {notification.reason_code}</span>
        {snapshot && <span>数据时点 {snapshot}</span>}
      </p>
      {notification.event_kind !== 'resolved' && (
        <p className="notif-guard">{GUARD_TEXT}</p>
      )}
      <div className="notif-actions">
        {actions.canRead && (
          <button type="button" className="notif-btn"
            onClick={() => onRead(notification.notification_ref)}>
            标记已读
          </button>
        )}
        {actions.canAcknowledge && (
          <button type="button" className="notif-btn primary"
            data-alert-ref={notification.alert_ref}
            onClick={() => onAcknowledge(notification.alert_ref)}>
            确认收到
          </button>
        )}
      </div>
    </li>
  )
}

export function NotificationCenter({ notifications, open, error, onToggle,
  onRead, onAcknowledge }: {
  notifications: InventoryNotification[]
  open: boolean
  /** 载荷被解析器拒绝或请求失败时的固定提示；出现时不渲染任何卡片。 */
  error: string | null
  onToggle: () => void
  onRead: (notificationRef: string) => void
  onAcknowledge: (alertRef: string) => void
}) {
  const unread = notifications.filter((item) => item.read_at === null).length
  return (
    <>
      <button type="button" className={`notif-toggle${open ? ' open' : ''}`}
        onClick={onToggle} aria-expanded={open} aria-controls="notification-center"
        aria-label={`${unread} 条未读库存通知`}>
        库存通知
        {unread > 0 && (
          <span className="notif-badge" aria-hidden="true">{unread}</span>
        )}
      </button>
      {open && (
        <section id="notification-center" className="notif-panel" role="region"
          aria-label="库存通知中心">
          <header className="notif-head">
            <h2 className="notif-title">库存通知</h2>
            <p className="notif-subtitle">最近 100 条 · 页面可见时每 60 秒刷新</p>
          </header>
          <div className="notif-body">
            {error && <p className="notif-error" role="alert">{error}</p>}
            {notifications.length === 0 ? (
              <p className="notif-empty">暂无库存通知</p>
            ) : (
              <ul className="notif-list">
                {notifications.map((item) => (
                  <NotificationCard key={item.notification_ref} notification={item}
                    onRead={onRead} onAcknowledge={onAcknowledge} />
                ))}
              </ul>
            )}
          </div>
        </section>
      )}
    </>
  )
}

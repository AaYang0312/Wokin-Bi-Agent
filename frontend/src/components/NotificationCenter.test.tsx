import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  ApiError, acknowledgeInventoryAlert, listNotifications, markNotificationRead,
} from '../api'
import {
  NOTIFICATION_POLL_INTERVAL_MS, NotificationCenter, createNotificationPoller,
  notificationActions, parseNotificationList,
} from './NotificationCenter'
import type { InventoryNotification } from '../types'

// 库存通知中心（计划 2026-09-14-continuous-inventory-notifications.md Task 5）。
// 仓库的组件测试跑在 node + renderToStaticMarkup 上（无 DOM/无 testing-library）：
// 交互与轮询契约收口在纯函数 `notificationActions` / `parseNotificationList` /
// `createNotificationPoller` 里，组件只做接线——这里对两者都直接断言。

function notification(overrides: Partial<InventoryNotification> = {}):
  InventoryNotification {
  return {
    notification_ref: 'ntf-' + 'a'.repeat(32),
    alert_ref: 'alert-open0001',
    event_kind: 'triggered',
    status: 'open',
    level: 'physical_total',
    sku_ref: 'ent-1a2b3c4d',
    scope_ref: 'pl-0123456789ab|wh-0123456789ab',
    quantity: '3',
    threshold: '5',
    unit: 'piece',
    data_as_of: '2026-09-17T11:55:00+08:00',
    reason_code: 'low_replenish',
    read_at: null,
    created_at: '2026-09-17T12:00:00+08:00',
    ...overrides,
  }
}

const handlers = {
  onToggle: () => undefined,
  onRead: () => undefined,
  onAcknowledge: () => undefined,
}

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init) as Response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock

}

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status,
    json: async () => body } as Response
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('parseNotificationList：API 投影的运行时解析（类型不是信任凭据）', () => {
  it('accepts the exact fourteen-key projection', () => {
    const parsed = parseNotificationList([notification()])
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      expect(parsed.notifications).toHaveLength(1)
      expect(parsed.notifications[0].quantity).toBe('3')
    }
  })

  it('keeps null quantity distinct from zero', () => {
    const parsed = parseNotificationList([notification({ quantity: null })])
    expect(parsed.ok).toBe(true)
  })

  it('rejects a non-array payload and lists beyond 100 items', () => {
    expect(parseNotificationList({}).ok).toBe(false)
    expect(parseNotificationList(null).ok).toBe(false)
    expect(parseNotificationList(
      Array.from({ length: 101 }, () => notification())).ok).toBe(false)
  })

  it('rejects unknown and missing keys', () => {
    const extra = notification() as unknown as Record<string, unknown>
    extra.owner_subject_id = 'subject-a'
    expect(parseNotificationList([extra]).ok).toBe(false)
    const missing = notification()
    delete (missing as Partial<InventoryNotification>).scope_ref
    expect(parseNotificationList([missing]).ok).toBe(false)
  })

  it('rejects unknown event kinds, statuses and levels', () => {
    // updated 是纯审计事件，永远进不了通知中心。
    expect(parseNotificationList(
      [notification({
        event_kind: 'updated' as unknown as InventoryNotification['event_kind'],
      })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ status: 'cancelled' as unknown as InventoryNotification['status'] })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ level: 'channel_total' as unknown as InventoryNotification['level'] })]).ok).toBe(false)
  })

  it('rejects bad refs', () => {
    expect(parseNotificationList(
      [notification({ notification_ref: 'badref' })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ alert_ref: 'alert has spaces' })]).ok).toBe(false)
  })

  it('rejects bad numbers and dates', () => {
    expect(parseNotificationList([notification({ quantity: 'abc' })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ quantity: 0 as unknown as InventoryNotification['quantity'] })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ created_at: 'not-a-date' })]).ok).toBe(false)
    expect(parseNotificationList(
      [notification({ read_at: 17 as unknown as InventoryNotification['read_at'] })]).ok).toBe(false)
  })

  it('rejects items that are not objects', () => {
    expect(parseNotificationList(['ntf-a']).ok).toBe(false)
    expect(parseNotificationList([null]).ok).toBe(false)
  })
})

describe('notificationActions：已读与确认是两个动作，且只看状态', () => {
  it('offers both actions to an unread open notification', () => {
    expect(notificationActions(notification())).toEqual({
      canRead: true, canAcknowledge: true })
  })

  it('stops offering read once read_at is set', () => {
    const actions = notificationActions(notification({ read_at: '2026-09-17T12:30:00+08:00' }))
    expect(actions.canRead).toBe(false)
  })

  it('never offers acknowledge for acknowledged, resolved or suppressed', () => {
    for (const status of ['acknowledged', 'resolved', 'suppressed'] as const) {
      expect(notificationActions(notification({ status })).canAcknowledge)
        .toBe(false)
    }
  })
})

describe('NotificationCenter 渲染', () => {
  it('shows the unread badge with a per-count accessible label', () => {
    const one = renderToStaticMarkup(
      <NotificationCenter notifications={[notification()]} open={false}
        error={null} {...handlers} />)
    expect(one).toContain('aria-label="1 条未读库存通知"')
    const two = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification(), notification({ notification_ref: 'ntf-' + 'b'.repeat(32) })
      ]} open={false} error={null} {...handlers} />)
    expect(two).toContain('aria-label="2 条未读库存通知"')
    const read = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification({ read_at: '2026-09-17T12:30:00+08:00' })
      ]} open={false} error={null} {...handlers} />)
    expect(read).toContain('aria-label="0 条未读库存通知"')
  })

  it('renders the panel only while open, with accessible region semantics', () => {
    const closed = renderToStaticMarkup(
      <NotificationCenter notifications={[notification()]} open={false}
        error={null} {...handlers} />)
    expect(closed).toContain('aria-expanded="false"')
    expect(closed).not.toContain('库存通知中心')
    const open = renderToStaticMarkup(
      <NotificationCenter notifications={[notification()]} open={true}
        error={null} {...handlers} />)
    expect(open).toContain('aria-expanded="true"')
    expect(open).toContain('aria-label="库存通知中心"')
  })

  it('separates triggered, retriggered and resolved wording', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification(),
        notification({ notification_ref: 'ntf-' + 'b'.repeat(32),
          alert_ref: 'alert-open0002', event_kind: 'retriggered',
          status: 'acknowledged', read_at: '2026-09-17T12:30:00+08:00' }),
        notification({ notification_ref: 'ntf-' + 'c'.repeat(32),
          alert_ref: 'alert-resolved01', event_kind: 'resolved',
          status: 'resolved', quantity: '9', read_at: '2026-09-17T12:30:00+08:00' }),
      ]} open={true} error={null} {...handlers} />)
    expect(html).toContain('首次触发')
    expect(html).toContain('持续异常')
    expect(html).toContain('已恢复')
    expect(html).not.toContain('已恢复的条件不')
  })

  it('never claims recovery on a card that is not resolved', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification({ event_kind: 'retriggered', status: 'acknowledged',
          read_at: '2026-09-17T12:30:00+08:00' }),
      ]} open={true} error={null} {...handlers} />)
    expect(html).toContain('数据未满足解除条件')
    expect(html).not.toContain('库存已恢复')
    expect(html).not.toContain('已恢复')
  })

  it('keeps read and acknowledge as separate actions on the right target', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[notification()]} open={true}
        error={null} {...handlers} />)
    expect(html).toContain('标记已读')
    expect(html).toContain('确认收到')
    expect(html).toContain('data-alert-ref="alert-open0001"')
  })

  it('hides the acknowledge button once acknowledged and marks read cards', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification({ status: 'acknowledged',
          read_at: '2026-09-17T12:30:00+08:00' }),
      ]} open={true} error={null} {...handlers} />)
    expect(html).toContain('已确认收到')
    expect(html).not.toContain('确认收到</button>')
    expect(html).toContain('已读')
    expect(html).not.toContain('标记已读')
  })

  it('renders quantity null as unread-never-zero while zero stays zero', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[
        notification({ quantity: null }),
        notification({ notification_ref: 'ntf-' + 'b'.repeat(32),
          alert_ref: 'alert-zero0001', quantity: '0' }),
      ]} open={true} error={null} {...handlers} />)
    expect(html).toContain('数量未读到')
    expect(html).toContain('数量 0')
  })

  it('always shows scope, reason, unit and the data snapshot time', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[notification()]} open={true}
        error={null} {...handlers} />)
    expect(html).toContain('pl-0123456789ab|wh-0123456789ab')
    expect(html).toContain('low_replenish')
    expect(html).toContain('piece')
    expect(html).toContain('ent-1a2b3c4d')
    expect(html).toContain('数据时点')
  })

  it('renders no card and no guessed placeholder for a level without a source', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[]} open={true} error={null}
        {...handlers} />)
    expect(html).toContain('暂无库存通知')
    expect(html).not.toContain('无法判断')
    expect(html).not.toContain('数据缺失')
    expect(html).not.toContain('店铺可售')
  })

  it('renders an explicit error and no cards when the payload was rejected', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[]} open={true}
        error="通知数据不符合契约，已拒绝渲染" {...handlers} />)
    expect(html).toContain('role="alert"')
    expect(html).toContain('通知数据不符合契约')
    expect(html).not.toContain('首次触发')
  })

  it('renders hostile text inert (escaped, never executable)', () => {
    const html = renderToStaticMarkup(
      <NotificationCenter notifications={[notification({
        scope_ref: '<img src=x onerror=alert(1)>',
        sku_ref: '<script>alert(1)</script>',
      })]} open={true} error={null} {...handlers} />)
    expect(html).toContain('&lt;img src=x')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<img src=x')
    expect(html).not.toContain('<script>')
  })
})

describe('createNotificationPoller：每 60 秒、只在页面可见时、可完全清理', () => {
  type Deps = Parameters<typeof createNotificationPoller>[0]

  function makeDeps() {
    // 替身带状态：定时器只在 armed 时才会真的 fire，可见性监听只在 attached
    // 时才派发——cancel/remove 之后再手动触发事件，必须什么都不发生。
    const deps = {
      refresh: vi.fn(async () => undefined),
      visible: true,
      armed: false,
      attached: false,
      scheduleCalls: 0,
      requestedInterval: 0,
      visibilityHandler: null as (() => void) | null,
      tick: null as (() => void) | null,
    }
    const schedule = vi.fn((tick: () => void, intervalMs: number) => {
      deps.tick = tick
      deps.requestedInterval = intervalMs
      deps.scheduleCalls += 1
      deps.armed = true
      return () => {
        deps.armed = false
      }
    })
    const onVisibilityChange = vi.fn((handler: () => void) => {
      deps.visibilityHandler = handler
      deps.attached = true
      return () => {
        deps.attached = false
      }
    })
    const fireTick = () => {
      if (deps.armed) deps.tick?.()
    }
    const fireVisibility = () => {
      if (deps.attached) deps.visibilityHandler?.()
    }
    return { deps, schedule, onVisibilityChange, fireTick, fireVisibility }
  }

  function poller(deps: ReturnType<typeof makeDeps>) {
    return createNotificationPoller({
      refresh: deps.deps.refresh,
      isVisible: () => deps.deps.visible,
      schedule: deps.schedule,
      onVisibilityChange: deps.onVisibilityChange,
    } satisfies Deps)
  }

  it('fetches immediately on start and every 60 seconds while visible', () => {
    const env = makeDeps()
    const center = poller(env)
    center.start()
    expect(env.deps.refresh).toHaveBeenCalledTimes(1)
    expect(env.schedule).toHaveBeenCalledTimes(1)
    expect(env.deps.requestedInterval).toBe(NOTIFICATION_POLL_INTERVAL_MS)
    expect(NOTIFICATION_POLL_INTERVAL_MS).toBe(60_000)
    env.fireTick()
    expect(env.deps.refresh).toHaveBeenCalledTimes(2)
    center.stop()
  })

  it('stops the timer while hidden and resumes with one refresh when visible', () => {
    const env = makeDeps()
    const center = poller(env)
    center.start()
    env.deps.visible = false
    env.fireVisibility()
    // 隐藏时定时器整个停掉。
    expect(env.deps.armed).toBe(false)
    env.fireTick()
    // 隐藏期间的 tick 不刷新。
    expect(env.deps.refresh).toHaveBeenCalledTimes(1)
    env.deps.visible = true
    env.fireVisibility()
    expect(env.deps.refresh).toHaveBeenCalledTimes(2)
    expect(env.schedule).toHaveBeenCalledTimes(2)
    center.stop()
  })

  it('does not fetch or schedule at all when starting hidden', () => {
    const env = makeDeps()
    env.deps.visible = false
    const center = poller(env)
    center.start()
    expect(env.deps.refresh).not.toHaveBeenCalled()
    expect(env.schedule).not.toHaveBeenCalled()
    expect(env.onVisibilityChange).toHaveBeenCalledTimes(1)
    center.stop()
  })

  it('stop clears the timer and the visibility listener completely', () => {
    const env = makeDeps()
    const center = poller(env)
    center.start()
    center.stop()
    expect(env.deps.armed).toBe(false)
    expect(env.deps.attached).toBe(false)
    env.fireTick()
    env.fireVisibility()
    // start 时的那次刷新之外不再刷新。
    expect(env.deps.refresh).toHaveBeenCalledTimes(1)
  })
})

describe('库存通知 API 调用与错误', () => {
  it('lists notifications through the authenticated GET channel', async () => {
    const fetchMock = stubFetch((url) => {
      expect(url).toBe('/api/notifications')
      return jsonResponse(200, [notification()])
    })
    const payload = await listNotifications()
    expect(payload).toHaveLength(1)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin' })
  })

  it('marks a notification read through the web-write boundary', async () => {
    const fetchMock = stubFetch((url, init) => {
      expect(url).toBe(`/api/notifications/ntf-${'a'.repeat(32)}/read`)
      expect(init).toMatchObject({
        method: 'POST', credentials: 'same-origin',
        headers: expect.objectContaining({
          'Content-Type': 'application/json', 'X-BI-Agent': 'web' }),
        body: '{}',
      })
      return jsonResponse(200, { notification_ref: 'ntf-x', read_at: null })
    })
    await markNotificationRead('ntf-' + 'a'.repeat(32))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('acknowledges an alert through the web-write boundary', async () => {
    const fetchMock = stubFetch((url, init) => {
      expect(url).toBe('/api/inventory-alerts/alert-open0001/acknowledge')
      expect(init?.method).toBe('POST')
      return jsonResponse(200, { alert_ref: 'alert-open0001',
        status: 'acknowledged', acknowledged_at: null })
    })
    await acknowledgeInventoryAlert('alert-open0001')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces the fixed public error envelope as an ApiError', async () => {
    stubFetch(() => jsonResponse(404, { code: 'not_found', message: '告警不存在' }))
    const caught = await acknowledgeInventoryAlert('alert-missing')
      .then(() => null, (error: unknown) => error)
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(404)
    expect((caught as ApiError).message).toBe('告警不存在')
  })
})

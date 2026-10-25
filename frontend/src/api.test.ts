import { describe, expect, it, vi } from 'vitest'

import { ApiError, createSseParser, sendMessage, uploadSycmFile } from './api'
import type { ChatEvent } from './types'

describe('createSseParser', () => {
  it('parses UTF-8 split inside a multibyte character', () => {
    const events: ChatEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    const bytes = new TextEncoder().encode(
      'event: error\ndata: {"code":"demo","message":"你好"}\n\n',
    )
    const split = bytes.findIndex((byte) => byte > 0x7f) + 1
    parser.push(bytes.slice(0, split))
    parser.push(bytes.slice(split))
    parser.finish()
    expect(events).toEqual([
      { event: 'error', data: { code: 'demo', message: '你好' } },
    ])
  })

  it('rejects unknown event names and unfinished frames', () => {
    expect(() => {
      const parser = createSseParser(() => {})
      parser.push(new TextEncoder().encode('event: token\ndata: {}\n\n'))
    }).toThrow('未知SSE事件')
    expect(() => {
      const parser = createSseParser(() => {})
      parser.push(new TextEncoder().encode('event: done\ndata: {"status":"complete"}'))
      parser.finish()
    }).toThrow('不完整')
  })

  it('reassembles frames split across chunks at arbitrary boundaries (T7)', () => {
    const events: ChatEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    const bytes = new TextEncoder().encode(
      'event: status\ndata: {"stage":"thinking"}\n\n'
      + 'event: done\ndata: {"status":"complete"}\n\n')
    // 逐字节喂入：任何切分点都不能丢帧或拆错
    for (const byte of bytes) parser.push(new Uint8Array([byte]))
    parser.finish()
    expect(events).toEqual([
      { event: 'status', data: { stage: 'thinking' } },
      { event: 'done', data: { status: 'complete' } },
    ])
  })

  it('accepts CRLF line endings and mixed separators (T7)', () => {
    const events: ChatEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    parser.push(new TextEncoder().encode(
      'event: status\r\ndata: {"stage":"querying"}\r\n\r\n'
      + 'event: done\ndata: {"status":"error"}\n\n'))
    parser.finish()
    expect(events).toEqual([
      { event: 'status', data: { stage: 'querying' } },
      { event: 'done', data: { status: 'error' } },
    ])
  })

  it('rejects invalid JSON in data without emitting any event (T7)', () => {
    const events: ChatEvent[] = []
    const parser = createSseParser((event) => events.push(event))
    expect(() => {
      parser.push(new TextEncoder().encode(
        'event: error\ndata: {"code":"demo",}\n\n'))
      parser.finish()
    }).toThrow('SSE JSON不合法')
    expect(events).toEqual([])
  })
})

describe('uploadSycmFile 请求形式（M4 契约：原始字节体 + WebWrite 适配头）', () => {
  const NAME = '淘店-沃金五金机电商城-店铺维度_20261001_ab12cd34.xlsx'
  const BYTES = [0x50, 0x4b, 0x03, 0x04]

  it('posts raw bytes with the web write headers and the encoded filename query', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      upload_id: '0f0e0d0c-0b0a-4999-8999-000000000001', status: 'accepted',
      reason_code: null, rows_read: 61, rows_written: 61, shops: ['沃金五金机电商城'],
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      await uploadSycmFile(new File([new Uint8Array(BYTES)], NAME))
      expect(fetchMock).toHaveBeenCalledTimes(1)
      // 无参 mock 的 calls 元组是 []：先过 unknown 再断言调用形状（tsc TS2352）。
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
      expect(url).toBe(`/api/uploads/sycm?filename=${encodeURIComponent(NAME)}`)
      expect(init.method).toBe('POST')
      expect(init.credentials).toBe('same-origin')
      expect(init.headers).toEqual({
        'Content-Type': 'application/octet-stream', 'X-BI-Agent': 'web',
      })
      expect(new Uint8Array(init.body as ArrayBuffer)).toEqual(new Uint8Array(BYTES))
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('raises ApiError carrying the status on non-2xx (404=功能关)', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ detail: { code: 'not_found', message: '功能未启用' } }),
      { status: 404 }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const failure = uploadSycmFile(new File([new Uint8Array(BYTES)], NAME))
        .catch((caught: unknown) => caught)
      expect(await failure).toBeInstanceOf(ApiError)
      expect(await failure.then((caught) => (caught as ApiError).status)).toBe(404)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('sendMessage 流契约（T7：done 前结束不能当成功）', () => {
  const encoder = new TextEncoder()

  function streamOf(chunks: string[]) {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
    })
    return new Response(body, { status: 200 })
  }

  it('resolves in order on a clean stream that ends with a done frame', async () => {
    const events: ChatEvent[] = []
    const fetchMock = vi.fn(async () => streamOf([
      'event: status\ndata: {"stage":"thinking"}\n\n',
      'event: message\ndata: {"role":"assistant","id":"m1","content":"好了",'
      + '"artifacts":[],"status":"complete","created_at":"2026-10-26T00:00:00Z"}\n\n',
      'event: done\ndata: {"status":"complete"}\n\n',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    try {
      await sendMessage('chat-1', '问题', (event) => events.push(event),
        new AbortController().signal)
      expect(events.map((event) => event.event)).toEqual(
        ['status', 'message', 'done'])
      expect(events[2]).toEqual({ event: 'done', data: { status: 'complete' } })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects a stream cut mid-frame and never emits done (fail closed)', async () => {
    const events: ChatEvent[] = []
    const fetchMock = vi.fn(async () => streamOf([
      'event: status\ndata: {"stage":"querying"}\n\n',
      // 第二帧只到一半就关流：模拟服务端/网络在 done 前中断
      'event: done\ndata: {"status":"comp',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const attempt = sendMessage('chat-1', '问题',
        (event) => events.push(event), new AbortController().signal)
      await expect(attempt).rejects.toThrow('不完整')
      // 被掐断的流绝不能发出 done：不完整回复不允许标 complete
      expect(events.map((event) => event.event)).toEqual(['status'])
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects an empty stream without a done terminal (review F3)', async () => {
    const fetchMock = vi.fn(async () => streamOf([]))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const attempt = sendMessage('chat-1', '问题', () => {},
        new AbortController().signal)
      await expect(attempt).rejects.toThrow('done')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('rejects complete frames followed by EOF without done (review F3)', async () => {
    // 完整的 status + message 帧后流干净地结束、但没有 done 终态：
    // 字节流读完不等于业务完成，不允许 resolve 成"正常返回"
    const events: ChatEvent[] = []
    const fetchMock = vi.fn(async () => streamOf([
      'event: status\ndata: {"stage":"answering"}\n\n',
      'event: message\ndata: {"role":"assistant","id":"m1","content":"半途",'
      + '"artifacts":[],"status":"complete","created_at":"2026-10-26T00:00:00Z"}\n\n',
    ]))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const attempt = sendMessage('chat-1', '问题',
        (event) => events.push(event), new AbortController().signal)
      await expect(attempt).rejects.toThrow('done')
      expect(events.map((event) => event.event)).toEqual(['status', 'message'])
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('surfaces a non-2xx response as ApiError before parsing any body', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ message: '服务器开小差' }), { status: 503 }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      const attempt = sendMessage('chat-1', '问题', () => {},
        new AbortController().signal)
      await expect(attempt).rejects.toBeInstanceOf(ApiError)
      await expect(attempt.catch((caught: unknown) =>
        (caught as ApiError).status)).resolves.toBe(503)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

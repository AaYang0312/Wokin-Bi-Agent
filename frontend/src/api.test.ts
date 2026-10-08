import { describe, expect, it, vi } from 'vitest'

import { ApiError, createSseParser, uploadSycmFile } from './api'
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

import { useEffect, useRef, useState } from 'react'

import {
  ApiError, createChat, deleteChat, listChats, loadMessages, probeReviewAccess,
  renameChat, sendMessage,
} from './api'
import { ChatView } from './components/ChatView'
import { QueryMemoryReviewSection } from './components/QueryMemoryReview'
import { Sidebar } from './components/Sidebar'
import type { Artifact, ChatMessage, ChatSummary, ReviewAccess } from './types'

const stageText: Record<string, string> = {
  thinking: '正在理解问题…',
  querying: '正在查询数据…',
  answering: '正在组织回答…',
}

// 抽屉断点：必须与 styles.css 里 @media (max-width: 767px) 保持一致。
const DRAWER_QUERY = '(max-width: 767px)'

function useNarrow() {
  const [narrow, setNarrow] = useState(() => (
    typeof window === 'undefined' ? false : window.matchMedia(DRAWER_QUERY).matches))
  useEffect(() => {
    if (typeof window === 'undefined') return
    const query = window.matchMedia(DRAWER_QUERY)
    const sync = () => setNarrow(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])
  return narrow
}

/**
 * 审核入口：只在后端确认审核能力（候选探针 200）时出现。404（功能关）与
 * 403（无权限）都不渲染——聊天界面本身永远不是审核入口，普通用户看不到任何
 * 审核痕迹；权限拒绝由面板内部表达。
 */
export function ReviewEntry({ access, open, onToggle }: {
  access: ReviewAccess | null
  open: boolean
  onToggle: () => void
}) {
  if (access !== 'available') return null
  return (
    <button type="button" className={`review-toggle${open ? ' open' : ''}`}
      onClick={onToggle} aria-expanded={open} aria-controls="review-dock">
      审核记忆
    </button>
  )
}

export default function App() {
  const [chats, setChats] = useState<ChatSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [status, setStatus] = useState<string | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [error, setError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [reviewAccess, setReviewAccess] = useState<ReviewAccess | null>(null)
  const [reviewOpen, setReviewOpen] = useState(false)
  const narrow = useNarrow()
  const controller = useRef<AbortController | null>(null)
  const selectedRef = useRef<string | null>(null)
  const menuToggleRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const drawerWasOpen = useRef(false)

  useEffect(() => { selectedRef.current = selectedId }, [selectedId])

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSidebarOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [])

  // 审核能力探测：只在挂载时问一次后端（GET candidates）。
  // available 才出现入口；forbidden/off/unavailable 都保持聊天界面原样。
  useEffect(() => {
    let cancelled = false
    void probeReviewAccess().then((access) => {
      if (!cancelled) setReviewAccess(access)
    })
    return () => { cancelled = true }
  }, [])

  // 窄屏抽屉的焦点交接：开时进入抽屉，收时回给触发按钮（inert 会把焦点丢给 body）。
  useEffect(() => {
    if (!narrow) {
      drawerWasOpen.current = sidebarOpen
      return
    }
    if (sidebarOpen) drawerRef.current?.querySelector<HTMLElement>('.mobile-close')?.focus()
    else if (drawerWasOpen.current) menuToggleRef.current?.focus()
    drawerWasOpen.current = sidebarOpen
  }, [narrow, sidebarOpen])

  async function refreshChats() {
    const next = await listChats()
    setChats(next)
    return next
  }

  async function selectChat(chatId: string) {
    controller.current?.abort()
    controller.current = null
    setSelectedId(chatId)
    setMessages([])
    setStatus(null)
    setArtifacts([])
    setError(null)
    try {
      setMessages(await loadMessages(chatId))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '加载会话失败')
    }
  }

  async function makeChat() {
    try {
      const chat = await createChat()
      setChats((current) => [chat, ...current])
      await selectChat(chat.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '新建会话失败')
    }
  }

  useEffect(() => {
    void (async () => {
      try {
        const next = await refreshChats()
        if (next[0]) await selectChat(next[0].id)
        else await makeChat()
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '初始化失败')
      }
    })()
    return () => controller.current?.abort()
  }, [])

  async function rename(chatId: string, title: string) {
    try {
      const updated = await renameChat(chatId, title)
      setChats((current) => current.map((chat) => chat.id === chatId ? updated : chat))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '改名失败')
    }
  }

  async function remove(chatId: string) {
    if (!window.confirm('删除这条对话及其消息？')) return
    try {
      await deleteChat(chatId)
      const next = await refreshChats()
      if (chatId === selectedRef.current) {
        if (next[0]) await selectChat(next[0].id)
        else await makeChat()
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '删除失败')
    }
  }

  async function send(content: string) {
    const chatId = selectedRef.current
    if (!chatId || controller.current) return
    const local: ChatMessage = {
      id: `local-${Date.now()}`, role: 'user', content, artifacts: [], status: 'complete',
      created_at: new Date().toISOString(),
    }
    const active = new AbortController()
    controller.current = active
    setMessages((current) => [...current, local])
    setStatus('正在理解问题…')
    setArtifacts([])
    setError(null)
    try {
      await sendMessage(chatId, content, (event) => {
        if (selectedRef.current !== chatId) return
        if (event.event === 'status') setStatus(stageText[event.data.stage])
        if (event.event === 'artifact') setArtifacts((current) => [...current, event.data])
        if (event.event === 'message') setMessages((current) => [...current, event.data])
        if (event.event === 'error') setError(event.data.message)
      }, active.signal)
      if (selectedRef.current === chatId) setMessages(await loadMessages(chatId))
      await refreshChats()
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
        const message = caught instanceof ApiError || caught instanceof Error ? caught.message : '发送失败'
        setError(message)
      }
    } finally {
      if (controller.current === active) controller.current = null
      if (selectedRef.current === chatId) {
        setStatus(null)
        setArtifacts([])
      }
    }
  }

  const active = chats.find((chat) => chat.id === selectedId) ?? null

  return (
    <div className="app-shell">
      <ReviewEntry access={reviewAccess} open={reviewOpen}
        onToggle={() => setReviewOpen((value) => !value)} />
      <div className="workspace">
        <Sidebar chats={chats} selectedId={selectedId} busy={controller.current !== null}
          open={sidebarOpen} narrow={narrow} drawerRef={drawerRef}
          onClose={() => setSidebarOpen(false)} onCreate={makeChat}
          onSelect={(chatId) => { setSidebarOpen(false); void selectChat(chatId) }}
          onRename={rename} onDelete={remove} />
        <ChatView messages={messages} status={status} artifacts={artifacts} error={error}
          disabled={controller.current !== null || !selectedId}
          title={active?.title ?? null} updatedAt={active?.updated_at ?? null} chatCount={chats.length}
          narrow={narrow} drawerOpen={sidebarOpen} menuToggleRef={menuToggleRef}
          onOpenSidebar={() => setSidebarOpen(true)} onSend={send}
          onDrilldown={(intent) => void send(intent.question)} />
      </div>
      {reviewOpen && reviewAccess === 'available' && (
        <div className="review-dock" id="review-dock" role="dialog" aria-label="记忆审核">
          <QueryMemoryReviewSection onClose={() => setReviewOpen(false)} />
        </div>
      )}
    </div>
  )
}

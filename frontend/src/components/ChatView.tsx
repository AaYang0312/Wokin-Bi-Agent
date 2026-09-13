import type { RefObject } from 'react'

import type { Artifact, ChatMessage, DrilldownIntent } from '../types'
import { ArtifactView } from './ArtifactView'
import { Composer } from './Composer'
import { AlertIcon, BrandMark, ClockIcon, GaugeIcon, MenuIcon } from './icons'
import { MessageView } from './MessageView'

const examples = [
  '最近7天支付金额如何？',
  '9月1日至7日退款发生多少？',
  '假设下月销售额10万元，推广费率12%',
]

function shortTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function ChatView({
  messages, status, artifacts, error, disabled, title, updatedAt, chatCount, narrow, drawerOpen,
  onOpenSidebar, onSend, onDrilldown, menuToggleRef,
}: {
  messages: ChatMessage[]
  status: string | null
  artifacts: Artifact[]
  error: string | null
  disabled: boolean
  title: string | null
  updatedAt: string | null
  chatCount: number
  narrow: boolean
  drawerOpen: boolean
  onOpenSidebar: () => void
  onSend: (content: string) => void
  /** 图表上的平台下钻：和"输入一句新问题"走同一条路，因此仍会在服务端重新授权。 */
  onDrilldown?: (intent: DrilldownIntent) => void
  menuToggleRef?: RefObject<HTMLButtonElement | null>
}) {
  const stamp = updatedAt ? shortTime(updatedAt) : null
  // 抽屉只在窄屏浮出：这时背后的内容才需要 inert + aria-hidden。
  const covered = narrow && drawerOpen

  return (
    <main className="chat-main" aria-hidden={covered || undefined} inert={covered}>
      <header className="topbar">
        <button className="menu-toggle" ref={menuToggleRef} onClick={onOpenSidebar}
          aria-label="打开会话列表" aria-expanded={drawerOpen}
          aria-controls="chat-nav-drawer"><MenuIcon /></button>
        <span className="topbar-icon" aria-hidden="true"><GaugeIcon size={19} /></span>
        <h1 className="topbar-title">{title ?? '经营助手'}</h1>
        <div className="topbar-right">
          {stamp && <span className="pill pill-meta"><ClockIcon size={13} />更新于 {stamp}</span>}
          <span className={`pill pill-live ${status ? 'on' : ''}`}>
            <span className="dot" aria-hidden="true" />{status ? '生成中' : '待命'}
          </span>
        </div>
      </header>

      <div className="message-list" aria-live="polite">
        {messages.length === 0 ? (
          <div className="empty-state">
            <p className="hero-kicker"><span>已建立</span><strong>{chatCount}</strong><span>个会话</span></p>
            <h2 className="hero-title">经营数据，直接问。</h2>
            <p>可查询已覆盖期间的经营情况，或基于明确假设测算推广预算。</p>
            <div className="example-list">
              {examples.map((example) => (
                <button key={example} onClick={() => onSend(example)}>
                  <span className="example-icon" aria-hidden="true"><BrandMark size={14} /></span>
                  <span>{example}</span>
                </button>
              ))}
            </div>
          </div>
        ) : messages.map((message) => (
          <MessageView key={message.id} message={message} onDrilldown={onDrilldown} />
        ))}
        {status && <p className="stream-status" role="status">{status}</p>}
        {artifacts.map((artifact, index) => (
          <ArtifactView
            artifact={artifact}
            datasets={artifacts}
            onDrilldown={onDrilldown}
            key={index} />
        ))}
        {error && <p className="stream-error" role="alert"><AlertIcon size={15} />{error}</p>}
      </div>

      <div className="composer-wrap">
        <Composer disabled={disabled} onSend={onSend} />
      </div>
    </main>
  )
}

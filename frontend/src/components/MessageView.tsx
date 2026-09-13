import type { ReactNode } from 'react'

import type { ChatMessage } from '../types'
import type { Align, Block, Inline } from '../richText'
import { ASCII_TABLE_LANGUAGE, parseRichText } from '../richText'
import type { DrilldownIntent } from '../types'
import { ArtifactView } from './ArtifactView'
import { BrandMark } from './icons'

function renderInline(inline: Inline[], keyPrefix: string): ReactNode[] {
  return inline.map((part, index) => {
    const key = `${keyPrefix}-${index}`
    switch (part.kind) {
      case 'strong':
        return <strong key={key}>{part.text}</strong>
      case 'em':
        return <em key={key}>{part.text}</em>
      case 'strike':
        return <del key={key}>{part.text}</del>
      case 'code':
        return <code key={key}>{part.text}</code>
      case 'link':
        return (
          <a key={key} href={part.href} target="_blank" rel="noreferrer noopener">
            {part.text}
          </a>
        )
      default:
        // 纯文本直接作为字符串节点，避免每个句子都套一层无意义的 span。
        return part.text
    }
  })
}

function alignStyle(align: Align): React.CSSProperties {
  return align ? { textAlign: align } : {}
}

function renderBlock(block: Block, index: number): ReactNode {
  const key = `block-${index}`
  switch (block.type) {
    case 'heading': {
      const content = renderInline(block.inline, key)
      if (block.level === 1) return <h2 key={key}>{content}</h2>
      if (block.level === 2) return <h3 key={key}>{content}</h3>
      return <h4 key={key}>{content}</h4>
    }
    case 'list': {
      const items = block.items.map((item, at) => (
        <li key={`${key}-${at}`}>{renderInline(item, `${key}-${at}`)}</li>
      ))
      return block.ordered
        ? <ol key={key} start={block.start}>{items}</ol>
        : <ul key={key}>{items}</ul>
    }
    case 'table':
      return (
        <div className="table-wrap" key={key}>
          <table className="rich-table">
            <thead>
              <tr>
                {block.header.map((cell, at) => (
                  <th key={`${key}-h-${at}`} style={alignStyle(block.aligns[at])}>
                    {renderInline(cell, `${key}-h-${at}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${key}-r-${rowIndex}`}>
                  {row.map((cell, at) => (
                    <td key={`${key}-r-${rowIndex}-${at}`} style={alignStyle(block.aligns[at])}>
                      {renderInline(cell, `${key}-r-${rowIndex}-${at}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'code':
      return (
        <pre key={key} className={block.language === ASCII_TABLE_LANGUAGE ? 'rich-code ascii-table' : 'rich-code'}>
          <code>{block.text}</code>
        </pre>
      )
    case 'rule':
      return <hr key={key} />
    default:
      return <p key={key}>{renderInline(block.inline, key)}</p>
  }
}

export function RichContent({ text }: { text: string }) {
  return <div className="rich-content">{parseRichText(text).map(renderBlock)}</div>
}

export function MessageView({
  message, onDrilldown,
}: {
  message: ChatMessage
  /** 图表下钻：外层把它变成一次新的、仍由服务端展开授权的提问。 */
  onDrilldown?: (intent: DrilldownIntent) => void
}) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-label">
        {message.role === 'assistant' && <span className="label-mark" aria-hidden="true"><BrandMark size={13} /></span>}
        {message.role === 'user' ? '你' : '经营助手'}
      </div>
      <div className="message-content">
        {message.role === 'assistant' ? <RichContent text={message.content} /> : message.content}
      </div>
      {message.artifacts.map((artifact, index) => (
        <ArtifactView
          key={index}
          artifact={artifact}
          datasets={message.artifacts}
          onDrilldown={onDrilldown} />))}
    </article>
  )
}

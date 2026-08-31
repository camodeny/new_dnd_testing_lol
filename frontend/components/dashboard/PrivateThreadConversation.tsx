'use client'

import { useMemo, useState } from 'react'
import MarkdownContent from '@/components/common/MarkdownContent'
import { useLiveTableRealtime } from '@/hooks/useLiveTableRealtime'
import { gameplayThreads } from '@/lib/api'
import { privateThreadLabel } from '@/lib/privateThreads'
import type { CampaignMember, CampaignThread, User } from '@/types'

interface PrivateThreadConversationProps {
  campaignId: string
  thread: CampaignThread
  members: CampaignMember[]
  currentUser: User
  onClose: () => void
  onError: (message: string) => void
}

type ConversationEntry = {
  id: string
  role: 'player' | 'dm'
  content: string
  author: string
  timestamp: string | null
  order: number
}

export default function PrivateThreadConversation({
  campaignId,
  thread,
  members,
  currentUser,
  onClose,
  onError,
}: PrivateThreadConversationProps) {
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const realtime = useLiveTableRealtime({ campaignId, threadId: thread.id })
  const title = privateThreadLabel(thread, members, currentUser.id)

  const entries = useMemo<ConversationEntry[]>(() => {
    const playerEntries = realtime.messages.map((message) => {
      const userId = String(message.user_id ?? '')
      return {
        id: String(message.id ?? message.event_id),
        role: 'player' as const,
        content: String(message.raw_content ?? ''),
        author: userId === currentUser.id
          ? currentUser.username
          : members.find((member) => member.user_id === userId)?.username ?? 'Player',
        timestamp: typeof message.accepted_at === 'string' ? message.accepted_at : null,
        order: Number(message.sequence ?? 0),
      }
    })
    const dmEntries = realtime.dmMessages.map((message, index) => ({
      id: `dm:${message.id}`,
      role: 'dm' as const,
      content: message.final_text,
      author: 'AI DM',
      timestamp: typeof message.completed_at === 'string' ? message.completed_at : null,
      order: 1_000_000 + index,
    }))
    return [...playerEntries, ...dmEntries].sort((left, right) => {
      if (left.timestamp && right.timestamp) {
        const timeDifference = Date.parse(left.timestamp) - Date.parse(right.timestamp)
        if (timeDifference) return timeDifference
      }
      return left.order - right.order
    })
  }, [currentUser.id, currentUser.username, members, realtime.dmMessages, realtime.messages])

  const submit = async () => {
    const content = input.trim()
    if (!content || sending) return
    setSending(true)
    setInput('')
    try {
      const operationId = crypto.randomUUID()
      await gameplayThreads.submit(campaignId, thread.id, content, operationId)
      await realtime.refresh()
    } catch (error) {
      setInput(content)
      onError((error as Error).message)
    } finally {
      setSending(false)
    }
  }

  const isAiDmThread = thread.private_kind === 'dm'
  const aiVisibleText = realtime.dmState?.streaming ? String(realtime.dmState.visible_text ?? '') : ''

  return (
    <div className="private-thread-conversation">
      <header className="dashboard-top-header private-thread-header">
        <div>
          <div className="private-thread-eyebrow"><i className="bi bi-lock-fill" aria-hidden="true" /> Private</div>
          <div className="location-name">{title}</div>
          <div className="private-thread-subtitle">
            {isAiDmThread
              ? 'Only you and the AI DM can read this conversation.'
              : 'Only the selected players can read this conversation.'}
          </div>
        </div>
        <button type="button" className="btn btn-secondary small" onClick={onClose}>
          Back to table
        </button>
      </header>

      <div className="session-messages private-thread-messages">
        {realtime.error && (
          <div className="private-thread-reconnect" role="status">
            Reconnecting from the private snapshot. {realtime.error}
          </div>
        )}
        {entries.length === 0 && !aiVisibleText && (
          <div className="private-thread-empty">
            <i className={isAiDmThread ? 'bi bi-stars' : 'bi bi-chat-heart'} aria-hidden="true" />
            <p>{isAiDmThread ? 'Ask the AI DM something away from the table.' : 'Start a conversation visible only to its players.'}</p>
          </div>
        )}
        {entries.map((entry) => (
          <article className="session-msg private-thread-message" key={entry.id}>
            <div className={`session-msg-avatar${entry.role === 'dm' ? ' dm-avatar' : ''}`} aria-hidden="true">
              {entry.role === 'dm' ? 'AI' : entry.author.slice(0, 2).toUpperCase()}
            </div>
            <div>
              <div className="private-thread-message-meta">
                <strong>{entry.author}</strong>
                {entry.timestamp && <time>{new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>}
              </div>
              {entry.role === 'dm' ? <MarkdownContent content={entry.content} /> : <p>{entry.content}</p>}
            </div>
          </article>
        ))}
        {aiVisibleText && (
          <article className="session-msg private-thread-message is-streaming">
            <div className="session-msg-avatar dm-avatar" aria-hidden="true">AI</div>
            <div>
              <div className="private-thread-message-meta"><strong>AI DM</strong><span>responding…</span></div>
              <MarkdownContent content={aiVisibleText} />
            </div>
          </article>
        )}
      </div>

      <div className="session-input-area private-thread-input-area">
        <div className="session-input-shell">
          <textarea
            className="session-input-editable"
            aria-label={`Message ${title}`}
            placeholder={isAiDmThread ? 'Whisper to the AI DM…' : `Message ${title}…`}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void submit()
              }
            }}
            rows={1}
            disabled={sending}
          />
          <button type="button" className="session-send-btn" aria-label="Send private message" onClick={() => void submit()} disabled={!input.trim() || sending}>
            <i className="bi bi-send-fill" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  )
}

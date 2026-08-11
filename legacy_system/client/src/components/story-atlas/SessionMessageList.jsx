import { useLayoutEffect, useRef } from 'react'
import { SessionMessageItem } from '../dashboard/SessionPanel'

export default function SessionMessageList({
  messages = [],
  currentUser = null,
  session = null,
  onProposalApplied = () => {},
  onProposalDismissed = () => {},
  characters = [],
  aiThinking = false,
  aiThinkingStatus = '',
  hasOlderMessages = false,
  loadingOlderMessages = false,
  onLoadOlderMessages = null,
}) {
  const messagesContainerRef = useRef(null)
  const messagesEndRef = useRef(null)
  const olderLoadScrollRef = useRef(null)
  const previousMessageCountRef = useRef(0)

  useLayoutEffect(() => {
    const container = messagesContainerRef.current
    if (!container) return

    if (olderLoadScrollRef.current) {
      container.scrollTop = container.scrollHeight - olderLoadScrollRef.current.previousScrollHeight + olderLoadScrollRef.current.previousScrollTop
      olderLoadScrollRef.current = null
      previousMessageCountRef.current = messages.length
      return
    }

    const previousCount = previousMessageCountRef.current
    previousMessageCountRef.current = messages.length

    if (previousCount === 0 && messages.length > 0) {
      container.scrollTop = container.scrollHeight
      setTimeout(() => {
        if (messagesContainerRef.current) {
          messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight
        }
      }, 50)
      return
    }

    if (messages.length > previousCount) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  const loadOlderFromTop = (force = false) => {
    const container = messagesContainerRef.current
    if (!container || !hasOlderMessages || loadingOlderMessages || !onLoadOlderMessages) return
    if (!force && container.scrollTop > 80) return

    olderLoadScrollRef.current = {
      previousScrollHeight: container.scrollHeight,
      previousScrollTop: container.scrollTop,
    }
    Promise.resolve(onLoadOlderMessages())
      .then((loadedCount) => {
        if (!loadedCount) olderLoadScrollRef.current = null
      })
      .catch(() => {
        olderLoadScrollRef.current = null
      })
  }

  const handleMessagesScroll = () => loadOlderFromTop(false)

  return (
    <div className="sampler-messages" ref={messagesContainerRef} onScroll={handleMessagesScroll} style={{ height: '100%', overflowY: 'auto' }}>
      {(hasOlderMessages || loadingOlderMessages) && (
        <div className="session-load-history" style={{ textAlign: 'center', padding: '12px' }}>
          <button
            type="button"
            className="btn btn-secondary small"
            onClick={() => loadOlderFromTop(true)}
            disabled={loadingOlderMessages}
          >
            {loadingOlderMessages ? 'Loading older messages...' : 'Load older messages'}
          </button>
        </div>
      )}
      {messages.length === 0 && (
        <div className="sampler-empty" style={{ textAlign: 'center', padding: '24px', opacity: 0.7 }}>
          <span>✦</span>
          <h2>Empty Conversation</h2>
          <p>No messages to display. Describe your action in the composer below to begin.</p>
        </div>
      )}
      {messages.map((msg) => (
        <SessionMessageItem
          key={msg.id}
          msg={msg}
          currentUser={currentUser}
          sessionId={session?.id}
          onProposalApplied={onProposalApplied}
          onProposalDismissed={onProposalDismissed}
          characters={characters}
        />
      ))}
      {aiThinking && (
        <div className="sampler-thinking">
          <span><i /><i /><i /></span>
          <div>
            <strong>AI Dungeon Master is responding</strong>
            <small>{aiThinkingStatus || 'Checking the scene, your character, and recent events'}</small>
          </div>
        </div>
      )}
      <div ref={messagesEndRef} />
    </div>
  )
}

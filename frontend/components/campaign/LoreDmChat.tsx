'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { campaignMembers as membersApi } from '@/lib/api'

interface LoreDmChatProps {
  campaignId: string
  characterId: string
  onUseLore: (proposal: string) => void
  applying: boolean
}

type ChatMsg = { role: 'user' | 'assistant'; content: string; proposal?: string }

const WELCOME: ChatMsg = {
  role: 'assistant',
  content: 'Not sure what private lore is? It\u2019s secrets, debts, and goals only you and the DM will ever see. Tell me a little about your character and we\u2019ll find something worth keeping.',
}

function chatUrl(campaignId: string, characterId: string): string {
  // Same-origin through the Next /api rewrite (targets the live backend).
  // Do NOT bypass to a hardcoded backend port: nothing listens there and
  // the rewrite is what carries auth in local dev.
  return `/api/campaigns/${encodeURIComponent(campaignId)}/characters/${encodeURIComponent(characterId)}/lore-chat`
}

export default function LoreDmChat({ campaignId, characterId, onUseLore, applying }: LoreDmChatProps) {
  const [messages, setMessages] = useState<ChatMsg[]>([WELCOME])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [chatError, setChatError] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    setMessages([WELCOME])
    setChatError('')
    membersApi
      .getLoreDmChat(campaignId, characterId)
      .then((data) => {
        if (cancelled) return
        const hist = ((data as { messages?: ChatMsg[] }).messages ?? []).map((m) => ({
          role: m.role === 'assistant' ? 'assistant' as const : 'user' as const,
          content: m.content,
          ...(m.proposal ? { proposal: m.proposal } : {}),
        }))
        if (hist.length > 0) setMessages(hist)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [campaignId, characterId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'nearest' })
  }, [messages])

  const patchAiMessage = useCallback((aiIndex: number, update: (m: ChatMsg) => ChatMsg) => {
    setMessages((prev) => {
      const copy = [...prev]
      if (copy[aiIndex] && copy[aiIndex].role === 'assistant') {
        copy[aiIndex] = update(copy[aiIndex])
      } else if (copy.length === aiIndex) {
        copy.push(update({ role: 'assistant', content: '' }))
      }
      return copy
    })
  }, [])

  const handleSend = useCallback(async () => {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setChatError('')
    const history = [...messages]
    const aiIndex = history.length + 1
    setMessages((m) => [...m, { role: 'user', content: text }])
    setLoading(true)
    let fullText = ''
    try {
      const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
      const res = await fetch(chatUrl(campaignId, characterId), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ content: text }),
      })
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed || (!trimmed.startsWith('data:') && !trimmed.startsWith('event:'))) continue
          if (trimmed.startsWith('event:')) {
            if (trimmed.includes('error')) throw new Error('chat unavailable')
            continue
          }
          const dataStr = trimmed.slice(5).trim()
          if (!dataStr) continue
          try {
            const data = JSON.parse(dataStr)
            if (data.type === 'token' && typeof data.text === 'string') {
              fullText += data.text
              const snapshot = fullText
              patchAiMessage(aiIndex, (m) => ({ ...m, content: snapshot }))
            } else if (data.type === 'proposal' && typeof data.lore_text === 'string') {
              const proposal = data.lore_text
              patchAiMessage(aiIndex, (m) => ({ ...m, proposal }))
            }
          } catch {
            // ignore partial JSON
          }
        }
      }
      if (!fullText) {
        patchAiMessage(aiIndex, (m) => (
          m.content ? m : { ...m, content: "Hmm, I didn't get a response — try again?" }
        ))
      }
    } catch (err) {
      setChatError((err as Error).message || 'Chat failed')
    } finally {
      setLoading(false)
    }
  }, [input, loading, campaignId, characterId, messages, patchAiMessage])

  return (
    <div className="lobby-lore-chat" aria-label="Work out lore with the DM">
      <div className="lobby-lore-chat-log" role="log" aria-live="polite">
        {messages.map((m, i) => (
          <div key={i} className={`lobby-lore-chat-msg is-${m.role}`}>
            <div className="lobby-lore-chat-bubble">{m.content}</div>
            {m.role === 'assistant' && m.proposal && (
              <div className="lobby-lore-proposal">
                <div className="lobby-lore-proposal-label">Lore draft</div>
                <p className="lobby-lore-proposal-text">{m.proposal}</p>
                <button
                  type="button"
                  className="lobby-generate-btn"
                  onClick={() => onUseLore(m.proposal as string)}
                  disabled={applying}
                >
                  Use this lore
                </button>
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      {chatError && <div className="lobby-lore-chat-error" role="alert">{chatError}</div>}
      <div className="lobby-lore-chat-composer">
        <input
          type="text"
          aria-label="Message the DM about your lore"
          placeholder="Tell the DM about your character…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void handleSend() }}
          disabled={loading}
        />
        <button type="button" className="lobby-generate-btn" onClick={() => void handleSend()} disabled={loading || !input.trim()}>
          <i className="bi bi-send" aria-hidden="true" /> Send
        </button>
      </div>
    </div>
  )
}

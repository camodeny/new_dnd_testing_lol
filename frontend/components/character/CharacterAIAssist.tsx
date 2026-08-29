'use client'

import { useEffect, useState } from 'react'
import Button from '@/components/common/Button'
import MarkdownContent from '@/components/common/MarkdownContent'
import type { CharacterDraft } from './characterFormConfig'

interface Props {
  onGenerated: (draft: Partial<CharacterDraft>) => void
  characterId?: string
  draftCharacter?: Record<string, unknown> | CharacterDraft | null
  activePage?: string | null
  clearTrigger?: number
}

const EXAMPLES = [
  'a grumpy dwarf cleric who loves ale and hates goblins',
  'elven ranger, 2nd level, folk hero, loves animals',
  'tiefling rogue 3, charlatan, sneaky but loyal',
]

// naive keyword -> draft mapper (placeholder for real LLM call)
function mockGenerate(prompt: string): Partial<CharacterDraft> {
  const lower = prompt.toLowerCase()
  const draft: Partial<CharacterDraft> = {}

  // race
  if (lower.includes('elf')) draft.race = 'Elf'
  else if (lower.includes('dwarf')) draft.race = 'Dwarf'
  else if (lower.includes('tiefling')) draft.race = 'Tiefling'
  else if (lower.includes('halfling')) draft.race = 'Halfling'
  else if (lower.includes('orc')) draft.race = 'Half-Orc'
  else if (lower.includes('human')) draft.race = 'Human'
  else if (lower.includes('dragonborn')) draft.race = 'Dragonborn'
  else if (lower.includes('gnome')) draft.race = 'Gnome'

  // background
  if (lower.includes('folk hero')) draft.background = 'Folk Hero'
  else if (lower.includes('charlatan')) draft.background = 'Charlatan'
  else if (lower.includes('soldier')) draft.background = 'Soldier'
  else if (lower.includes('sage')) draft.background = 'Sage'

  // classes
  const classMap: Record<string, string> = {
    wizard: 'Wizard', cleric: 'Cleric', rogue: 'Rogue', ranger: 'Ranger',
    fighter: 'Fighter', barbarian: 'Barbarian', bard: 'Bard', druid: 'Druid',
    paladin: 'Paladin', monk: 'Monk', sorcerer: 'Sorcerer', warlock: 'Warlock',
  }
  const hitDiceByClass: Record<string, string> = {
    Barbarian: 'd12',
    Fighter: 'd10', Paladin: 'd10', Ranger: 'd10',
    Bard: 'd8', Cleric: 'd8', Druid: 'd8', Monk: 'd8', Rogue: 'd8', Warlock: 'd8',
    Sorcerer: 'd6', Wizard: 'd6',
  }
  const foundClasses: { class_name: string; level: number; subclass: string; hit_die_type: string }[] = []
  for (const [kw, name] of Object.entries(classMap)) {
    if (lower.includes(kw)) {
      const m = lower.match(new RegExp(`${kw}[^\\d]{0,10}(\\d+)`))
      const lvl = m ? Math.min(20, Math.max(1, parseInt(m[1], 10))) : 1
      foundClasses.push({ class_name: name, level: lvl, subclass: '', hit_die_type: hitDiceByClass[name] ?? 'd8' })
    }
  }
  if (foundClasses.length) (draft as unknown as Record<string, unknown>).classes = foundClasses

  // name - take first quoted or first capitalized words
  const nameMatch = prompt.match(/["']([^"']+)["']/) || prompt.match(/named?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)/i)
  if (nameMatch) draft.name = nameMatch[1].trim()
  else {
    const cap = prompt.match(/\b([A-Z][a-z]+)\b/)
    if (cap && !['I', 'A'].includes(cap[1])) draft.name = cap[1]
  }

  // ability scores - give class-appropriate bump
  const abilityScores: Record<string, number> = {
    strength: 10, dexterity: 10, constitution: 10, intelligence: 10, wisdom: 10, charisma: 10,
  }
  if (lower.includes('wizard') || lower.includes('sage')) { abilityScores.intelligence = 15; abilityScores.wisdom = 13 }
  if (lower.includes('cleric')) { abilityScores.wisdom = 15; abilityScores.constitution = 13 }
  if (lower.includes('rogue')) { abilityScores.dexterity = 15; abilityScores.charisma = 13 }
  if (lower.includes('fighter') || lower.includes('barbarian')) { abilityScores.strength = 15; abilityScores.constitution = 14 }
  if (lower.includes('ranger')) { abilityScores.dexterity = 15; abilityScores.wisdom = 13 }
  if (lower.includes('bard') || lower.includes('paladin')) { abilityScores.charisma = 15 }
  draft.ability_scores = abilityScores

  // personality from prompt remainder
  if (prompt.length > 20) {
    (draft as unknown as Record<string, unknown>).personality = {
      personality_traits: prompt.slice(0, 120),
      ideals: '', bonds: '', flaws: '',
    }
    ;(draft as unknown as Record<string, unknown>).background_details = {
      backstory: prompt,
      allies_organizations: '', additional_features_traits: '', treasure: '',
    }
  }

  return draft
}

type ChatMsg = { role: 'ai' | 'user'; content: string }

const WELCOME: ChatMsg = { role: 'ai', content: "Hey! I can help you build your D&D 5e sheet — just describe who you imagine (e.g. 'grumpy dwarf cleric who loves ale' or 'shy half-elf druid, level 2'). I'll draft the stats and you can tweak everything before saving." }

export default function CharacterAIAssist({ onGenerated, characterId = 'new', draftCharacter, activePage, clearTrigger }: Props) {
  const [messages, setMessages] = useState<ChatMsg[]>([WELCOME])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (clearTrigger) setMessages([WELCOME])
  }, [clearTrigger])

  useEffect(() => {
    let cancelled = false
    async function loadHistory() {
      const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
      const backendBase =
        (typeof process !== 'undefined' && (process.env.NEXT_PUBLIC_BACKEND_URL as string | undefined)) ||
        (typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'http://localhost:5889' : '')
      const chatUrl = backendBase
        ? `${backendBase.replace(/\/$/, '')}/api/characters/${encodeURIComponent(characterId)}/chat`
        : `/api/characters/${encodeURIComponent(characterId)}/chat`
      try {
        const res = await fetch(chatUrl, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        if (!res.ok) return
        const data = await res.json()
        if (cancelled) return
        if (Array.isArray(data.messages) && data.messages.length > 0) {
          const mapped: ChatMsg[] = data.messages.map((m: { role: string; content: string }) => ({
            role: m.role === 'assistant' ? 'ai' : 'user',
            content: m.content,
          }))
          setMessages(mapped as ChatMsg[])
        } else {
          setMessages([WELCOME])
        }
      } catch {
        // keep welcome
      }
    }
    loadHistory()
    return () => {
      cancelled = true
    }
  }, [characterId])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    const history = [...messages]
    setMessages((m) => [...m, { role: 'user', content: text }])
    setLoading(true)

    // ai will be appended on first token; no empty placeholder to avoid duplicate bubbles
    const aiIndex = history.length + 1

    const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null
    // bypass Next rewrites for SSE to avoid buffering; hit backend directly when local
    const backendBase =
      (typeof process !== 'undefined' && (process.env.NEXT_PUBLIC_BACKEND_URL as string | undefined)) ||
      (typeof window !== 'undefined' && window.location.hostname === 'localhost' ? 'http://localhost:5889' : '')
    const chatUrl = backendBase
      ? `${backendBase.replace(/\/$/, '')}/api/characters/${encodeURIComponent(characterId)}/chat`
      : `/api/characters/${encodeURIComponent(characterId)}/chat`

    try {
      const res = await fetch(chatUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          content: text,
          history: history.map((m) => ({ role: m.role === 'ai' ? 'assistant' : m.role, content: m.content })),
          draft_character: draftCharacter ?? null,
          active_page: activePage ?? null,
        }),
      })

      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let fullText = ''
      let gotPatch = false

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed || !trimmed.startsWith('data:')) continue
          const dataStr = trimmed.slice(5).trim()
          if (!dataStr) continue
          try {
            const data = JSON.parse(dataStr)
            if (data.type === 'token' && typeof data.text === 'string') {
              fullText += data.text
              setMessages((m) => {
                const copy = [...m]
                if (copy[aiIndex] && copy[aiIndex].role === 'ai') {
                  copy[aiIndex] = { ...copy[aiIndex], content: fullText }
                } else if (copy.length === aiIndex) {
                  copy.push({ role: 'ai', content: fullText })
                } else {
                  // fallback: ensure ai message exists
                  copy[aiIndex] = { role: 'ai', content: fullText }
                }
                return copy
              })
            } else if (data.type === 'patch' && data.patch) {
              gotPatch = true
              onGenerated(data.patch as Partial<CharacterDraft>)
              if (!fullText) {
                setMessages((m) => {
                  const copy = [...m]
                  if (copy[aiIndex] && copy[aiIndex].role === 'ai') {
                    copy[aiIndex] = { ...copy[aiIndex], content: 'Draft applied to the form → review the steps and hit Create when ready.' }
                  } else if (copy.length === aiIndex) {
                    copy.push({ role: 'ai', content: 'Draft applied to the form → review the steps and hit Create when ready.' })
                  }
                  return copy
                })
              }
            } else if (data.type === 'done') {
              // no-op
            } else if (data.error) {
              throw new Error(data.error)
            }
          } catch {
            // ignore parse errors for partial json
          }
        }
      }

      if (!fullText) {
        setMessages((m) => {
          const copy = [...m]
          if (copy[aiIndex] && copy[aiIndex].role === 'ai') {
            copy[aiIndex] = { ...copy[aiIndex], content: "Hmm, I didn't get a response — try again?" }
          } else if (copy.length === aiIndex) {
            copy.push({ role: 'ai', content: "Hmm, I didn't get a response — try again?" })
          }
          return copy
        })
      } else if (!gotPatch) {
        // still show completion; patch will be empty but message is there
      }
    } catch {
      try {
        const draft = mockGenerate(text)
        onGenerated(draft)
        setMessages((m) => {
          const copy = [...m]
          const msg = "Draft applied to the form → review the 5 steps on the right and hit Create when you're happy. Want to adjust anything? Just tell me. (offline mock)"
          if (copy[aiIndex] && copy[aiIndex].role === 'ai') copy[aiIndex] = { ...copy[aiIndex], content: msg }
          else if (copy.length === aiIndex) copy.push({ role: 'ai', content: msg })
          else copy[aiIndex] = { role: 'ai', content: msg }
          return copy
        })
      } catch {
        setMessages((m) => {
          const copy = [...m]
          const msg = "Hmm, I had trouble with that — try rephrasing your idea."
          if (copy[aiIndex] && copy[aiIndex].role === 'ai') copy[aiIndex] = { ...copy[aiIndex], content: msg }
          else if (copy.length === aiIndex) copy.push({ role: 'ai', content: msg })
          else copy[aiIndex] = { role: 'ai', content: msg }
          return copy
        })
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="character-ai-chat">
      <div className="character-ai-chat__messages">
        {messages.map((msg, i) => (
          <div key={i} className={`character-ai-chat__bubble is-${msg.role}`}>
            <span className="character-ai-chat__bubble-role">{msg.role === 'ai' ? 'AI' : 'You'}</span>
            {msg.role === 'ai' ? <MarkdownContent content={msg.content} /> : <p>{msg.content}</p>}
          </div>
        ))}
        {loading && messages[messages.length - 1]?.role !== 'ai' && <div className="character-ai-chat__bubble is-ai is-typing"><span>AI</span><p>Drafting…</p></div>}
      </div>

      <div className="character-ai-chat__composer">
        <div className="character-ai-chat__input-wrap">
          <textarea
            rows={1}
            placeholder="Describe your character…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
            disabled={loading}
          />
          <button
            type="button"
            className="character-ai-chat__send"
            onClick={handleSend}
            disabled={loading || !input.trim()}
            aria-label="Send"
          >
            <i className="bi bi-arrow-up" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  )
}

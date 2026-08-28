'use client'

import { useState } from 'react'
import Button from '@/components/common/Button'
import type { CharacterDraft } from './characterFormConfig'

interface Props {
  onGenerated: (draft: Partial<CharacterDraft>) => void
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

export default function CharacterAIAssist({ onGenerated }: Props) {
  const [messages, setMessages] = useState<ChatMsg[]>([
    { role: 'ai', content: "Hey! I can help you build your D&D 5e sheet — just describe who you imagine (e.g. 'grumpy dwarf cleric who loves ale' or 'shy half-elf druid, level 2'). I'll draft the stats and you can tweak everything before saving." },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSend = async () => {
    const text = input.trim()
    if (!text || loading) return
    setInput('')
    setMessages((m) => [...m, { role: 'user', content: text }])
    setLoading(true)
    await new Promise((r) => setTimeout(r, 800))
    try {
      const draft = mockGenerate(text)
      onGenerated(draft)
      setMessages((m) => [...m, { role: 'ai', content: "Draft applied to the form → review the 5 steps on the right and hit Create when you're happy. Want to adjust anything? Just tell me." }])
    } catch {
      setMessages((m) => [...m, { role: 'ai', content: "Hmm, I had trouble with that — try rephrasing your idea." }])
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
            <p>{msg.content}</p>
          </div>
        ))}
        {loading && <div className="character-ai-chat__bubble is-ai is-typing"><span>AI</span><p>Drafting…</p></div>}
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

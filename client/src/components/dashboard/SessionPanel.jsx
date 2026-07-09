import { memo, useState, useRef, useLayoutEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import MarkdownContent from '../common/MarkdownContent'
import DiceRollStage from './DiceRollStage'
import SessionInput from './SessionInput'
import SheetProposalInline from '../session/SheetProposalInline'
import { formatMessageForDm, hasIcSegment, parseTaggedMessage } from '../../utils/messageTags'

import { parseDate } from '../../utils/date'

function formatTime(iso) {
  if (!iso) return ''
  const d = parseDate(iso)
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function rollDie(sides, modifier = 0, rollMode = 'normal') {
  if (rollMode === 'advantage') {
    const r1 = Math.floor(Math.random() * sides) + 1
    const r2 = Math.floor(Math.random() * sides) + 1
    const result = Math.max(r1, r2)
    return { result, rolls: [r1, r2], total: result + modifier }
  }
  if (rollMode === 'disadvantage') {
    const r1 = Math.floor(Math.random() * sides) + 1
    const r2 = Math.floor(Math.random() * sides) + 1
    const result = Math.min(r1, r2)
    return { result, rolls: [r1, r2], total: result + modifier }
  }
  const r = Math.floor(Math.random() * sides) + 1
  return { result: r, rolls: [r], total: r + modifier }
}

const SKILL_ABILITIES = {
  Athletics: 'strength',
  Acrobatics: 'dexterity',
  'Sleight of Hand': 'dexterity',
  Stealth: 'dexterity',
  Arcana: 'intelligence',
  History: 'intelligence',
  Investigation: 'intelligence',
  Nature: 'intelligence',
  Religion: 'intelligence',
  'Animal Handling': 'wisdom',
  Insight: 'wisdom',
  Medicine: 'wisdom',
  Perception: 'wisdom',
  Survival: 'wisdom',
  Deception: 'charisma',
  Intimidation: 'charisma',
  Performance: 'charisma',
  Persuasion: 'charisma',
}

const STANDARD_SKILLS = [
  'Acrobatics', 'Animal Handling', 'Arcana', 'Athletics', 'Deception',
  'History', 'Insight', 'Intimidation', 'Investigation', 'Medicine',
  'Nature', 'Perception', 'Performance', 'Persuasion', 'Religion',
  'Sleight of Hand', 'Stealth', 'Survival'
]

function getAbilityModifier(score) {
  if (score === undefined || score === null) return 0
  return Math.floor((score - 10) / 2)
}

function getSavingThrowModifier(character, ability) {
  const save = (character.saving_throws || []).find(
    (s) => s.ability?.toLowerCase() === ability.toLowerCase()
  )
  if (save && save.bonus_override !== null && save.bonus_override !== undefined && save.bonus_override !== '') {
    return parseInt(save.bonus_override, 10)
  }
  const score = character.ability_scores?.[ability.toLowerCase()] ?? 10
  const baseMod = getAbilityModifier(score)
  const isProf = save ? save.is_proficient : false
  const profBonus = character.general?.proficiency_bonus ?? 2
  return baseMod + (isProf ? profBonus : 0)
}

function getSkillModifier(character, skillName) {
  const skill = (character.skills || []).find(
    (s) => s.skill_name?.toLowerCase() === skillName.toLowerCase()
  )
  if (skill && skill.bonus_override !== null && skill.bonus_override !== undefined && skill.bonus_override !== '') {
    return parseInt(skill.bonus_override, 10)
  }
  const ability = SKILL_ABILITIES[skillName] || 'wisdom'
  const score = character.ability_scores?.[ability] ?? 10
  const baseMod = getAbilityModifier(score)
  const isProf = skill ? skill.is_proficient : false
  const isExp = skill ? skill.is_expertise : false
  const profBonus = character.general?.proficiency_bonus ?? 2
  let mod = baseMod
  if (isExp) {
    mod += profBonus * 2
  } else if (isProf) {
    mod += profBonus
  }
  return mod
}

function getInitiativeModifier(character) {
  return character.combat?.initiative_bonus ?? 0
}

function parseDiceString(str) {
  const clean = str.replace(/\s+/g, '')
  const match = clean.match(/^(\d+)d(\d+)(?:([+-])(\d+))?$/i)
  if (!match) {
    return { num: 1, sides: 6, mod: 0 }
  }
  const num = parseInt(match[1], 10)
  const sides = parseInt(match[2], 10)
  let mod = 0
  if (match[3] && match[4]) {
    const sign = match[3] === '+' ? 1 : -1
    mod = sign * parseInt(match[4], 10)
  }
  return { num, sides, mod }
}

function rollDiceString(str) {
  const { num, sides, mod } = parseDiceString(str)
  let total = 0
  const rolls = []
  for (let i = 0; i < num; i++) {
    const r = Math.floor(Math.random() * sides) + 1
    rolls.push(r)
    total += r
  }
  return { rolls, result: total, total: total + mod, modifier: mod, sides }
}

const DICE = [4, 6, 8, 10, 12, 20, 100]

function formatRollSummary(roll) {
  if (!roll) return ''
  const kept = roll.rolls.length > 1 ? ` (keep ${roll.result})` : ''
  const modifier = roll.modifier ? ` ${roll.modifier > 0 ? '+' : '-'} ${Math.abs(roll.modifier)}` : ''
  return `${roll.label}: ${roll.rolls.join(', ')}${kept}${modifier} = ${roll.total}`
}

function getMessageSenderLabel(msg, currentUser) {
  if (msg.role === 'dm') return 'DM'
  if (msg.role === 'system') return 'System'
  if (msg.user_id && currentUser?.id && msg.user_id === currentUser.id) return 'You'
  return msg.username || 'Player'
}

const rollRegex = /^\[Roll:\s*([^\]]+)\]\s*total:\s*(-?\d+)\s*\|\s*rolls:\s*([\d,\s]+)\s*\|\s*mod:\s*(-?\d+)\s*\|\s*sides:\s*(\d+)/i

function RollCard({ label, total, rolls, modifier, sides }) {
  const isD20 = sides === 20
  let isCritHit = false
  let isCritMiss = false

  if (isD20) {
    const chosenRoll = total - modifier
    if (chosenRoll === 20) isCritHit = true
    if (chosenRoll === 1) isCritMiss = true
  }

  const statusClass = isCritHit ? 'crit-hit' : isCritMiss ? 'crit-miss' : ''
  const displayMod = modifier > 0 ? `+${modifier}` : modifier < 0 ? `${modifier}` : ''

  const rollDetails = rolls.join(', ')
  const isAdv = label.includes('(Advantage)')
  const isDis = label.includes('(Disadvantage)')

  let modeStr = ''
  if (isAdv) modeStr = ' (Adv)'
  if (isDis) modeStr = ' (Dis)'

  const formula = `${isD20 ? (rolls.length > 1 ? '2' : '1') : rolls.length}d${sides}${modeStr}`

  return (
    <div className={`roll-card ${statusClass}`}>
      <div className="roll-card-header">
        <span className="roll-card-icon"><i className="bi bi-dice-5-fill"></i></span>
        <span className="roll-card-title">{label}</span>
      </div>
      <div className="roll-card-body">
        <div className="roll-card-result">
          <span className="roll-card-total">{total}</span>
          {isCritHit && <span className="roll-card-badge crit-hit-badge">CRIT!</span>}
          {isCritMiss && <span className="roll-card-badge crit-miss-badge">FAIL!</span>}
        </div>
        <div className="roll-card-breakdown">
          <span className="roll-card-formula">{formula}</span>
          <span className="roll-card-rolls">[{rollDetails}]</span>
          {displayMod && <span className="roll-card-mod">{displayMod} modifier</span>}
        </div>
      </div>
    </div>
  )
}

function ComboRollCard({ rollsList }) {
  const grandTotal = rollsList.reduce((sum, r) => sum + r.total, 0)
  
  return (
    <div className="roll-card combo-roll-card" style={{ borderLeft: '3px solid var(--color-primary)' }}>
      <div className="roll-card-header" style={{ borderBottom: '1px solid var(--border-color)', paddingBottom: '8px', marginBottom: '8px', display: 'flex', alignItems: 'center' }}>
        <span className="roll-card-icon"><i className="bi bi-dice-5-fill"></i></span>
        <span className="roll-card-title" style={{ color: 'var(--text-gold)', fontWeight: 'bold' }}>Combo Dice Roll</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.75rem', color: 'var(--text-dim)' }}>{rollsList.length} rolls</span>
      </div>
      <div className="combo-rolls-list" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {rollsList.map((roll, idx) => {
          const isD20 = roll.sides === 20
          let isCritHit = false
          let isCritMiss = false
          if (isD20) {
            const chosenRoll = roll.total - roll.modifier
            if (chosenRoll === 20) isCritHit = true
            if (chosenRoll === 1) isCritMiss = true
          }
          const displayMod = roll.modifier > 0 ? `+${roll.modifier}` : roll.modifier < 0 ? `${roll.modifier}` : ''
          const isAdv = roll.label.includes('(Advantage)')
          const isDis = roll.label.includes('(Disadvantage)')
          let modeStr = ''
          if (isAdv) modeStr = ' (Adv)'
          if (isDis) modeStr = ' (Dis)'
          const formula = `${isD20 ? (roll.rolls.length > 1 ? '2' : '1') : roll.rolls.length}d${roll.sides}${modeStr}`

          return (
            <div key={idx} className="combo-roll-item" style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '6px 10px',
              background: 'rgba(255,255,255,0.02)',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--border-color)',
              gap: '12px'
            }}>
              <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                <span style={{ fontSize: '0.85rem', fontWeight: 'bold', color: 'var(--text-bright)', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }}>
                  {roll.label}
                </span>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>
                  {formula} [{roll.rolls.join(', ')}] {displayMod && ` ${displayMod} modifier`}
                </span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                {isCritHit && <span className="roll-card-badge crit-hit-badge" style={{ fontSize: '0.65rem', padding: '2px 4px' }}>CRIT!</span>}
                {isCritMiss && <span className="roll-card-badge crit-miss-badge" style={{ fontSize: '0.65rem', padding: '2px 4px' }}>FAIL!</span>}
                <strong style={{ fontSize: '1.2rem', color: 'var(--text-bright)', fontFamily: 'var(--heading)' }}>{roll.total}</strong>
              </div>
            </div>
          )
        })}
      </div>
      <div className="combo-roll-footer" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '10px', borderTop: '1px solid var(--border-color)', paddingTop: '8px' }}>
        <span style={{ fontSize: '0.85rem', color: 'var(--text-dim)' }}>Grand Total:</span>
        <strong style={{ fontSize: '1.35rem', color: 'var(--color-primary)', fontFamily: 'var(--heading)' }}>{grandTotal}</strong>
      </div>
    </div>
  )
}

function TurnEndedCard({ characterName }) {
  return (
    <div className="turn-ended-card">
      <div className="turn-ended-card-icon">
        <i className="bi bi-hourglass-bottom"></i>
      </div>
      <div className="turn-ended-card-content">
        <div className="turn-ended-card-title">Turn Status</div>
        <div className="turn-ended-card-body">
          <strong>{characterName}</strong> ended their turn.
        </div>
      </div>
    </div>
  )
}

function PlayerMessageContent({ content, charName }) {
  const segments = parseTaggedMessage(content)

  return (
    <div className="session-player-tagged-content">
      {segments.map((segment, index) => {
        const text = segment.text.trim()
        if (!text) return null

        if (segment.type === 'ic') {
          return (
            <div key={`ic-${index}`} className="session-ic-message">
              <div className="session-ic-banner">
                <strong>{charName}</strong>
              </div>
              <div className="session-ic-text">{text}</div>
            </div>
          )
        }

        if (segment.type === 'ooc') {
          const lines = text.split('\n')
          const rollsList = []
          const nonRollLines = []

          lines.forEach((line) => {
            const trimmedLine = line.trim()
            if (!trimmedLine) return

            if (trimmedLine.startsWith('[Turn Ended]')) {
              nonRollLines.push({ type: 'turn_ended', name: trimmedLine.replace('[Turn Ended]', '').trim() || 'Player' })
              return
            }

            const match = trimmedLine.match(rollRegex)
            if (match) {
              rollsList.push({
                label: match[1],
                total: parseInt(match[2], 10),
                rolls: match[3].split(',').map((r) => parseInt(r.trim(), 10)),
                modifier: parseInt(match[4], 10),
                sides: parseInt(match[5], 10)
              })
            } else {
              nonRollLines.push({ type: 'text', content: line })
            }
          })

          const renderedRoll = (() => {
            if (rollsList.length === 0) return null
            if (rollsList.length === 1) {
              const r = rollsList[0]
              return (
                <RollCard
                  key={`roll-${index}`}
                  label={r.label}
                  total={r.total}
                  rolls={r.rolls}
                  modifier={r.modifier}
                  sides={r.sides}
                />
              )
            }
            return (
              <ComboRollCard
                key={`combo-${index}`}
                rollsList={rollsList}
              />
            )
          })()

          return (
            <div key={`ooc-group-${index}`} style={{ display: 'flex', flexDirection: 'column', gap: '8px', width: '100%' }}>
              {renderedRoll}
              {nonRollLines.map((line, lineIdx) => {
                if (line.type === 'turn_ended') {
                  return (
                    <TurnEndedCard
                      key={`turn-ended-${index}-${lineIdx}`}
                      characterName={line.name}
                    />
                  )
                }
                return (
                  <div key={`ooc-${index}-${lineIdx}`} className="session-ooc-message">
                    <span>{line.content}</span>
                  </div>
                )
              })}
            </div>
          )
        }
      })}
    </div>
  )
}

function DMMessageContent({ content }) {
  const segments = parseTaggedMessage(content)

  return (
    <div className="session-dm-tagged-content">
      {segments.map((segment, index) => {
        const text = segment.text.trim()
        if (!text) return null

        if (segment.type === 'npc') {
          return (
            <div key={`npc-${index}`} className="session-npc-message">
              <div className="session-npc-banner">
                <span><i className="bi bi-person-badge-fill"></i> NPC</span>
                <strong>{segment.target}</strong>
              </div>
              <div className="session-npc-text">
                <MarkdownContent content={text} />
              </div>
            </div>
          )
        }

        return (
          <div key={`dm-${index}`} className="session-dm-narration">
            <MarkdownContent content={text} />
          </div>
        )
      })}
    </div>
  )
}

function getGradientSeed(str) {
  if (!str) return 'linear-gradient(135deg, #475569, #1e293b)'
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

function getInitials(name) {
  if (!name) return '?'
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

const SessionMessageItem = memo(function SessionMessageItem({
  msg,
  currentUser,
  sessionId,
  onProposalApplied,
  onProposalDismissed,
  characters = [],
}) {
  const isIc = msg.role === 'player' && hasIcSegment(msg.content || '')
  const senderLabel = getMessageSenderLabel(msg, currentUser)
  const initials = getInitials(msg.username || senderLabel)
  const gradient = getGradientSeed(msg.username || senderLabel)

  const char = characters.find((c) => c.user_id === msg.user_id)
  const charName = char ? char.name : (msg.username || 'Player')

  return (
    <div className={`session-msg session-msg-${msg.role} ${isIc ? 'is-ic' : ''}`}>
      {msg.is_proposal ? (
        <SheetProposalInline
          proposal={msg.proposal}
          sessionId={sessionId}
          currentUser={currentUser}
          onApplied={onProposalApplied}
          onDismissed={onProposalDismissed}
        />
      ) : (
        <div className="session-msg-layout">
          <div className="session-msg-avatar-wrapper">
            {msg.role === 'dm' ? (
              <div className="session-msg-avatar dm-avatar">
                <i className="bi bi-person-fill-check"></i>
              </div>
            ) : msg.role === 'system' ? (
              <div className="session-msg-avatar system-avatar">
                <i className="bi bi-gear-fill"></i>
              </div>
            ) : (
              <div className="session-msg-avatar player-avatar" style={{ background: gradient }}>
                {initials}
              </div>
            )}
          </div>

          <div className="session-msg-body">
            <div className="session-msg-header">
              <span className="session-msg-username">{senderLabel}</span>
              <span className="session-msg-time">{formatTime(msg.created_at)}</span>
            </div>
            <div className={`session-msg-content ${msg.role === 'player' ? 'session-msg-content-tagged' : ''}`}>
              {msg.role === 'dm' ? (
                <DMMessageContent content={msg.content} />
              ) : msg.role === 'player' ? (
                <PlayerMessageContent content={msg.content} charName={charName} />
              ) : (
                msg.content
              )}
            </div>
          </div>

          {isIc && (
            <div className="session-msg-ic-badge-container">
              <span className="ic-badge">IC</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
})

export default function SessionPanel({
  session,
  messages,
  currentUser,
  currentCharacter,
  canSendMessage = true,
  readOnlyReason = '',
  characters = [],
  onStartSession,
  onEndSession,
  onSendMessage,
  hasOlderMessages = false,
  loadingOlderMessages = false,
  onLoadOlderMessages,
  aiThinking,
  aiThinkingStatus,
  onProposalApplied,
  onProposalDismissed,
  onToggleLootStash,
  onToggleShops,
  encounterMap,
}) {
  const [input, setInput] = useState('')
  const [modifier, setModifier] = useState(0)
  const [rollMode, setRollMode] = useState('normal')
  const [customLabel, setCustomLabel] = useState('')
  const [activeTab, setActiveTab] = useState('custom')
  const [showDice, setShowDice] = useState(false)
  const [lastRoll, setLastRoll] = useState(null)
  const [rollQueue, setRollQueue] = useState([])
  const [activeChatTab, setActiveChatTab] = useState('chat')
  const messagesContainerRef = useRef(null)
  const messagesEndRef = useRef(null)
  const olderLoadScrollRef = useRef(null)
  const previousMessageCountRef = useRef(0)
  const rollIdRef = useRef(0)

  const placements = encounterMap?.placements || []
  const groupedPlacements = useMemo(() => {
    const groups = { player: [], npc: [], monster: [] }
    placements.forEach((p) => {
      if (groups[p.actor_type]) {
        groups[p.actor_type].push(p)
      }
    })
    return groups
  }, [placements])

  function getGridCoordinate(col, row) {
    if (typeof col !== 'number' || typeof row !== 'number') return ''
    const letter = String.fromCharCode(65 + (col % 26))
    const prefix = col >= 26 ? String.fromCharCode(65 + Math.floor(col / 26) - 1) : ''
    return `${prefix}${letter}-${row + 1}`
  }

  const renderSidebarRosterCard = (placement) => {
    return (
      <div
        key={placement.id}
        className={`encounter-combatant-card ${placement.actor_type}`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          padding: '8px 12px',
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid var(--border-color)',
          borderRadius: 'var(--radius-md)',
        }}
      >
        <span className="combatant-avatar" style={{
          width: '28px',
          height: '28px',
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '0.75rem',
          fontWeight: 'bold',
          background: placement.actor_type === 'player' ? 'var(--color-primary-glow)' : placement.actor_type === 'monster' ? 'rgba(220, 38, 38, 0.2)' : 'rgba(255, 255, 255, 0.05)',
          border: '1px solid ' + (placement.actor_type === 'player' ? 'var(--color-primary)' : placement.actor_type === 'monster' ? 'var(--color-danger)' : 'var(--border-color)'),
          color: 'var(--text-bright)'
        }}>
          {placement.label?.slice(0, 2).toUpperCase() || '?'}
        </span>
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
          <strong style={{ fontSize: '0.85rem', color: 'var(--text-bright)', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }}>
            {placement.label}
          </strong>
          <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>Actor ID: {placement.actor_id}</span>
        </div>
        <span className="combatant-coord-badge" style={{
          fontSize: '0.75rem',
          background: 'var(--bg-input)',
          border: '1px solid var(--border-color)',
          color: 'var(--text-gold)',
          padding: '2px 6px',
          borderRadius: '4px',
          fontFamily: 'var(--mono)',
        }}>
          {getGridCoordinate(placement.col, placement.row)}
        </span>
      </div>
    )
  }

  const renderSidebarRoster = () => {
    return (
      <div className="session-roster-tab" style={{ padding: '4px 0', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {groupedPlacements.player.length > 0 && (
          <div className="roster-sidebar-group">
            <h4 style={{ margin: '0 0 8px', fontSize: '0.75rem', fontWeight: 'bold', textTransform: 'uppercase', color: 'var(--text-gold)', letterSpacing: '0.5px' }}>🛡️ Party</h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {groupedPlacements.player.map(renderSidebarRosterCard)}
            </div>
          </div>
        )}

        {groupedPlacements.npc.length > 0 && (
          <div className="roster-sidebar-group">
            <h4 style={{ margin: '0 0 8px', fontSize: '0.75rem', fontWeight: 'bold', textTransform: 'uppercase', color: 'var(--text-gold)', letterSpacing: '0.5px' }}>🤝 Allies</h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {groupedPlacements.npc.map(renderSidebarRosterCard)}
            </div>
          </div>
        )}

        {groupedPlacements.monster.length > 0 && (
          <div className="roster-sidebar-group">
            <h4 style={{ margin: '0 0 8px', fontSize: '0.75rem', fontWeight: 'bold', textTransform: 'uppercase', color: 'var(--text-gold)', letterSpacing: '0.5px' }}>⚔️ Threats</h4>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {groupedPlacements.monster.map(renderSidebarRosterCard)}
            </div>
          </div>
        )}
      </div>
    )
  }

  const filteredMessages = useMemo(() => {
    return messages.filter(() => {
      // TBD or Roster tab always contains 0 chats
      if (activeChatTab === 'tbd' || activeChatTab === 'roster') {
        return false;
      }
      
      // Chat tab (default) contains all chats
      return true;
    });
  }, [messages, activeChatTab])

  const [activeSlashCommand, setActiveSlashCommand] = useState(null)
  const [physicalLabel, setPhysicalLabel] = useState('')
  const [physicalSides, setPhysicalSides] = useState(20)
  const [physicalRolls, setPhysicalRolls] = useState('')
  const [physicalModifier, setPhysicalModifier] = useState(0)
  const [physicalTotal, setPhysicalTotal] = useState(0)

  const cancelSlashCommand = () => {
    setActiveSlashCommand(null)
    setPhysicalLabel('')
    setPhysicalSides(20)
    setPhysicalRolls('')
    setPhysicalModifier(0)
    setPhysicalTotal(0)
  }

  const submitPhysicalRoll = () => {
    const rolls = physicalRolls
      .split(',')
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => !Number.isNaN(n))

    if (rolls.length === 0) return

    const totalVal = parseInt(physicalTotal, 10) || 0
    const sidesVal = parseInt(physicalSides, 10) || 20
    const modifierVal = parseInt(physicalModifier, 10) || 0
    const msg = `[Roll: ${physicalLabel || 'Physical Roll'}] total: ${totalVal} | rolls: ${rolls.join(',')} | mod: ${modifierVal} | sides: ${sidesVal}`

    onSendMessage(formatMessageForDm(msg))
    cancelSlashCommand()
  }

  const updatePhysicalRolls = (value) => {
    setPhysicalRolls(value)
    const rolls = value
      .split(',')
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => !Number.isNaN(n))
    const sum = rolls.reduce((a, b) => a + b, 0)
    setPhysicalTotal(sum + (parseInt(physicalModifier, 10) || 0))
  }

  const updatePhysicalModifier = (value) => {
    const modifierValue = parseInt(value, 10) || 0
    setPhysicalModifier(modifierValue)
    const rolls = physicalRolls
      .split(',')
      .map((s) => parseInt(s.trim(), 10))
      .filter((n) => !Number.isNaN(n))
    const sum = rolls.reduce((a, b) => a + b, 0)
    setPhysicalTotal(sum + modifierValue)
  }

  const handleSlashKeyDown = (e) => {
    if (!activeSlashCommand) return

    if (e.key === 'Escape') {
      cancelSlashCommand()
      e.preventDefault()
    } else if (e.key === 'Enter') {
      if (e.target.closest?.('button')) return
      submitPhysicalRoll()
      e.preventDefault()
    }
  }


  const uniqueSkills = useMemo(() => {
    if (!currentCharacter) return []
    return Array.from(new Set([
      ...STANDARD_SKILLS,
      ...(currentCharacter.skills || []).map(s => s.skill_name).filter(Boolean)
    ]))
  }, [currentCharacter])

  useLayoutEffect(() => {
    const container = messagesContainerRef.current
    const olderLoadScroll = olderLoadScrollRef.current
    if (container && olderLoadScroll) {
      container.scrollTop = container.scrollHeight - olderLoadScroll.previousScrollHeight + olderLoadScroll.previousScrollTop
      olderLoadScrollRef.current = null
      previousMessageCountRef.current = messages.length
      return
    }

    const previousCount = previousMessageCountRef.current
    previousMessageCountRef.current = messages.length
    if (!container) return

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

  const handleSend = () => {
    if (!canSendMessage) return
    if (activeSlashCommand === 'roll') {
      submitPhysicalRoll()
      return
    }

    // Clean zero-width space characters and convert non-breaking space to standard space
    const text = input
      .replace(/\u200b|\u200c|\u200d|\ufeff/g, '')
      .replace(/\u00a0/g, ' ')
      .trim()
    if (!text) return

    const lowerText = text.toLowerCase()

    if (lowerText === '/roll') {
      setActiveSlashCommand('roll')
      setInput('')
      return
    }

    if (lowerText.startsWith('/roll ')) {
      const match = text.match(/\/roll\s*d(\d+)(?:\s*([+-]\s*\d+))?/i)
      if (match) {
        const sides = parseInt(match[1], 10)
        const mod = match[2] ? parseInt(match[2].replace(/\s/g, ''), 10) : 0
        const { rolls, total, result } = rollDie(sides, mod, rollMode)
        recordLocalRoll({ sides, rolls, total, result, modifier: mod, label: `d${sides}` })
        setShowDice(true)
      }
      setInput('')
      return
    }

    if (lowerText.startsWith('/')) {
      const parts = text.slice(1).split(' ')
      const cmd = parts[0].toLowerCase()
      if (cmd === 'sheet' && parts[1]) {
        setInput(`/sheet ${parts.slice(1).join(' ')}`)
        return
      }
    }

    if (!session) {
      onStartSession().then(() => {
        setTimeout(() => onSendMessage(formatMessageForDm(text)), 100)
      })
      setInput('')
      return
    }

    onSendMessage(formatMessageForDm(text))
    setInput('')
  }

  const recordLocalRoll = (rollData) => {
    rollIdRef.current += 1
    const roll = {
      id: rollIdRef.current,
      sides: rollData.sides,
      rolls: rollData.rolls,
      total: rollData.total,
      result: rollData.result,
      modifier: rollData.modifier,
      label: rollData.label,
      rolledAt: new Date().toISOString(),
    }
    setLastRoll(roll)
    setRollQueue((prev) => [...prev, roll])
  }

  const handleRemoveRoll = (id) => {
    setRollQueue((prev) => prev.filter((r) => r.id !== id))
  }

  const postQueueToChat = () => {
    if (!canSendMessage) return
    if (rollQueue.length === 0) return
    const msg = rollQueue.map((roll) => {
      return `[Roll: ${roll.label}] total: ${roll.total} | rolls: ${roll.rolls.join(',')} | mod: ${roll.modifier} | sides: ${roll.sides}`
    }).join('\n')
    onSendMessage(formatMessageForDm(msg))
    setRollQueue([])
  }

  const handleCustomDieClick = (sides) => {
    const { rolls, total, result } = rollDie(sides, modifier, rollMode)
    const label = customLabel.trim() || `d${sides}`
    recordLocalRoll({ sides, rolls, total, result, modifier, label })
  }

  const handleSkillRoll = (skillName, mod) => {
    const { rolls, total, result } = rollDie(20, mod, rollMode)
    const suffix = rollMode === 'advantage' ? ' (Advantage)' : rollMode === 'disadvantage' ? ' (Disadvantage)' : ''
    recordLocalRoll({
      sides: 20,
      rolls,
      total,
      result,
      modifier: mod,
      label: `${skillName} check${suffix}`
    })
  }

  const handleSaveRoll = (ability, mod) => {
    const { rolls, total, result } = rollDie(20, mod, rollMode)
    const suffix = rollMode === 'advantage' ? ' (Advantage)' : rollMode === 'disadvantage' ? ' (Disadvantage)' : ''
    recordLocalRoll({
      sides: 20,
      rolls,
      total,
      result,
      modifier: mod,
      label: `${ability.charAt(0).toUpperCase() + ability.slice(1)} Save${suffix}`
    })
  }

  const handleInitiativeRoll = (mod) => {
    const { rolls, total, result } = rollDie(20, mod, rollMode)
    const suffix = rollMode === 'advantage' ? ' (Advantage)' : rollMode === 'disadvantage' ? ' (Disadvantage)' : ''
    recordLocalRoll({
      sides: 20,
      rolls,
      total,
      result,
      modifier: mod,
      label: `Initiative${suffix}`
    })
  }

  const handleWeaponAttackRoll = (weaponName, attackBonus) => {
    const { rolls, total, result } = rollDie(20, attackBonus, rollMode)
    const suffix = rollMode === 'advantage' ? ' (Advantage)' : rollMode === 'disadvantage' ? ' (Disadvantage)' : ''
    recordLocalRoll({
      sides: 20,
      rolls,
      total,
      result,
      modifier: attackBonus,
      label: `${weaponName} Attack${suffix}`
    })
  }

  const handleWeaponDamageRoll = (weaponName, damageStr) => {
    const { rolls, result, total, modifier, sides } = rollDiceString(damageStr)
    recordLocalRoll({
      sides,
      rolls,
      total,
      result,
      modifier,
      label: `${weaponName} Damage`
    })
  }

  const handleSpellAttackRoll = (attackBonus) => {
    const { rolls, total, result } = rollDie(20, attackBonus, rollMode)
    const suffix = rollMode === 'advantage' ? ' (Advantage)' : rollMode === 'disadvantage' ? ' (Disadvantage)' : ''
    recordLocalRoll({
      sides: 20,
      rolls,
      total,
      result,
      modifier: attackBonus,
      label: `Spell Attack${suffix}`
    })
  }

  const cleanedInput = input
    .replace(/\u200b|\u200c|\u200d|\ufeff/g, '')
    .replace(/\u00a0/g, ' ')
    .trim()

  const filteredSuggestions = [
    {
      trigger: '/roll',
      desc: 'Log a physical dice roll to chat',
      onClick: () => { setActiveSlashCommand('roll'); setInput(''); }
    },
    {
      trigger: '/roll d[sides] [+/- mod]',
      desc: 'Direct roll (e.g., /roll d20 + 5)',
      onClick: () => { setInput('/roll d20 + 5'); }
    },
    {
      trigger: '/sheet [request]',
      desc: 'Request character sheet update (e.g., add 10 gold)',
      onClick: () => { setInput('/sheet '); }
    }
  ].filter(item => item.trigger.startsWith(cleanedInput.toLowerCase()) || cleanedInput === '/')

  const showCommandsHelp = cleanedInput.startsWith('/') && !activeSlashCommand && !showDice && filteredSuggestions.length > 0
  const canSubmitPhysicalRoll = activeSlashCommand !== 'roll' || physicalRolls.trim().length > 0

  return (
    <div className="session-panel">
      {!session ? (
        <div className="session-idle">
          <div className="session-idle-icon"><i className="bi bi-dice-5-fill"></i></div>
          <h3>No Active Session</h3>
          <p>Start a new session to begin playing with the AI Dungeon Master.</p>
          <div className="session-idle-actions" style={{ display: 'flex', gap: '12px', justifyContent: 'center', marginTop: '12px', flexWrap: 'wrap' }}>
            <button className="btn btn-primary" onClick={onStartSession}>
              Start Session
            </button>
            {onToggleLootStash && (
              <button className="btn btn-secondary btn-mobile-loot" onClick={onToggleLootStash}>
                <i className="bi bi-box-seam"></i> Loot Stash
              </button>
            )}
          </div>
        </div>
      ) : (
        <>
          <div className="session-header">
            <span className="session-active-indicator" />
            <span className="session-status">Session Active</span>
            {onToggleShops && (
              <button className="btn btn-secondary small" onClick={onToggleShops} style={{ marginLeft: 'auto', marginRight: '8px' }}>
                <i className="bi bi-shop"></i> Local Shops
              </button>
            )}
            {onToggleLootStash && (
              <button className="btn btn-secondary small btn-mobile-loot" onClick={onToggleLootStash} style={{ marginLeft: onToggleShops ? '0' : 'auto', marginRight: '8px' }}>
                <i className="bi bi-box-seam"></i> Loot Stash
              </button>
            )}
            <button className="btn btn-secondary small" onClick={onEndSession} style={{ marginLeft: !onToggleShops && !onToggleLootStash ? 'auto' : '0' }}>
              End Session
            </button>
          </div>

          <div className="chat-tabs-header">
            <button className={`chat-tab-btn ${activeChatTab === 'chat' ? 'active' : ''}`} onClick={() => setActiveChatTab('chat')}>Chat</button>
            {encounterMap?.placements?.length > 0 && (
              <button className={`chat-tab-btn ${activeChatTab === 'roster' ? 'active' : ''}`} onClick={() => setActiveChatTab('roster')}>Roster</button>
            )}
            <button className={`chat-tab-btn ${activeChatTab === 'tbd' ? 'active' : ''}`} onClick={() => setActiveChatTab('tbd')}>TBD</button>
          </div>

          {activeChatTab === 'roster' ? (
            <div className="session-messages">
              {renderSidebarRoster()}
            </div>
          ) : (
            <div className="session-messages" ref={messagesContainerRef} onScroll={handleMessagesScroll}>
              {(hasOlderMessages || loadingOlderMessages) && (
                <div className="session-load-history">
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
              {filteredMessages.length === 0 && (
                <div className="session-empty-msg">
                  No messages to display in this tab.
                </div>
              )}
              {filteredMessages.map((msg) => (
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
                <div className="session-msg session-msg-dm">
                  <div className="session-msg-header">
                    <span className="session-msg-role"><i className="bi bi-mic-fill"></i> DM</span>
                  </div>
                  <div className="session-msg-content session-msg-thinking">
                    <div className="thinking-indicator-wrapper">
                      <span className="thinking-spinner"></span>
                      <span className="thinking-status-text">{aiThinkingStatus || "Thinking..."}</span>
                    </div>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          )}

          {showDice && (
            <div className="session-roll-bar">
              <>
                {createPortal(
                  <div className="dice-stage">
                    <DiceRollStage roll={lastRoll} />
                  </div>,
                  document.body
                )}
                <div className="dice-roller" role="group" aria-label="Dice roller">
                  <div className="dice-dock-top">
                    <div className="dice-stage-readout" aria-live="polite">
                      {lastRoll ? (
                        <>
                          <span className="dice-readout-label">{lastRoll.label}</span>
                          <strong>{lastRoll.result}</strong>
                          <span>{formatRollSummary(lastRoll)}</span>
                        </>
                      ) : (
                        <>
                          <span className="dice-readout-label">Ready</span>
                          <strong>d20</strong>
                          <span>Select a die.</span>
                        </>
                      )}
                    </div>
                    <button
                      className="btn dice-dock-close"
                      onClick={() => setShowDice(false)}
                      title="Close dice roller"
                      aria-label="Close dice roller"
                    >
                      <i className="bi bi-x-lg"></i>
                    </button>
                  </div>

                  {rollQueue.length > 0 && (
                    <div className="dice-queue-bar" style={{
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '10px',
                      background: 'rgba(255, 255, 255, 0.02)',
                      border: '1px solid rgba(255, 255, 255, 0.06)',
                      borderRadius: 'var(--radius-md)',
                      padding: '12px',
                      marginTop: '-4px',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <span style={{ fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: 'var(--text-gold)', fontWeight: 'bold' }}>
                          Dice Queue ({rollQueue.length})
                        </span>
                        <button
                          type="button"
                          className="btn-clear-queue"
                          onClick={() => setRollQueue([])}
                          style={{
                            background: 'none',
                            border: 'none',
                            color: 'var(--text-dim)',
                            cursor: 'pointer',
                            fontSize: '0.75rem',
                            textDecoration: 'underline',
                            padding: 0
                          }}
                        >
                          Clear All
                        </button>
                      </div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', alignItems: 'center' }}>
                        {rollQueue.map((roll, idx) => (
                          <div key={roll.id || idx} className="dice-queue-chip" style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '6px',
                            background: 'var(--bg-panel-elevated)',
                            border: '1px solid var(--border-color)',
                            borderRadius: '16px',
                            padding: '4px 10px',
                            fontSize: '0.8rem',
                            color: 'var(--text-bright)',
                            boxShadow: '0 2px 6px rgba(0,0,0,0.15)'
                          }}>
                            <span style={{ fontWeight: 'bold', color: 'var(--text-bright)' }}>{roll.result}</span>
                            <span style={{ color: 'var(--text-dim)', fontSize: '0.7rem' }}>({roll.label})</span>
                            <button
                              type="button"
                              onClick={() => handleRemoveRoll(roll.id)}
                              style={{
                                background: 'none',
                                border: 'none',
                                color: 'var(--text-dim)',
                                cursor: 'pointer',
                                padding: '0 0 0 4px',
                                display: 'inline-flex',
                                alignItems: 'center',
                                fontSize: '0.85rem'
                              }}
                              title="Remove"
                            >
                              <i className="bi bi-x" style={{ fontSize: '1rem', lineHeight: 1 }}></i>
                            </button>
                          </div>
                        ))}
                      </div>
                      <button
                        className="btn btn-primary btn-post-queue"
                        onClick={postQueueToChat}
                        style={{
                          width: '100%',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          gap: '8px',
                          padding: '8px 16px',
                          fontSize: '0.9rem',
                          fontWeight: 'bold',
                          marginTop: '4px',
                          background: 'var(--color-primary)',
                          borderColor: 'var(--color-primary)',
                          color: '#fff'
                        }}
                      >
                        <i className="bi bi-chat-text-fill"></i> Post Rolls
                      </button>
                    </div>
                  )}

                  {currentCharacter && (
                    <div className="dice-tabs">
                      <button
                        className={`btn btn-tab small ${activeTab === 'custom' ? 'active' : ''}`}
                        onClick={() => setActiveTab('custom')}
                      >
                        Custom
                      </button>
                      <button
                        className={`btn btn-tab small ${activeTab === 'skills' ? 'active' : ''}`}
                        onClick={() => setActiveTab('skills')}
                      >
                        Skills
                      </button>
                      <button
                        className={`btn btn-tab small ${activeTab === 'saves' ? 'active' : ''}`}
                        onClick={() => setActiveTab('saves')}
                      >
                        Saves
                      </button>
                      <button
                        className={`btn btn-tab small ${activeTab === 'combat' ? 'active' : ''}`}
                        onClick={() => setActiveTab('combat')}
                      >
                        Combat
                      </button>
                    </div>
                  )}

                  {activeTab === 'custom' || !currentCharacter ? (
                    <>
                      <div className="dice-grid">
                        {DICE.map((sides) => (
                          <button
                            key={sides}
                            className="btn btn-die"
                            onClick={() => handleCustomDieClick(sides)}
                            title={`d${sides}`}
                          >
                            d{sides}
                          </button>
                        ))}
                      </div>
                      <div className="dice-controls">
                        <label className="dice-modifier">
                          Label:
                          <input
                            type="text"
                            className="input dice-label-input"
                            value={customLabel}
                            onChange={(e) => setCustomLabel(e.target.value)}
                            placeholder="e.g. Athletics jump"
                            style={{ width: '130px', padding: '6px 10px' }}
                          />
                        </label>
                        <label className="dice-modifier">
                          Mod:
                          <input
                            type="number"
                            className="input dice-mod-input"
                            value={modifier}
                            onChange={(e) => setModifier(parseInt(e.target.value, 10) || 0)}
                          />
                        </label>
                        <label className="dice-modifier">
                          Mode:
                          <select
                            className="input dice-select"
                            value={rollMode}
                            onChange={(e) => setRollMode(e.target.value)}
                            style={{ padding: '5px 10px', background: 'var(--bg-input)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-bright)' }}
                          >
                            <option value="normal">Normal</option>
                            <option value="advantage">Advantage</option>
                            <option value="disadvantage">Disadvantage</option>
                          </select>
                        </label>
                      </div>
                    </>
                  ) : activeTab === 'skills' ? (
                    <>
                      <div className="dice-skills-grid">
                        {uniqueSkills.map((skillName) => {
                          const mod = getSkillModifier(currentCharacter, skillName);
                          const skillRec = (currentCharacter.skills || []).find(
                            s => s.skill_name?.toLowerCase() === skillName.toLowerCase()
                          );
                          const typeLabel = skillRec?.is_expertise ? ' (E)' : skillRec?.is_proficient ? ' (P)' : '';
                          const displayMod = mod >= 0 ? `+${mod}` : mod;
                          return (
                            <button
                              key={skillName}
                              className="btn btn-skill-roll"
                              onClick={() => handleSkillRoll(skillName, mod)}
                              title={`Roll ${skillName} (${SKILL_ABILITIES[skillName] || 'custom'})`}
                            >
                              <span>{skillName}{typeLabel}</span>
                              <span className="mod-val">{displayMod}</span>
                            </button>
                          );
                        })}
                      </div>
                      <div className="dice-controls">
                        <label className="dice-modifier">
                          Mode:
                          <select
                            className="input dice-select"
                            value={rollMode}
                            onChange={(e) => setRollMode(e.target.value)}
                            style={{ padding: '5px 10px', background: 'var(--bg-input)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-bright)' }}
                          >
                            <option value="normal">Normal</option>
                            <option value="advantage">Advantage</option>
                            <option value="disadvantage">Disadvantage</option>
                          </select>
                        </label>
                      </div>
                    </>
                  ) : activeTab === 'saves' ? (
                    <>
                      <div className="dice-saves-grid">
                        {['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma'].map((ability) => {
                          const mod = getSavingThrowModifier(currentCharacter, ability);
                          const saveRec = (currentCharacter.saving_throws || []).find(
                            s => s.ability?.toLowerCase() === ability.toLowerCase()
                          );
                          const typeLabel = saveRec?.is_proficient ? ' (P)' : '';
                          const displayMod = mod >= 0 ? `+${mod}` : mod;
                          const abilityName = ability.toUpperCase().slice(0, 3);
                          return (
                            <button
                              key={ability}
                              className="btn btn-save-roll"
                              onClick={() => handleSaveRoll(ability, mod)}
                              title={`Roll ${ability} saving throw`}
                            >
                              <span>{abilityName} Save{typeLabel}</span>
                              <span className="mod-val">{displayMod}</span>
                            </button>
                          );
                        })}
                      </div>
                      <div className="dice-controls">
                        <label className="dice-modifier">
                          Mode:
                          <select
                            className="input dice-select"
                            value={rollMode}
                            onChange={(e) => setRollMode(e.target.value)}
                            style={{ padding: '5px 10px', background: 'var(--bg-input)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-bright)' }}
                          >
                            <option value="normal">Normal</option>
                            <option value="advantage">Advantage</option>
                            <option value="disadvantage">Disadvantage</option>
                          </select>
                        </label>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="dice-combat-tab">
                        <div className="combat-roll-row">
                          <div>
                            <div className="combat-item-name">Initiative</div>
                            <div className="combat-item-meta">Dexterity-based turn order</div>
                          </div>
                          <div className="combat-roll-buttons">
                            <button
                              className="btn btn-primary small"
                              onClick={() => handleInitiativeRoll(getInitiativeModifier(currentCharacter))}
                            >
                              Roll Initiative ({getInitiativeModifier(currentCharacter) >= 0 ? `+${getInitiativeModifier(currentCharacter)}` : getInitiativeModifier(currentCharacter)})
                            </button>
                          </div>
                        </div>

                        {currentCharacter.weapons && currentCharacter.weapons.length > 0 ? (
                          currentCharacter.weapons.map((w, i) => (
                            <div key={i} className="combat-roll-row">
                              <div>
                                <div className="combat-item-name">{w.name} {w.is_equipped ? '⚔️' : ''}</div>
                                <div className="combat-item-meta">{w.damage} {w.damage_type} &bull; {w.properties}</div>
                              </div>
                              <div className="combat-roll-buttons">
                                <button
                                  className="btn btn-secondary small"
                                  onClick={() => handleWeaponAttackRoll(w.name, w.attack_bonus || 0)}
                                >
                                  Attack ({w.attack_bonus >= 0 ? `+${w.attack_bonus}` : w.attack_bonus})
                                </button>
                                {w.damage && (
                                  <button
                                    className="btn small"
                                    style={{ background: 'rgba(245, 158, 11, 0.15)', borderColor: 'rgba(245, 158, 11, 0.3)', color: 'var(--text-gold)' }}
                                    onClick={() => handleWeaponDamageRoll(w.name, w.damage)}
                                  >
                                    Damage ({w.damage})
                                  </button>
                                )}
                              </div>
                            </div>
                          ))
                        ) : (
                          <div className="empty-combat" style={{ padding: '8px 0', fontSize: '0.8rem', color: 'var(--text-dim)' }}>
                            No weapons defined on character sheet.
                          </div>
                        )}

                        {currentCharacter.spellcasting && (
                          <div className="combat-roll-row">
                            <div>
                              <div className="combat-item-name">Spellcasting Magic</div>
                              <div className="combat-item-meta">Save DC: {currentCharacter.spellcasting.spell_save_dc || 10}</div>
                            </div>
                            <div className="combat-roll-buttons">
                              <button
                                className="btn btn-primary small"
                                onClick={() => handleSpellAttackRoll(currentCharacter.spellcasting.spell_attack_bonus || 0)}
                              >
                                Spell Attack ({currentCharacter.spellcasting.spell_attack_bonus >= 0 ? `+${currentCharacter.spellcasting.spell_attack_bonus}` : currentCharacter.spellcasting.spell_attack_bonus || 0})
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                      <div className="dice-controls">
                        <label className="dice-modifier">
                          Mode:
                          <select
                            className="input dice-select"
                            value={rollMode}
                            onChange={(e) => setRollMode(e.target.value)}
                            style={{ padding: '5px 10px', background: 'var(--bg-input)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-sm)', color: 'var(--text-bright)' }}
                          >
                            <option value="normal">Normal</option>
                            <option value="advantage">Advantage</option>
                            <option value="disadvantage">Disadvantage</option>
                          </select>
                        </label>
                      </div>
                    </>
                  )}
                </div>
              </>
            </div>
          )}

          {showCommandsHelp && (
            <div className="session-command-suggestions">
              <div className="suggestion-header">Commands</div>
              {filteredSuggestions.map((suggestion) => (
                <button key={suggestion.trigger} type="button" className="suggestion-item" onClick={suggestion.onClick}>
                  <span className="suggestion-trigger">{suggestion.trigger}</span>
                  <span className="suggestion-desc">{suggestion.desc}</span>
                </button>
              ))}
            </div>
          )}

          <div className="session-input-area">
            <div className={`session-input-shell ${hasIcSegment(input) ? 'has-ic' : ''} ${activeSlashCommand ? 'has-command' : ''}`}>
              {activeSlashCommand === 'roll' ? (
                <div className="session-slash-inline" onKeyDown={handleSlashKeyDown}>
                  <div className="slash-inline-top-row">
                    <span className="slash-inline-command"><i className="bi bi-dice-5-fill"></i> /roll</span>
                    <span className="slash-inline-hint">Log a physical dice roll</span>
                    <button
                      type="button"
                      className="btn slash-inline-close"
                      onClick={cancelSlashCommand}
                      aria-label="Cancel slash command"
                      title="Cancel (Esc)"
                    >
                      <i className="bi bi-x-lg"></i>
                    </button>
                  </div>
                  <div className="slash-inline-fields">
                    <label className="slash-inline-field slash-inline-label-field">
                      <span>Label</span>
                      <input
                        type="text"
                        placeholder="Physical Roll"
                        value={physicalLabel}
                        onChange={(e) => setPhysicalLabel(e.target.value)}
                        aria-label="Roll label"
                      />
                    </label>
                    <label className="slash-inline-field slash-inline-die-field">
                      <span>Die</span>
                      <select
                        value={physicalSides}
                        onChange={(e) => setPhysicalSides(parseInt(e.target.value, 10))}
                        aria-label="Die type"
                      >
                        <option value="20">d20</option>
                        <option value="12">d12</option>
                        <option value="10">d10</option>
                        <option value="8">d8</option>
                        <option value="6">d6</option>
                        <option value="4">d4</option>
                        <option value="100">d100</option>
                      </select>
                    </label>
                    <label className="slash-inline-field slash-inline-rolls-field">
                      <span>Result</span>
                      <input
                        type="text"
                        placeholder="e.g. 14"
                        value={physicalRolls}
                        onChange={(e) => updatePhysicalRolls(e.target.value)}
                        autoFocus
                        aria-label="Natural rolls"
                      />
                    </label>
                    <label className="slash-inline-field slash-inline-number-field">
                      <span>Mod</span>
                      <input
                        type="number"
                        value={physicalModifier}
                        onChange={(e) => updatePhysicalModifier(e.target.value)}
                        aria-label="Roll modifier"
                      />
                    </label>
                    <label className="slash-inline-field slash-inline-total-field">
                      <span>Total</span>
                      <input
                        type="number"
                        value={physicalTotal}
                        onChange={(e) => setPhysicalTotal(parseInt(e.target.value, 10) || 0)}
                        aria-label="Roll total"
                        readOnly
                      />
                    </label>
                  </div>
                </div>
              ) : (
                canSendMessage ? (
                  <SessionInput
                    value={input}
                    onChange={setInput}
                    onSubmit={handleSend}
                    onKeyDown={handleSlashKeyDown}
                    disabled={aiThinking}
                    placeholder={aiThinking ? 'Waiting for DM...' : 'Message the table...'}
                  />
                ) : (
                  <div className="session-spectator-banner">
                    <i className="bi bi-eye"></i>
                    <span>{readOnlyReason || 'Read-only spectator mode.'}</span>
                  </div>
                )
              )}
            </div>
            <button
              className={`btn btn-roll-toggle ${showDice ? 'active' : ''}`}
              onClick={() => setShowDice(!showDice)}
              title="Toggle dice roller"
              aria-label="Toggle dice roller"
            >
              <i className="bi bi-dice-5-fill"></i>
            </button>
            <button
              className="btn btn-primary session-send-btn"
              onClick={handleSend}
              disabled={!canSendMessage || aiThinking || !canSubmitPhysicalRoll}
            >
              {aiThinking ? '...' : activeSlashCommand ? 'Post' : 'Send'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

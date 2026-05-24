import { useState, useRef, useLayoutEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import MarkdownContent from '../common/MarkdownContent'
import DiceRollStage from './DiceRollStage'
import SessionInput from './SessionInput'
import SheetProposalInline from '../session/SheetProposalInline'
import { formatMessageForDm, hasIcSegment, parseTaggedMessage } from '../../utils/messageTags'

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
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

function getMessageSenderIcon(role) {
  if (role === 'dm') return 'bi bi-mic-fill'
  if (role === 'system') return 'bi bi-gear-fill'
  return 'bi bi-person-fill'
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

function PlayerMessageContent({ content }) {
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
                <span><i className="bi bi-chat-quote-fill"></i> PC</span>
                <strong>In character</strong>
              </div>
              <div className="session-ic-text">{text}</div>
            </div>
          )
        }

        if (segment.type === 'ooc' && text.startsWith('[Turn Ended]')) {
          const name = text.replace('[Turn Ended]', '').trim() || 'Player'
          return (
            <TurnEndedCard
              key={`turn-ended-${index}`}
              characterName={name}
            />
          )
        }

        const match = text.match(rollRegex)
        if (segment.type === 'ooc' && match) {
          const label = match[1]
          const total = parseInt(match[2], 10)
          const rolls = match[3].split(',').map((r) => parseInt(r.trim(), 10))
          const modifier = parseInt(match[4], 10)
          const sides = parseInt(match[5], 10)

          return (
            <RollCard
              key={`roll-${index}`}
              label={label}
              total={total}
              rolls={rolls}
              modifier={modifier}
              sides={sides}
            />
          )
        }

        return (
          <div key={`ooc-${index}`} className="session-ooc-message">
            <span className="session-ooc-label">OOC</span>
            <span>{text}</span>
          </div>
        )
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

export default function SessionPanel({
  session,
  messages,
  currentUser,
  currentCharacter,
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
}) {
  const [input, setInput] = useState('')
  const [modifier, setModifier] = useState(0)
  const [rollMode, setRollMode] = useState('normal')
  const [customLabel, setCustomLabel] = useState('')
  const [autoPost, setAutoPost] = useState(false)
  const [activeTab, setActiveTab] = useState('custom')
  const [showDice, setShowDice] = useState(false)
  const [lastRoll, setLastRoll] = useState(null)
  const messagesContainerRef = useRef(null)
  const messagesEndRef = useRef(null)
  const olderLoadScrollRef = useRef(null)
  const previousMessageCountRef = useRef(0)
  const rollIdRef = useRef(0)

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
    if (!container || messages.length === previousCount) return

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

  const postRollToChat = (roll) => {
    if (!roll) return
    const msg = `[Roll: ${roll.label}] total: ${roll.total} | rolls: ${roll.rolls.join(',')} | mod: ${roll.modifier} | sides: ${roll.sides}`
    onSendMessage(formatMessageForDm(msg))
  }

  const handleSend = () => {
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
    if (autoPost) {
      postRollToChat(roll)
    }
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
            {messages.length === 0 && (
              <div className="session-empty-msg">
                The session has begun. Type an action or speak to the DM.
              </div>
            )}
            {messages.map((msg) => (
              <div key={msg.id} className={`session-msg session-msg-${msg.role}`}>
                {msg.is_proposal ? (
                  <SheetProposalInline
                    proposal={msg.proposal}
                    sessionId={session?.id}
                    currentUser={currentUser}
                    onApplied={onProposalApplied}
                    onDismissed={onProposalDismissed}
                  />
                ) : (
                  <>
                    <div className="session-msg-header">
                      <span className="session-msg-role">
                        <i className={getMessageSenderIcon(msg.role)}></i> {getMessageSenderLabel(msg, currentUser)}
                      </span>
                      <span className="session-msg-time">{formatTime(msg.created_at)}</span>
                    </div>
                    <div className={`session-msg-content ${msg.role === 'player' ? 'session-msg-content-tagged' : ''}`}>
                      {msg.role === 'dm' ? (
                        <DMMessageContent content={msg.content} />
                      ) : msg.role === 'player' ? (
                        <PlayerMessageContent content={msg.content} />
                      ) : (
                        msg.content
                      )}
                    </div>
                  </>
                )}
              </div>
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

          <div className="session-roll-bar">
            <button
              className={`btn btn-roll-toggle ${showDice ? 'active' : ''}`}
              onClick={() => setShowDice(!showDice)}
              title="Toggle dice roller"
              aria-label="Toggle dice roller"
            >
              <i className="bi bi-dice-5-fill"></i>
            </button>
            {showDice && (
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
                          <button
                            className="btn btn-secondary small btn-post-roll"
                            onClick={() => postRollToChat(lastRoll)}
                            title="Post roll to chat feed"
                          >
                            <i className="bi bi-chat-text-fill"></i> Post
                          </button>
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
                        <label className="dice-auto-post">
                          <input
                            type="checkbox"
                            checked={autoPost}
                            onChange={(e) => setAutoPost(e.target.checked)}
                          />
                          Auto-post
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
                        <label className="dice-auto-post">
                          <input
                            type="checkbox"
                            checked={autoPost}
                            onChange={(e) => setAutoPost(e.target.checked)}
                          />
                          Auto-post
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
                        <label className="dice-auto-post">
                          <input
                            type="checkbox"
                            checked={autoPost}
                            onChange={(e) => setAutoPost(e.target.checked)}
                          />
                          Auto-post
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
                        <label className="dice-auto-post">
                          <input
                            type="checkbox"
                            checked={autoPost}
                            onChange={(e) => setAutoPost(e.target.checked)}
                          />
                          Auto-post
                        </label>
                      </div>
                    </>
                  )}
                </div>
              </>
            )}
          </div>

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
                <SessionInput
                  value={input}
                  onChange={setInput}
                  onSubmit={handleSend}
                  onKeyDown={handleSlashKeyDown}
                  disabled={aiThinking}
                  placeholder={aiThinking ? 'Waiting for DM...' : 'Type your action. Wrap speech in quotes for IC.'}
                />
              )}
            </div>
            <button
              className="btn btn-primary session-send-btn"
              onClick={handleSend}
              disabled={aiThinking || !canSubmitPhysicalRoll}
            >
              {aiThinking ? '...' : activeSlashCommand ? 'Post' : 'Send'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

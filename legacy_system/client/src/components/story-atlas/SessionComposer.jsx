import { useState, useMemo, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'
import SessionInput from '../dashboard/SessionInput'
import DiceRollStage from '../dashboard/DiceRollStage'
import { formatMessageForDm, hasIcSegment } from '../../utils/messageTags'

// D&D helper functions (copied from SessionPanel)
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

export default function SessionComposer({
  active = true,
  currentUser,
  currentCharacter,
  canSendMessage = true,
  readOnlyReason = '',
  aiThinking = false,
  onSendMessage,
  session,
  onStartSession,
  input,
  setInput,
  showDice,
  setShowDice,
  lastRoll,
  setLastRoll,
  rollQueue,
  setRollQueue,
  activeTab,
  setActiveTab,
  modifier,
  setModifier,
  rollMode,
  setRollMode,
  customLabel,
  setCustomLabel,
  activeSlashCommand,
  setActiveSlashCommand,
  physicalLabel,
  setPhysicalLabel,
  physicalSides,
  setPhysicalSides,
  physicalRolls,
  setPhysicalRolls,
  physicalModifier,
  setPhysicalModifier,
  physicalTotal,
  setPhysicalTotal,
}) {
  const rollIdRef = useRef(0)

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

    onSendMessage(formatMessageForDm(msg), pendingRollRequest ? {
      roll_request_id: pendingRollRequest.request_id,
      roll_result: {
        label: physicalLabel || pendingRollRequest.label || 'Physical Roll',
        total: totalVal,
        rolls,
        modifier: modifierVal,
        sides: sidesVal,
      },
    } : {})
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

  const handleSend = () => {
    if (!canSendMessage) return
    if (activeSlashCommand === 'roll') {
      submitPhysicalRoll()
      return
    }

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

    if (!session && onStartSession) {
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
    const requestedRoll = rollQueue.length === 1 && pendingRollRequest ? rollQueue[0] : null
    onSendMessage(formatMessageForDm(msg), requestedRoll ? {
      roll_request_id: pendingRollRequest.request_id,
      roll_result: {
        label: requestedRoll.label,
        total: requestedRoll.total,
        rolls: requestedRoll.rolls,
        modifier: requestedRoll.modifier,
        sides: requestedRoll.sides,
      },
    } : {})
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
  const pendingRollRequest = session?.pending_roll_requests?.[0] || null

  const openRequestedRoll = () => {
    if (!pendingRollRequest) return
    setCustomLabel(pendingRollRequest.label || pendingRollRequest.ability_or_skill || 'Requested roll')
    setRollMode(pendingRollRequest.advantage_state || 'normal')
    setActiveTab('custom')
    setShowDice(true)
  }

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      {pendingRollRequest && (
        <div className="story-atlas-roll-request" role="status" style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px',
          padding: '10px 14px', borderTop: '1px solid var(--border-color)', background: 'rgba(205, 160, 78, 0.08)'
        }}>
          <div>
            <strong style={{ color: 'var(--text-gold)' }}>{pendingRollRequest.label}</strong>
            <div style={{ color: 'var(--text-dim)', fontSize: '0.8rem' }}>{pendingRollRequest.reason_public}</div>
          </div>
          <button type="button" className="btn btn-primary small" onClick={openRequestedRoll}>
            <i className="bi bi-dice-5-fill" /> Roll
          </button>
        </div>
      )}
      {showDice && active && (
        <div className="session-roll-bar" style={{ display: 'block' }}>
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
                  {['custom', 'skills', 'saves', 'combat'].map(tab => (
                    <button
                      key={tab}
                      className={`btn btn-tab small ${activeTab === tab ? 'active' : ''}`}
                      onClick={() => setActiveTab(tab)}
                      style={{ textTransform: 'capitalize' }}
                    >
                      {tab}
                    </button>
                  ))}
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
                        >
                          <span>{skillName}{typeLabel}</span>
                          <span className="mod-val">{displayMod}</span>
                        </button>
                      );
                    })}
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
                        >
                          <span>{abilityName} Save{typeLabel}</span>
                          <span className="mod-val">{displayMod}</span>
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : (
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
                          <div className="combat-item-meta">{w.damage} {w.damage_type}</div>
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
                      No weapons defined.
                    </div>
                  )}
                </div>
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
                <span className="slash-inline-hint">Log physical roll</span>
                <button type="button" className="btn slash-inline-close" onClick={cancelSlashCommand}>
                  <i className="bi bi-x-lg"></i>
                </button>
              </div>
              <div className="slash-inline-fields">
                <label className="slash-inline-field">
                  <span>Label</span>
                  <input type="text" placeholder="Physical Roll" value={physicalLabel} onChange={(e) => setPhysicalLabel(e.target.value)} />
                </label>
                <label className="slash-inline-field">
                  <span>Die</span>
                  <select value={physicalSides} onChange={(e) => setPhysicalSides(parseInt(e.target.value, 10))}>
                    <option value="20">d20</option>
                    <option value="12">d12</option>
                    <option value="10">d10</option>
                    <option value="8">d8</option>
                    <option value="6">d6</option>
                    <option value="4">d4</option>
                    <option value="100">d100</option>
                  </select>
                </label>
                <label className="slash-inline-field">
                  <span>Result</span>
                  <input type="text" placeholder="e.g. 14" value={physicalRolls} onChange={(e) => updatePhysicalRolls(e.target.value)} autoFocus />
                </label>
                <label className="slash-inline-field">
                  <span>Mod</span>
                  <input type="number" value={physicalModifier} onChange={(e) => updatePhysicalModifier(e.target.value)} />
                </label>
                <label className="slash-inline-field">
                  <span>Total</span>
                  <input type="number" value={physicalTotal} readOnly />
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
    </div>
  )
}

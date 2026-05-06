import { useState, useRef, useEffect } from 'react'
import MarkdownContent from '../common/MarkdownContent'
import DiceRollStage from './DiceRollStage'
import { formatMessageForDm, hasIcSegment, parseQuotedMessage, parseTaggedMessage } from '../../utils/messageTags'

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function rollDie(sides, modifier = 0, advantage = false) {
  if (advantage) {
    const r1 = Math.floor(Math.random() * sides) + 1
    const r2 = Math.floor(Math.random() * sides) + 1
    return { result: Math.max(r1, r2), rolls: [r1, r2], total: Math.max(r1, r2) + modifier }
  }
  const r = Math.floor(Math.random() * sides) + 1
  return { result: r, rolls: [r], total: r + modifier }
}

const QUICK_SKILLS = [
  { label: 'Initiative', skill: 'Dexterity' },
  { label: 'Perception', skill: 'Wisdom' },
  { label: 'Stealth', skill: 'Dexterity' },
  { label: 'Investigation', skill: 'Intelligence' },
  { label: 'Insight', skill: 'Wisdom' },
  { label: 'Persuasion', skill: 'Charisma' },
  { label: 'Arcana', skill: 'Intelligence' },
  { label: 'History', skill: 'Intelligence' },
]

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

function SessionInputPreview({ value, highlightRef }) {
  const segments = parseQuotedMessage(value, { includeQuoteMarks: true })

  return (
    <div ref={highlightRef} className="session-input-highlight" aria-hidden="true">
      {segments.length ? segments.map((segment, index) => (
        <span
          key={`${segment.type}-${index}`}
          className={segment.type === 'ic' ? 'session-input-ic-highlight' : undefined}
        >
          {segment.text}
        </span>
      )) : '\u00a0'}
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

export default function SessionPanel({
  session,
  messages,
  currentUser,
  onStartSession,
  onEndSession,
  onSendMessage,
  aiThinking,
}) {
  const [input, setInput] = useState('')
  const [modifier, setModifier] = useState(0)
  const [advantage, setAdvantage] = useState(false)
  const [showDice, setShowDice] = useState(false)
  const [lastRoll, setLastRoll] = useState(null)
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)
  const inputHighlightRef = useRef(null)
  const rollIdRef = useRef(0)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = () => {
    const text = input.trim()
    if (!text) return

    if (text.startsWith('/roll ')) {
      const match = text.match(/\/roll\s*d(\d+)(?:\s*([+-]\s*\d+))?/)
      if (match) {
        const sides = parseInt(match[1], 10)
        const mod = match[2] ? parseInt(match[2].replace(/\s/g, ''), 10) : 0
        const { rolls, total, result } = rollDie(sides, mod)
        recordLocalRoll({ sides, rolls, total, result, modifier: mod, label: `d${sides}` })
        setShowDice(true)
      }
      setInput('')
      return
    }

    if (text.startsWith('/')) {
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

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleInputScroll = (e) => {
    if (inputHighlightRef.current) {
      inputHighlightRef.current.scrollTop = e.currentTarget.scrollTop
      inputHighlightRef.current.scrollLeft = e.currentTarget.scrollLeft
    }
  }

  const recordLocalRoll = ({ sides, rolls, total, result, modifier = 0, label }) => {
    rollIdRef.current += 1
    setLastRoll({
      id: rollIdRef.current,
      sides,
      rolls,
      total,
      result,
      modifier,
      label,
      rolledAt: new Date().toISOString(),
    })
  }

  const handleDieClick = (sides) => {
    const { rolls, total, result } = rollDie(sides, modifier, advantage)
    recordLocalRoll({ sides, rolls, total, result, modifier, label: `d${sides}` })
  }

  const handleSkillRoll = (skill) => {
    const { rolls, total, result } = rollDie(20, advantage ? 0 : 0, advantage)
    recordLocalRoll({ sides: 20, rolls, total, result, label: skill })
  }

  return (
    <div className="session-panel">
      {!session ? (
        <div className="session-idle">
          <div className="session-idle-icon"><i className="bi bi-dice-5-fill"></i></div>
          <h3>No Active Session</h3>
          <p>Start a new session to begin playing with the AI Dungeon Master.</p>
          <button className="btn btn-primary" onClick={onStartSession}>
            Start Session
          </button>
        </div>
      ) : (
        <>
          <div className="session-header">
            <span className="session-active-indicator" />
            <span className="session-status">Session Active</span>
            <button className="btn btn-secondary small" onClick={onEndSession} style={{ marginLeft: 'auto' }}>
              End Session
            </button>
          </div>

          <div className="session-messages">
            {messages.length === 0 && (
              <div className="session-empty-msg">
                The session has begun. Type an action or speak to the DM.
              </div>
            )}
            {messages.map((msg) => (
              <div key={msg.id} className={`session-msg session-msg-${msg.role}`}>
                <div className="session-msg-header">
                  <span className="session-msg-role">
                    <i className={getMessageSenderIcon(msg.role)}></i> {getMessageSenderLabel(msg, currentUser)}
                  </span>
                  <span className="session-msg-time">{formatTime(msg.created_at)}</span>
                </div>
                <div className={`session-msg-content ${msg.role === 'player' ? 'session-msg-content-tagged' : ''}`}>
                  {msg.role === 'dm' ? (
                    <MarkdownContent content={msg.content} />
                  ) : msg.role === 'player' ? (
                    <PlayerMessageContent content={msg.content} />
                  ) : (
                    msg.content
                  )}
                </div>
              </div>
            ))}
            {aiThinking && (
              <div className="session-msg session-msg-dm">
                <div className="session-msg-header">
                  <span className="session-msg-role"><i className="bi bi-mic-fill"></i> DM</span>
                </div>
                <div className="session-msg-content session-msg-thinking">
                  <span className="thinking-dot">.</span><span className="thinking-dot">.</span><span className="thinking-dot">.</span>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="session-roll-bar">
            <button
              className={`btn btn-roll-toggle ${showDice ? 'active' : ''}`}
              onClick={() => setShowDice(!showDice)}
            >
              <i className="bi bi-dice-5-fill"></i>
            </button>
            {showDice && (
              <>
                <div className="dice-stage">
                  <DiceRollStage roll={lastRoll} />
                </div>
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
                <div className="dice-grid">
                  {DICE.map((sides) => (
                    <button
                      key={sides}
                      className="btn btn-die"
                      onClick={() => handleDieClick(sides)}
                      title={`d${sides}`}
                    >
                      d{sides}
                    </button>
                  ))}
                </div>
                <div className="dice-controls">
                  <label className="dice-modifier">
                    Mod:
                    <input
                      type="number"
                      className="input dice-mod-input"
                      value={modifier}
                      onChange={(e) => setModifier(parseInt(e.target.value, 10) || 0)}
                    />
                  </label>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={advantage}
                      onChange={(e) => setAdvantage(e.target.checked)}
                    />
                    Advantage
                  </label>
                </div>
                <div className="dice-skills">
                  {QUICK_SKILLS.map((s) => (
                    <button
                      key={s.label}
                      className="btn btn-skill small"
                      onClick={() => handleSkillRoll(s.label)}
                      title={`Roll ${s.label} (${s.skill})`}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
              </>
            )}
          </div>

          <div className="session-input-area">
            <div className={`session-input-shell ${hasIcSegment(input) ? 'has-ic' : ''}`}>
              <SessionInputPreview value={input} highlightRef={inputHighlightRef} />
              <textarea
                ref={inputRef}
                className="textarea session-input"
                placeholder={aiThinking ? 'Waiting for DM...' : 'Type your action. Wrap speech in quotes for IC.'}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onScroll={handleInputScroll}
                onKeyDown={handleKeyDown}
                rows={2}
                disabled={aiThinking}
              />
            </div>
            <button className="btn btn-primary session-send-btn" onClick={handleSend} disabled={aiThinking}>
              {aiThinking ? '...' : 'Send'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

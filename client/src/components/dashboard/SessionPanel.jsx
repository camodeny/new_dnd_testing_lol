import { useState, useRef, useEffect } from 'react'

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

export default function SessionPanel({
  session,
  messages,
  onStartSession,
  onEndSession,
  onSendMessage,
  onRollDice,
  characters,
  aiThinking,
}) {
  const [input, setInput] = useState('')
  const [modifier, setModifier] = useState(0)
  const [advantage, setAdvantage] = useState(false)
  const [showDice, setShowDice] = useState(false)
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

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
        const { rolls, total } = rollDie(sides, mod)
        onRollDice(sides, rolls, total, mod)
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
        setTimeout(() => onSendMessage(text), 100)
      })
      setInput('')
      return
    }

    onSendMessage(text)
    setInput('')
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleDieClick = (sides) => {
    const { rolls, total, result } = rollDie(sides, modifier, advantage)
    const rollMsg = `🎲 d${sides} = ${rolls.join(', ')}${rolls.length > 1 ? ` (keep ${result})` : ''}${modifier ? ` + ${modifier} = ${total}` : ''}`
    onRollDice(sides, rolls, total, modifier)
    if (!session) {
      onStartSession().then(() => {
        setTimeout(() => onSendMessage(rollMsg), 100)
      })
    } else {
      onSendMessage(rollMsg)
    }
  }

  const handleSkillRoll = (skill) => {
    const { rolls, total, result } = rollDie(20, advantage ? 0 : 0, advantage)
    const rollMsg = `🎲 ${skill}: d20 = ${rolls.join(', ')}${rolls.length > 1 ? ` (keep ${result})` : ''} → ${total}`
    if (!session) {
      onStartSession().then(() => {
        setTimeout(() => onSendMessage(rollMsg), 100)
      })
    } else {
      onSendMessage(rollMsg)
    }
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
                    {msg.role === 'dm' ? <><i className="bi bi-mic-fill"></i> DM</> : msg.role === 'system' ? <><i className="bi bi-gear-fill"></i> System</> : <><i className="bi bi-person-fill"></i> You</>}
                  </span>
                  <span className="session-msg-time">{formatTime(msg.created_at)}</span>
                </div>
                <div className="session-msg-content">{msg.content}</div>
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
              <div className="dice-roller">
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
            )}
          </div>

          <div className="session-input-area">
            <textarea
              ref={inputRef}
              className="textarea session-input"
              placeholder={aiThinking ? 'Waiting for DM...' : "Type your action... (use /roll d20 or /commands)"}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={2}
              disabled={aiThinking}
            />
            <button className="btn btn-primary session-send-btn" onClick={handleSend} disabled={aiThinking}>
              {aiThinking ? '...' : 'Send'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

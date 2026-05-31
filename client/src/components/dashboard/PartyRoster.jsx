import { useNavigate } from 'react-router-dom'

function getGradient(str) {
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

function getInitials(name) {
  return name
    .split(' ')
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
}

const CONDITION_COLORS = {
  poisoned: '#a5b41c',
  charmed: '#e879f9',
  unconscious: '#6b7280',
  paralyzed: '#94a3b8',
  stunned: '#fbbf24',
  frightened: '#f59e0b',
  blinded: '#1e293b',
  deafened: '#64748b',
  prone: '#9ca3af',
  grappled: '#ef4444',
  restrained: '#f97316',
  incapacitated: '#a1a1aa',
  invisible: '#cbd5e1',
  concentration: '#f472b6',
}

function getHpPercent(current, max) {
  if (!max || max <= 0) return 0
  return Math.max(0, Math.min(100, (current / max) * 100))
}

function getHpColor(pct) {
  if (pct > 60) return '#4ade80'
  if (pct > 30) return '#facc15'
  return '#f87171'
}

function getClassIcon(classes) {
  if (!classes || classes.length === 0) return 'bi bi-person'
  const primaryClass = classes[0].class_name?.toLowerCase() || ''
  if (primaryClass.includes('paladin')) return 'bi bi-shield'
  if (primaryClass.includes('druid')) return 'bi bi-leaf'
  if (primaryClass.includes('rogue')) return 'bi bi-shield-slash'
  if (primaryClass.includes('bard')) return 'bi bi-music-note-beamed'
  if (primaryClass.includes('cleric')) return 'bi bi-brightness-high'
  if (primaryClass.includes('fighter')) return 'bi bi-swords'
  if (primaryClass.includes('wizard')) return 'bi bi-magic'
  if (primaryClass.includes('warlock')) return 'bi bi-eye'
  if (primaryClass.includes('sorcerer')) return 'bi bi-lightning-charge'
  if (primaryClass.includes('ranger')) return 'bi bi-target'
  if (primaryClass.includes('monk')) return 'bi bi-flower1'
  if (primaryClass.includes('barbarian')) return 'bi bi-hammer'
  return 'bi bi-person'
}

export default function PartyRoster({ characters = [], campaignId, onImport }) {
  const navigate = useNavigate()

  const playerLimit = 6
  const charactersList = characters || []
  const emptySlotsCount = Math.max(0, playerLimit - charactersList.length)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div className="party-roster-title-section">
        <h4>Party</h4>
      </div>

      {charactersList.length === 0 ? (
        <div className="roster-empty">
          <div className="roster-empty-icon"><i className="bi bi-shield-fill"></i></div>
          <p>No party members yet.</p>
          <div className="roster-empty-actions">
            <button className="btn btn-secondary small" onClick={() => navigate(`/characters/new?campaign=${campaignId}`)}>
              + Create New
            </button>
            {onImport && (
              <button className="btn btn-primary small" onClick={onImport}>
                <i className="bi bi-download"></i> Import Existing
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="roster-list">
          {charactersList.map((c) => {
            const gradient = getGradient(c.name)
            const initials = getInitials(c.name)
            const combat = c.combat || {}
            const hpPct = getHpPercent(combat.current_hp, combat.max_hp)
            const hpColor = '#4d7c5a' // Using the beautiful primary green color for health bars
            const levelSummary = c.classes?.map((cl) => `${cl.class_name} • Level ${cl.level}`).join(', ') || `Level ${c.total_level ?? '?'}`
            const mainRace = c.race || 'Race'
            const classIcon = getClassIcon(c.classes)

            return (
              <div
                key={c.id}
                className="roster-card"
                onClick={() => navigate(`/characters/${c.id}`)}
              >
                <div className="roster-card-body-layout">
                  <div className="roster-avatar-section">
                    <div className="roster-avatar" style={{ background: gradient }}>
                      {initials}
                    </div>
                    <span className="roster-hp-text-below">
                      {combat.current_hp}/{combat.max_hp} HP
                    </span>
                  </div>
                  
                  <div className="roster-details-section">
                    <div className="roster-name-row">
                      <div className="roster-name">{c.name}</div>
                    </div>
                    <div className="roster-subtitle">{mainRace} {c.classes?.length ? c.classes.map(cl => cl.class_name).join('/') : ''} &bull; Level {c.total_level ?? 5}</div>
                    
                    <div className="roster-hp-bar-container" title={`${combat.current_hp} / ${combat.max_hp} HP`}>
                      <div className="roster-hp-bar-bg">
                        <div
                          className="roster-hp-bar-fill"
                          style={{ width: `${hpPct}%`, background: '#3e7a5e' }}
                        />
                        {combat.temp_hp > 0 && (
                          <div
                            className="roster-hp-bar-temp"
                            style={{
                              width: `${Math.min(100, ((combat.current_hp + combat.temp_hp) / combat.max_hp) * 100)}%`,
                              left: `${hpPct}%`,
                            }}
                          />
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                {c.conditions?.length > 0 && (
                  <div className="roster-conditions">
                    {c.conditions.map((cond) => (
                      <span
                        key={cond.id}
                        className="condition-tag"
                        style={{
                          background: `${CONDITION_COLORS[cond.condition_name?.toLowerCase()] || '#6b7280'}22`,
                          color: CONDITION_COLORS[cond.condition_name?.toLowerCase()] || '#6b7280',
                          borderColor: `${CONDITION_COLORS[cond.condition_name?.toLowerCase()] || '#6b7280'}44`,
                        }}
                        title={cond.description || cond.condition_name}
                      >
                        {cond.condition_name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

    </div>
  )
}

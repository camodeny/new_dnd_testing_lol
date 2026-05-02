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

export default function PartyRoster({ characters, campaignId, onImport }) {
  const navigate = useNavigate()

  if (!characters || characters.length === 0) {
    return (
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
    )
  }

  return (
    <div className="roster-list">
      {characters.map((c) => {
        const gradient = getGradient(c.name)
        const initials = getInitials(c.name)
        const hpPct = getHpPercent(c.current_hp, c.max_hp)
        const hpColor = getHpColor(hpPct)
        const levels = c.classes?.map((cl) => `${cl.class_name} ${cl.level}`).join(', ') || `Level ${c.total_level}`

        return (
          <div
            key={c.id}
            className="roster-card"
            onClick={() => navigate(`/characters/${c.id}`)}
          >
            <div className="roster-card-header">
              <div className="roster-avatar" style={{ background: gradient }}>
                {initials}
              </div>
              <div className="roster-card-info">
                <div className="roster-name">{c.name}</div>
                <div className="roster-subtitle">{c.race} &mdash; {levels}</div>
              </div>
              <div className="roster-ac-badge" title="Armor Class">
                AC {c.armor_class}
              </div>
            </div>

            <div className="roster-hp-bar-container" title={`${c.current_hp} / ${c.max_hp} HP`}>
              <div className="roster-hp-bar-bg">
                <div
                  className="roster-hp-bar-fill"
                  style={{ width: `${hpPct}%`, background: hpColor }}
                />
                {c.temp_hp > 0 && (
                  <div
                    className="roster-hp-bar-temp"
                    style={{
                      width: `${Math.min(100, ((c.current_hp + c.temp_hp) / c.max_hp) * 100)}%`,
                      left: `${hpPct}%`,
                    }}
                  />
                )}
              </div>
              <span className="roster-hp-text">
                {c.current_hp}{c.temp_hp > 0 ? `+${c.temp_hp}` : ''} / {c.max_hp}
              </span>
            </div>

            <div className="roster-conditions">
              {c.conditions?.length > 0 ? (
                c.conditions.map((cond) => (
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
                ))
              ) : (
                <span className="condition-none">No conditions</span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

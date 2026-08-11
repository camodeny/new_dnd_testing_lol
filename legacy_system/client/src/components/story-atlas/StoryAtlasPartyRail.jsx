import { getGradientSeed } from '../../pages/CampaignViewPage'

export default function StoryAtlasPartyRail({
  campaign = {},
  party = [],
  currentUser = null,
  actions = {},
}) {
  const getInitials = (name) => {
    if (!name) return '?'
    return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
  }

  const getHpSummary = (char) => {
    const hp = char.combat?.current_hp !== undefined ? char.combat.current_hp : '?'
    const max = char.combat?.max_hp !== undefined ? char.combat.max_hp : '?'
    return `${hp} / ${max} HP`
  }

  const getClassSummary = (char) => {
    if (char.classes?.length) {
      return char.classes.map((c) => `${c.class_name} ${c.level}`).join(', ')
    }
    return `Level ${char.total_level ?? '?'}`
  }

  return (
    <aside className="sampler-party">
      <div className="sampler-campaign-mark">
        <span>✦</span>
        <div>
          <strong>{campaign.name || 'Campaign'}</strong>
          <small>{campaign.locationContext || 'Exploration'}</small>
        </div>
      </div>
      <div className="sampler-party-label">
        At the table <span>{party.length}</span>
      </div>
      <div className="sampler-party-list">
        {party.map((char) => {
          const initials = getInitials(char.name)
          const grad = getGradientSeed(char.name)
          const hpSummary = getHpSummary(char)
          const detail = `${char.race || 'Unknown'} · ${getClassSummary(char)}`
          return (
            <div
              className="sampler-party-member"
              key={char.id}
              onClick={() => actions.onNavigateCharacter?.(char.id)}
              style={{ cursor: 'pointer' }}
            >
              <span className="sampler-avatar" style={{ background: grad }}>
                {initials}
              </span>
              <div>
                <strong>{char.name}</strong>
                <small>{detail}</small>
                <em>{hpSummary}</em>
              </div>
            </div>
          )
        })}
      </div>
      <nav className="sampler-nav">
        <button onClick={() => actions.onNavigateCharacters?.()}>
          <i className="bi bi-person-badge" /> Characters
        </button>
        {actions.onToggleWorldJournal && (
          <button onClick={() => actions.onToggleWorldJournal()}>
            <i className="bi bi-book" /> World Journal
          </button>
        )}
        <button onClick={() => actions.onOpenSettings?.()}>
          <i className="bi bi-gear" /> Campaign settings
        </button>
        {actions.onNavigateAutomation && (
          <button onClick={() => actions.onNavigateAutomation()}>
            <i className="bi bi-activity" /> Run Workspace
          </button>
        )}
        {actions.onImportCharacter && (
          <button onClick={() => actions.onImportCharacter()}>
            <i className="bi bi-download" /> Import Character
          </button>
        )}
      </nav>
      {currentUser && (
        <div className="sampler-profile">
          <span>{getInitials(currentUser.username)}</span>
          <div>
            <strong>{currentUser.username}</strong>
            <small>{campaign.user_id === currentUser.id ? 'Host' : 'Player'}</small>
          </div>
          <button onClick={() => actions.onExitToCampaigns?.()} title="Exit to campaigns">
            <i className="bi bi-door-open" />
          </button>
        </div>
      )}
    </aside>
  )
}

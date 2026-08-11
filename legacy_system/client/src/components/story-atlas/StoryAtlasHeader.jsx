export default function StoryAtlasHeader({
  campaign = {},
  scene = {},
  session = null,
  encounter = {},
  view = 'story',
  setView = () => {},
  actions = {},
}) {
  const isEncounterActive = Boolean(encounter.hasActiveMap)
  const isSessionLive = Boolean(session)
  const statusLabel = isSessionLive ? 'SESSION LIVE' : 'TABLE READY'

  const handleSessionAction = () => {
    if (isSessionLive) {
      actions.onEndSession?.()
    } else {
      actions.onStartSession?.()
    }
  }

  return (
    <header className="sampler-topbar">
      <div className="story-atlas-session-context">
        <span className={isSessionLive ? 'is-live' : ''}>
          <i />
          {statusLabel}
        </span>
        <div className="story-atlas-scene-title">
          <strong>{scene.location_name || campaign.settings?.current_location || 'World Map'}</strong>
          <small>{isEncounterActive ? 'Combat encounter' : 'Exploration'}</small>
        </div>
      </div>

      <nav role="tablist" className="atlas-hybrid-tabs">
        <button
          role="tab"
          aria-selected={view === 'story'}
          className={view === 'story' ? 'active' : ''}
          onClick={() => setView('story')}
        >
          <i className="bi bi-chat-square-text" /> Story
        </button>
        {isEncounterActive && (
          <button
            role="tab"
            aria-selected={view === 'map'}
            className={view === 'map' ? 'active' : ''}
            onClick={() => setView('map')}
          >
            <i className="bi bi-map" /> Map
          </button>
        )}
      </nav>

      <button className="sampler-end" onClick={handleSessionAction}>
        {isSessionLive ? 'End session' : 'Start session'}
      </button>
    </header>
  )
}

import LlmPlayerManager from '../dashboard/LlmPlayerManager'

export default function StoryAtlasContextRail({
  campaign = {},
  scene = {},
  encounter = {},
  worldState = {},
  activity = [],
  setView = () => {},
  actions = {},
}) {
  const isEncounterActive = Boolean(encounter.hasActiveMap)
  const mapData = encounter.encounterMap || {}

  const openThreads = worldState.open_threads || []
  const timeOfDay = scene.time_of_day || ''
  const immediateTension = scene.immediate_tension || ''

  const metadataString = [
    timeOfDay,
    immediateTension,
  ].filter(Boolean).join(' · ')

  return (
    <aside className="sampler-context">
      {actions.showLlmTools && (
        <LlmPlayerManager
          campaignId={campaign.id}
          enabled={actions.showLlmTools}
          isOwner={actions.isOwner}
          onAdded={actions.onLlmPlayerAdded}
        />
      )}
      <section className="sampler-scene">
        <small>CURRENT SCENE</small>
        <h3>{scene.location_name || campaign.settings?.current_location || 'World Map'}</h3>
        {metadataString && <p>{metadataString}</p>}
        
        {isEncounterActive ? (
          <button onClick={() => setView('map')} aria-label="Open tactical map view">
            <span>
              <i className="bi bi-map" />
            </span>
            <div>
              <strong>{mapData.title || 'Combat map'}</strong>
              <small>{campaign.name || 'Open tactical view'}</small>
            </div>
            <i className="bi bi-arrow-up-right" />
          </button>
        ) : (
          <div className="sampler-scene-state">
            <i className="bi bi-signpost-split" />
            <div>
              <strong>Exploration in progress</strong>
              <small>No combat map active</small>
            </div>
          </div>
        )}
      </section>

      {openThreads.length > 0 && (
        <section>
          <header>
            <small>STORY THREADS</small>
            <span>{openThreads.length} active</span>
          </header>
          {openThreads.map((thread, idx) => (
            <div className="sampler-thread" key={idx}>
              <i />
              <div>
                <strong>{thread.title || thread}</strong>
                {thread.description && <small>{thread.description}</small>}
              </div>
            </div>
          ))}
        </section>
      )}

      <section>
        <header>
          <small>RECENT ACTIVITY</small>
        </header>
        <div style={{ display: 'grid', gap: '8px', marginTop: '8px' }}>
          {activity.length > 0 ? (
            activity.map((act) => (
              <div key={act.id} style={{ display: 'flex', gap: '8px', fontSize: '10px' }}>
                <span style={{ flexShrink: 0 }}>{act.avatar || '🎲'}</span>
                <div>
                  <strong>{act.user}</strong> {act.text}
                </div>
              </div>
            ))
          ) : (
            <p className="muted">Important rolls and updates will collect here.</p>
          )}
        </div>
      </section>
    </aside>
  )
}

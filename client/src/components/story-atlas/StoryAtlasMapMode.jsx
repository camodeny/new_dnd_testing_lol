import { useState } from 'react'
import EncounterMapBoard from '../dashboard/EncounterMapBoard'
import SessionMessageList from './SessionMessageList'
import SessionComposer from './SessionComposer'
import { getGradientSeed } from '../../pages/CampaignViewPage'

export default function StoryAtlasMapMode({
  campaign = {},
  party = [],
  scene = {},
  session = null,
  messages = [],
  currentUser = null,
  currentCharacter = null,
  encounter = {},
  setView = () => {},
  actions = {},
  // Hoisted Composer states
  composerProps = {},
}) {
  const isEncounterActive = Boolean(encounter.hasActiveMap)
  const mapData = encounter.encounterMap || {}
  
  // Local overlay controls synced with parent or defaulted
  const [showGridLines, setShowGridLines] = useState(true)
  const [showTacticalOverlay, setShowTacticalOverlay] = useState(true)
  const [showCellInspector, setShowCellInspector] = useState(true)
  const [activeHoverId, setActiveHoverId] = useState(null)

  const getInitials = (name) => {
    if (!name) return '?'
    return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
  }

  const round = encounter.encounterState?.round ?? 1
  const turnOrder = encounter.encounterState?.turn_order || []
  const activeTurnIndex = encounter.encounterState?.active_turn_index

  const activeCombatant = activeTurnIndex !== null && activeTurnIndex !== undefined && turnOrder[activeTurnIndex]
    ? turnOrder[activeTurnIndex]
    : null

  return (
    <section className="atlas-workspace">
      <div className="sampler-map is-immersive">
        <EncounterMapBoard
          encounterMap={mapData}
          loading={encounter.loading}
          isOwner={campaign.user_id === currentUser?.id}
          currentUser={currentUser}
          currentCharacter={currentCharacter}
          onEncounterMapChange={actions.onEncounterMapChange}
          onSendMessage={actions.onSendMessage}
          showGridLines={showGridLines}
          showTacticalOverlay={showTacticalOverlay}
          showCellInspector={showCellInspector}
          activeHoverId={activeHoverId}
          setActiveHoverId={setActiveHoverId}
          imagePadding="0px"
        />
      </div>

      <header className="atlas-header">
        <div>
          <span>✦</span>
          <strong>{campaign.name || 'Campaign'}</strong>
          <small>
            {isEncounterActive
              ? `Combat map · Round ${round}`
              : `Scene board · ${session ? 'Exploration' : 'Table ready'}`}
          </small>
        </div>

        <nav className="atlas-hybrid-tabs">
          <button onClick={() => setView('story')}>
            <i className="bi bi-chat-square-text" /> Story
          </button>
          <button className="active">
            <i className="bi bi-map" /> Map
          </button>
        </nav>

        <button className="sampler-end" onClick={() => actions.onEndSession?.()}>
          End session
        </button>
      </header>

      {/* Toolbar mapped to functional overlay controls */}
      <nav className="atlas-tools" aria-label="Map tools">
        <button
          className={showCellInspector ? 'active' : ''}
          onClick={() => setShowCellInspector(!showCellInspector)}
          title="Toggle Cell Inspector"
        >
          <i className="bi bi-cursor" />
        </button>
        <button
          className={showGridLines ? 'active' : ''}
          onClick={() => setShowGridLines(!showGridLines)}
          title="Toggle Grid Lines"
        >
          <i className="bi bi-grid-3x3" />
        </button>
        <button
          className={showTacticalOverlay ? 'active' : ''}
          onClick={() => setShowTacticalOverlay(!showTacticalOverlay)}
          title="Toggle Tactical Layer"
        >
          <i className="bi bi-layers" />
        </button>
        <span />
        <button onClick={() => actions.onOpenSettings?.()} title="Campaign Settings">
          <i className="bi bi-gear" />
        </button>
      </nav>

      {/* Live Feed Sidebar Panel */}
      <aside className="atlas-feed">
        <header>
          <div>
            <small>LIVE FEED</small>
            <strong>{scene.location_name || campaign.settings?.current_location || 'Explore'}</strong>
          </div>
        </header>
        <div className="atlas-feed-scroll">
          <SessionMessageList
            messages={messages}
            currentUser={currentUser}
            session={session}
            onProposalApplied={actions.onProposalApplied}
            onProposalDismissed={actions.onProposalDismissed}
            characters={party}
            aiThinking={composerProps.aiThinking}
            aiThinkingStatus={composerProps.aiThinkingStatus}
          />
        </div>
        <footer style={{ background: '#192021', borderTop: '1px solid rgba(255,255,255,0.1)' }}>
          <SessionComposer
            {...composerProps}
            currentUser={currentUser}
            currentCharacter={currentCharacter}
            onSendMessage={actions.onSendMessage}
            session={session}
            onStartSession={actions.onStartSession}
          />
        </footer>
      </aside>

      {/* Bottom Party Dock */}
      <div className="atlas-party-dock">
        {party.map((member) => {
          const initials = getInitials(member.name)
          const grad = getGradientSeed(member.name)
          const hpCurrent = member.combat?.current_hp !== undefined ? member.combat.current_hp : '?'
          const hpMax = member.combat?.max_hp !== undefined ? member.combat.max_hp : '?'
          
          return (
            <div key={member.id} title={`${member.name} (${hpCurrent}/${hpMax} HP)`}>
              <span style={{ background: grad }}>{initials}</span>
              <small>{hpCurrent}</small>
            </div>
          )
        })}
      </div>
    </section>
  )
}

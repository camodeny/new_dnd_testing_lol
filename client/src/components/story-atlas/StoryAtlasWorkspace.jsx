import { useState, useEffect } from 'react'
import './StoryAtlas.css'
import StoryAtlasPartyRail from './StoryAtlasPartyRail'
import StoryAtlasHeader from './StoryAtlasHeader'
import StoryAtlasStoryStage from './StoryAtlasStoryStage'
import StoryAtlasContextRail from './StoryAtlasContextRail'
import StoryAtlasMapMode from './StoryAtlasMapMode'
import SessionComposer from './SessionComposer'

export default function StoryAtlasWorkspace({
  campaign = {},
  party = [],
  scene = {},
  session = null,
  messages = [],
  currentUser = null,
  currentCharacter = null,
  encounter = {},
  worldState = {},
  activity = [],
  actions = {},
  // Hoisted states to satisfy no-reset/no-remount state preservation across layouts
  composerProps = {},
}) {
  const [view, setView] = useState(() => encounter.hasActiveMap ? 'map' : 'story')
  const [mobileTab, setMobileTab] = useState(() => encounter.hasActiveMap ? 'map' : 'story')

  const isMapModeActive = view === 'map' && encounter.hasActiveMap

  // Keep mobileTab in sync when view changes
  useEffect(() => {
    if (view === 'map' && mobileTab !== 'map') {
      setMobileTab('map')
    } else if (view === 'story' && mobileTab === 'map') {
      setMobileTab('story')
    }
  }, [view])

  const handleMobileTabChange = (tab) => {
    setMobileTab(tab)
    if (tab === 'map') {
      setView('map')
    } else {
      setView('story')
    }
  }

  return (
    <div className={`story-atlas-workspace-container mobile-tab-${mobileTab} ${isMapModeActive ? 'is-map-active' : ''}`}>
      {/* Immersive Map view (AtlasWorkspace Mode) */}
      <div className="map-view-wrapper">
        <StoryAtlasMapMode
          campaign={campaign}
          party={party}
          scene={scene}
          session={session}
          messages={messages}
          currentUser={currentUser}
          currentCharacter={currentCharacter}
          encounter={encounter}
          view={view}
          setView={setView}
          actions={actions}
          composerProps={composerProps}
        />
      </div>

      {/* Exploration view (HearthWorkspace Mode) */}
      <div className="story-atlas-workspace">
        <StoryAtlasPartyRail
          campaign={campaign}
          party={party}
          currentUser={currentUser}
          actions={actions}
        />
        
        <main className="sampler-main">
          <StoryAtlasHeader
            campaign={campaign}
            scene={scene}
            session={session}
            encounter={encounter}
            view={view}
            setView={setView}
            actions={actions}
          />
          <div className="sampler-stage">
            <StoryAtlasStoryStage
              session={session}
              messages={messages}
              currentUser={currentUser}
              characters={party}
              aiThinking={composerProps.aiThinking}
              aiThinkingStatus={composerProps.aiThinkingStatus}
              hasOlderMessages={composerProps.hasOlderMessages}
              loadingOlderMessages={composerProps.loadingOlderMessages}
              onLoadOlderMessages={composerProps.onLoadOlderMessages}
              actions={actions}
            />
          </div>
          {session && (
            <footer style={{ background: '#0e1314' }}>
              <SessionComposer
                {...composerProps}
                active={view === 'story'}
                currentUser={currentUser}
                currentCharacter={currentCharacter}
                onSendMessage={actions.onSendMessage}
                session={session}
                onStartSession={actions.onStartSession}
              />
            </footer>
          )}
        </main>

        <StoryAtlasContextRail
          campaign={campaign}
          scene={scene}
          encounter={encounter}
          worldState={worldState}
          activity={activity}
          setView={setView}
          actions={actions}
        />
      </div>

      {/* Mobile Bottom Navigation Bar */}
      <nav className="mobile-bottom-nav" role="tablist">
        <button
          role="tab"
          aria-selected={mobileTab === 'party'}
          className={mobileTab === 'party' ? 'active' : ''}
          onClick={() => handleMobileTabChange('party')}
        >
          <i className="bi bi-people" />
          <span>Party</span>
        </button>
        <button
          role="tab"
          aria-selected={mobileTab === 'story'}
          className={mobileTab === 'story' ? 'active' : ''}
          onClick={() => handleMobileTabChange('story')}
        >
          <i className="bi bi-chat-square-text" />
          <span>Story</span>
        </button>
        {encounter.hasActiveMap && (
          <button
            role="tab"
            aria-selected={mobileTab === 'map'}
            className={mobileTab === 'map' ? 'active' : ''}
            onClick={() => handleMobileTabChange('map')}
          >
            <i className="bi bi-map" />
            <span>Map</span>
          </button>
        )}
        <button
          role="tab"
          aria-selected={mobileTab === 'activity'}
          className={mobileTab === 'activity' ? 'active' : ''}
          onClick={() => handleMobileTabChange('activity')}
        >
          <i className="bi bi-activity" />
          <span>Activity</span>
        </button>
      </nav>
    </div>
  )
}

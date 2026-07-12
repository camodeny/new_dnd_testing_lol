import { useState } from 'react'
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
  const [view, setView] = useState('story')

  const isMapModeActive = view === 'map' && encounter.hasActiveMap

  return (
    <div className="story-atlas-workspace-container" style={{ width: '100%', height: '100svh', position: 'relative', overflow: 'hidden' }}>
      {/* Immersive Map view (AtlasWorkspace Mode) */}
      <div style={{ display: isMapModeActive ? 'block' : 'none', width: '100%', height: '100%' }}>
        <StoryAtlasMapMode
          campaign={campaign}
          party={party}
          scene={scene}
          session={session}
          messages={messages}
          currentUser={currentUser}
          currentCharacter={currentCharacter}
          encounter={encounter}
          setView={setView}
          actions={actions}
          composerProps={composerProps}
        />
      </div>

      {/* Exploration view (HearthWorkspace Mode) */}
      <div 
        className="story-atlas-workspace" 
        style={{ display: isMapModeActive ? 'none' : 'grid', width: '100%', height: '100%' }}
      >
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
    </div>
  )
}

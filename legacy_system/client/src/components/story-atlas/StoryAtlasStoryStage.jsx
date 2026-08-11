import SessionMessageList from './SessionMessageList'

export default function StoryAtlasStoryStage({
  session = null,
  messages = [],
  currentUser = null,
  characters = [],
  aiThinking = false,
  aiThinkingStatus = '',
  hasOlderMessages = false,
  loadingOlderMessages = false,
  onLoadOlderMessages = null,
  actions = {},
}) {
  if (!session) {
    return (
      <div className="sampler-empty">
        <span>✦</span>
        <small>THE NEXT CHAPTER</small>
        <h2>Your table is ready.</h2>
        <p>Gather the party, review the last scene, and begin when everyone is settled.</p>
        <button onClick={() => actions.onStartSession?.()}>
          Start session <i className="bi bi-arrow-right" />
        </button>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', width: '100%', minHeight: 0 }}>
      <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
        <SessionMessageList
          messages={messages}
          currentUser={currentUser}
          session={session}
          onProposalApplied={actions.onProposalApplied}
          onProposalDismissed={actions.onProposalDismissed}
          characters={characters}
          aiThinking={aiThinking}
          aiThinkingStatus={aiThinkingStatus}
          hasOlderMessages={hasOlderMessages}
          loadingOlderMessages={loadingOlderMessages}
          onLoadOlderMessages={onLoadOlderMessages}
        />
      </div>
    </div>
  )
}

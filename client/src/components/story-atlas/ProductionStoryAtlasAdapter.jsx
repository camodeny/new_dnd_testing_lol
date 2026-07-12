import { useState, useMemo } from 'react'
import StoryAtlasWorkspace from './StoryAtlasWorkspace'

export default function ProductionStoryAtlasAdapter({
  campaign,
  party,
  currentScene,
  world,
  session,
  messages,
  currentUser,
  currentCharacter,
  encounterMap,
  onStartSession,
  onEndSession,
  onSendMessage,
  onProposalApplied,
  onProposalDismissed,
  onNavigateCharacter,
  onNavigateCharacters,
  onOpenSettings,
  onExitToCampaigns,
  onEncounterMapChange,
  // Message history loading
  hasOlderMessages,
  loadingOlderMessages,
  onLoadOlderMessages,
  // Read-only / Spectator & AI state
  canSendMessage = true,
  readOnlyReason = '',
  aiThinking = false,
  aiThinkingStatus = '',
  // Action toggles and LLM details
  showLlmTools = false,
  isOwner = false,
  onLlmPlayerAdded = () => {},
  onToggleLootStash = () => {},
  onToggleShops = () => {},
  onNavigateAutomation = () => {},
  onToggleWorldJournal = () => {},
  onImportCharacter = () => {},
}) {
  // Hoist all SessionComposer states here to satisfy the requirement:
  // "transient Story UI state (draft text, active slash commands, dice state, scroll position) is preserved across Map toggles."
  const [input, setInput] = useState('')
  const [showDice, setShowDice] = useState(false)
  const [lastRoll, setLastRoll] = useState(null)
  const [rollQueue, setRollQueue] = useState([])
  const [activeTab, setActiveTab] = useState('custom')
  const [modifier, setModifier] = useState(0)
  const [rollMode, setRollMode] = useState('normal')
  const [customLabel, setCustomLabel] = useState('')
  const [activeSlashCommand, setActiveSlashCommand] = useState(null)
  const [physicalLabel, setPhysicalLabel] = useState('')
  const [physicalSides, setPhysicalSides] = useState(20)
  const [physicalRolls, setPhysicalRolls] = useState('')
  const [physicalModifier, setPhysicalModifier] = useState(0)
  const [physicalTotal, setPhysicalTotal] = useState(0)

  // Compute active scene location & details from world object
  const scene = useMemo(() => ({
    location_name: world?.current_scene?.location_name || campaign?.settings?.current_location || 'Exploration',
    time_of_day: world?.current_scene?.time_of_day || '',
    immediate_tension: world?.current_scene?.immediate_tension || '',
  }), [world, campaign])

  // Extract recent activity from messages to render on the context rail
  const activity = useMemo(() => {
    return messages
      .filter((m) => m.content && (m.content.includes('[Roll:') || m.is_proposal))
      .slice(-5)
      .map((m) => {
        const isRoll = m.content.includes('[Roll:')
        return {
          id: m.id,
          user: m.username || (m.role === 'dm' ? 'DM' : 'System'),
          text: isRoll ? 'rolled dice' : 'proposed update',
          avatar: isRoll ? '🎲' : '⚙️',
        }
      })
  }, [messages])

  const encounter = useMemo(() => ({
    hasActiveMap: Boolean(encounterMap),
    encounterMap: encounterMap,
    encounterState: encounterMap?.encounter_state_json || encounterMap?.encounter_state || {},
    loading: false,
  }), [encounterMap])

  // Pull threads from real world-state contract
  const worldState = useMemo(() => ({
    open_threads: world?.open_threads || campaign?.settings?.open_threads || [],
  }), [world, campaign])

  const actions = {
    onStartSession,
    onEndSession,
    onSendMessage,
    onProposalApplied,
    onProposalDismissed,
    onNavigateCharacter,
    onNavigateCharacters,
    onOpenSettings,
    onExitToCampaigns,
    onEncounterMapChange,
    onToggleWorldJournal,
    onImportCharacter,
    onToggleLootStash,
    onToggleShops,
    onNavigateAutomation,
    showLlmTools,
    isOwner,
    onLlmPlayerAdded,
  }

  const composerProps = {
    input, setInput,
    showDice, setShowDice,
    lastRoll, setLastRoll,
    rollQueue, setRollQueue,
    activeTab, setActiveTab,
    modifier, setModifier,
    rollMode, setRollMode,
    customLabel, setCustomLabel,
    activeSlashCommand, setActiveSlashCommand,
    physicalLabel, setPhysicalLabel,
    physicalSides, setPhysicalSides,
    physicalRolls, setPhysicalRolls,
    physicalModifier, setPhysicalModifier,
    physicalTotal, setPhysicalTotal,
    hasOlderMessages,
    loadingOlderMessages,
    onLoadOlderMessages,
    canSendMessage,
    readOnlyReason,
    aiThinking,
    aiThinkingStatus,
  }

  return (
    <StoryAtlasWorkspace
      campaign={campaign}
      party={party}
      scene={scene}
      session={session}
      messages={messages}
      currentUser={currentUser}
      currentCharacter={currentCharacter}
      encounter={encounter}
      worldState={worldState}
      activity={activity}
      actions={actions}
      composerProps={composerProps}
    />
  )
}

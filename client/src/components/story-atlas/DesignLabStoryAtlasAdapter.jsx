import { useState, useEffect, useMemo } from 'react'
import StoryAtlasWorkspace from './StoryAtlasWorkspace'

export default function DesignLabStoryAtlasAdapter({ scenario, map }) {
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
  const [aiThinking, setAiThinking] = useState(scenario === 'thinking')
  const [aiThinkingStatus, setAiThinkingStatus] = useState(scenario === 'thinking' ? 'Checking the scene...' : '')

  useEffect(() => {
    setAiThinking(scenario === 'thinking')
    setAiThinkingStatus(scenario === 'thinking' ? 'Checking the scene, your character, and recent events' : '')
  }, [scenario])

  const campaign = {
    name: 'The Oath Below Emberfield',
    locationContext: 'Chapter III · Ashglass Market',
    user_id: 999, // mock owner
  }

  const party = [
    { id: 1, name: 'Brixby Tinkertop', user_id: 1, initials: 'BT', combat: { current_hp: 22, max_hp: 22 }, race: 'Gnome', classes: [{ class_name: 'Bard', level: 3 }], color: '#cc5075' },
    { id: 2, name: 'Vesper Ash', user_id: 2, initials: 'VA', combat: { current_hp: 18, max_hp: 21 }, race: 'Tiefling', classes: [{ class_name: 'Rogue', level: 3 }], color: '#8367c7' },
    { id: 3, name: 'Orren Reed', user_id: 3, initials: 'OR', combat: { current_hp: 26, max_hp: 26 }, race: 'Human', classes: [{ class_name: 'Cleric', level: 3 }], color: '#4e8c76' },
  ]

  const currentUser = { id: 1, username: 'phazedrl' }
  const currentCharacter = party[0]

  const scene = {
    location_name: 'Ashglass Market',
    time_of_day: 'Morning',
    immediate_tension: 'Tense',
  }

  const session = scenario === 'ready' ? null : { id: 'mock-session' }

  const messages = useMemo(() => {
    if (scenario === 'ready') return []
    const msgs = [
      {
        id: 1,
        role: 'dm',
        content: `The late-morning sun bakes Ashglass Market Square, but the usual trade has ground to a halt. Every eye is fixed on the central platform where Baron Thorne points an accusing finger at the village healer.\n\n> <small>BARON THORNE</small>\n> “The grain went bad in her hands. This sickness started here.”\n\nThe crowd waits for someone to break the tension. What does Brixby do?`,
        created_at: new Date().toISOString(),
      },
      {
        id: 2,
        role: 'player',
        user_id: 1,
        username: 'Brixby Tinkertop',
        content: `“Before we condemn anyone, perhaps we should ask what spoiled grain smells like.” I move toward the sacks and look for signs of alchemy.`,
        created_at: new Date().toISOString(),
      }
    ]
    if (scenario === 'roll') {
      msgs.push({
        id: 3,
        role: 'player',
        content: '[Roll: Investigation check] total: 18 | rolls: 14 | mod: 4 | sides: 20',
        created_at: new Date().toISOString(),
      })
    }
    if (scenario === 'proposal') {
      msgs.push({
        id: 4,
        role: 'system',
        is_proposal: true,
        proposal: { id: 'mock-prop', status: 'pending', reason: 'New clue added: Alchemical residue found on the grain sacks.' },
        created_at: new Date().toISOString(),
      })
    }
    return msgs
  }, [scenario])

  const encounter = {
    hasActiveMap: scenario === 'encounter',
    encounterMap: scenario === 'encounter' ? map : null,
    encounterState: scenario === 'encounter' ? { round: 2, turn_order: [] } : {},
    loading: map.status === 'loading',
  }

  const worldState = {
    open_threads: [
      { title: 'The blighted grain', description: 'Find the source before dusk' },
      { title: 'Baron Thorne’s claim', description: 'Unverified accusation' }
    ]
  }

  const activity = scenario === 'roll' ? [
    { id: 1, user: 'Brixby', text: 'rolled Investigation 18', avatar: '🎲' }
  ] : scenario === 'proposal' ? [
    { id: 2, user: 'System', text: 'proposed Character sheet update', avatar: '⚙️' }
  ] : []

  const actions = {
    onStartSession: () => alert('Mock Session Started'),
    onEndSession: () => alert('Mock Session Ended'),
    onSendMessage: (msg) => alert(`Mock Send Message: ${msg}`),
    onProposalApplied: () => alert('Mock Proposal Applied'),
    onProposalDismissed: () => alert('Mock Proposal Dismissed'),
    onOpenSettings: () => alert('Mock Open Settings'),
    onExitToCampaigns: () => alert('Mock Exit to Campaigns'),
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
    aiThinking, aiThinkingStatus,
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

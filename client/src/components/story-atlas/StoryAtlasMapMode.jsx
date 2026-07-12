import { useState, useMemo } from 'react'
import EncounterMapBoard from '../dashboard/EncounterMapBoard'
import SessionMessageList from './SessionMessageList'
import SessionComposer from './SessionComposer'
import { getGradientSeed } from '../../pages/CampaignViewPage'
import { rollPlayerInitiative, advanceEncounterTurn } from '../../api/client'

export default function StoryAtlasMapMode({
  campaign = {},
  party = [],
  scene = {},
  session = null,
  messages = [],
  currentUser = null,
  currentCharacter = null,
  encounter = {},
  view = 'story',
  setView = () => {},
  actions = {},
  // Hoisted Composer states
  composerProps = {},
}) {
  const isEncounterActive = Boolean(encounter.hasActiveMap)
  const mapData = encounter.encounterMap || {}
  
  // Local VTT state controls
  const [showGridLines, setShowGridLines] = useState(true)
  const [showTacticalOverlay, setShowTacticalOverlay] = useState(true)
  const [showCellInspector, setShowCellInspector] = useState(true)
  const [activeHoverId, setActiveHoverId] = useState(null)
  
  const [manualInitValues, setManualInitValues] = useState({})
  const [rollingInitId, setRollingInitId] = useState(null)

  const currentUserActorId = currentUser?.id != null ? String(currentUser.id) : ''

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

  const isUserActiveTurn = useMemo(() => {
    if (!activeCombatant) return false
    return activeCombatant.actor_type === 'player' && String(activeCombatant.actor_id) === currentUserActorId
  }, [activeCombatant, currentUserActorId])

  const handleRollInitiative = async (combatant) => {
    if (!mapData?.id) return
    setRollingInitId(combatant.placement_id)
    try {
      const data = await rollPlayerInitiative(mapData.id, combatant.actor_type, combatant.actor_id)
      actions.onEncounterMapChange?.(data.encounter_map)
    } catch (err) {
      alert(`Initiative roll failed: ${err.message}`)
    } finally {
      setRollingInitId(null)
    }
  }

  const handleManualInitiativeSubmit = async (combatant, value) => {
    if (!mapData?.id) return
    const init = parseInt(value, 10)
    if (Number.isNaN(init)) return
    try {
      const data = await rollPlayerInitiative(mapData.id, combatant.actor_type, combatant.actor_id, init)
      actions.onEncounterMapChange?.(data.encounter_map)
      setManualInitValues(prev => {
        const next = { ...prev }
        delete next[`${combatant.actor_type}-${combatant.actor_id}`]
        return next
      })
    } catch (err) {
      alert(`Setting manual initiative failed: ${err.message}`)
    }
  }

  const handleNextTurn = async () => {
    try {
      const data = await advanceEncounterTurn(campaign.id)
      actions.onEncounterMapChange?.(data.encounter_map)
    } catch (err) {
      alert(`Failed to advance turn: ${err.message}`)
    }
  }

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

      {/* Full-width Combat Tracker inside Map view */}
      {isEncounterActive && (
        <div className="encounter-combat-tracker" style={{ position: 'absolute', top: '72px', left: '80px', right: '340px', zIndex: 10, background: 'rgba(10, 11, 15, 0.9)', backdropFilter: 'blur(8px)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 'var(--radius-md)', padding: '8px 12px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div className="encounter-combat-tracker-left">
            <span className="encounter-combat-tracker-round" style={{ fontWeight: 'bold', color: 'var(--text-gold)' }}>
              Round {round}
            </span>
          </div>

          <div className="encounter-tracker-list-container" style={{ flex: 1, overflowX: 'auto', margin: '0 16px' }}>
            <div className="encounter-tracker-list" style={{ display: 'flex', gap: '8px' }}>
              {turnOrder.map((combatant, idx) => {
                const canRollOrInput = combatant.actor_type === 'player' && String(combatant.actor_id) === currentUserActorId
                const hasInitiative = combatant.initiative !== null && combatant.initiative !== undefined
                const isActiveItem = idx === activeTurnIndex

                return (
                  <div
                    key={`${combatant.actor_type}-${combatant.actor_id}-${combatant.placement_id || idx}`}
                    className={`encounter-tracker-item ${combatant.actor_type} ${isActiveItem ? 'active' : ''}`}
                    style={{ display: 'flex', alignItems: 'center', gap: '6px', background: isActiveItem ? 'rgba(245, 158, 11, 0.15)' : 'rgba(255,255,255,0.05)', border: isActiveItem ? '1px solid var(--text-gold)' : '1px solid rgba(255,255,255,0.1)', borderRadius: '16px', padding: '2px 8px', fontSize: '0.75rem' }}
                  >
                    <span className="tracker-avatar" style={{ background: getGradientSeed(combatant.label || 'Combatant'), width: '16px', height: '16px', borderRadius: '50%', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: '9px', fontWeight: 'bold' }}>
                      {combatant.label?.slice(0, 2).toUpperCase() || '?'}
                    </span>
                    <span className="tracker-label">{combatant.label}</span>
                    
                    {hasInitiative ? (
                      <span className="tracker-init-badge" style={{ background: 'rgba(255,255,255,0.15)', borderRadius: '4px', padding: '1px 4px', fontWeight: 'bold' }}>{combatant.initiative}</span>
                    ) : canRollOrInput ? (
                      <div className="tracker-roll-action" style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
                        <button
                          type="button"
                          className="btn btn-primary btn-roll-init"
                          style={{ padding: '2px 6px', fontSize: '9px' }}
                          onClick={(e) => {
                            e.stopPropagation()
                            handleRollInitiative(combatant)
                          }}
                          disabled={rollingInitId === combatant.placement_id}
                        >
                          🎲 Roll
                        </button>
                        <input
                          type="number"
                          className="initiative-manual-input"
                          placeholder="or..."
                          value={manualInitValues[`${combatant.actor_type}-${combatant.actor_id}`] ?? ''}
                          onClick={(e) => e.stopPropagation()}
                          style={{ width: '36px', padding: '1px 3px', fontSize: '9px', background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.1)', color: '#fff', borderRadius: '3px' }}
                          onChange={(e) => {
                            const val = e.target.value
                            setManualInitValues(prev => ({
                              ...prev,
                              [`${combatant.actor_type}-${combatant.actor_id}`]: val
                            }))
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              handleManualInitiativeSubmit(combatant, e.target.value)
                            }
                          }}
                        />
                      </div>
                    ) : (
                      <span className="tracker-init-badge" title="Waiting for initiative roll">⏳</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div className="encounter-combat-tracker-right" style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            {activeCombatant ? (
              <div className="active-combatant-details" style={{ display: 'flex', gap: '4px', alignItems: 'center', fontSize: '0.75rem' }}>
                <span className="active-combatant-name" style={{ color: 'var(--text-gold)', fontWeight: 'bold' }}>
                  ⚔️ {activeCombatant.label}
                </span>
                <span className="active-combatant-movement" style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>
                  {activeCombatant.actions?.movement_remaining ?? 0} ft
                </span>
              </div>
            ) : (
              <span className="active-combatant-name" style={{ fontSize: '0.75rem', color: 'var(--text-dim)' }}>Waiting...</span>
            )}
            
            <div className="encounter-combat-controls">
              {(isUserActiveTurn || campaign.user_id === currentUser?.id) && (
                <button
                  type="button"
                  className="btn btn-primary btn-end-turn"
                  style={{ padding: '4px 10px', fontSize: '11px', fontWeight: 'bold' }}
                  onClick={handleNextTurn}
                  disabled={activeTurnIndex === null || activeTurnIndex === undefined}
                >
                  End Turn
                </button>
              )}
            </div>
          </div>
        </div>
      )}

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
            active={view === 'map'}
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

import { useEffect, useMemo, useState } from 'react'
import { getCampaigns, getCurrentEncounterMap, getEncounterMapImage } from '../api/client'
import DesignLabStoryAtlasAdapter from '../components/story-atlas/DesignLabStoryAtlasAdapter'
import './DesignLabPage.css'
import './DesignLabDirections.css'

const DIRECTIONS = [
  { id: 'hearth', number: '01', name: 'Story + Atlas', note: 'Story-first exploration, map-first combat' },
  { id: 'atlas', number: '02', name: 'Living Atlas', note: 'The scene becomes a canvas' },
  { id: 'chronicle', number: '03', name: 'The Chronicle', note: 'An editorial campaign book' },
  { id: 'theatre', number: '04', name: 'Theatre Mode', note: 'One dramatic beat at a time' },
  { id: 'command', number: '05', name: 'Command Deck', note: 'Dense tactical operations' },
]

const SCENARIOS = [
  { id: 'ready', label: 'Table ready', icon: 'bi-cup-hot' },
  { id: 'live', label: 'Live story', icon: 'bi-chat-square-text' },
  { id: 'thinking', label: 'AI responding', icon: 'bi-stars' },
  { id: 'roll', label: 'Dice result', icon: 'bi-dice-5' },
  { id: 'proposal', label: 'Sheet update', icon: 'bi-journal-check' },
  { id: 'encounter', label: 'Encounter map', icon: 'bi-map' },
]

const PARTY = [
  { initials: 'BT', name: 'Brixby Tinkertop', detail: 'Gnome Bard · Level 3', hp: '22 / 22', color: '#cc5075' },
  { initials: 'VA', name: 'Vesper Ash', detail: 'Tiefling Rogue · Level 3', hp: '18 / 21', color: '#8367c7' },
  { initials: 'OR', name: 'Orren Reed', detail: 'Human Cleric · Level 3', hp: '26 / 26', color: '#4e8c76' },
]

function useAccountMap() {
  const [map, setMap] = useState({ url: '', title: '', campaign: '', status: 'loading' })

  useEffect(() => {
    let active = true
    let objectUrl = ''

    const load = async () => {
      try {
        const campaignData = await getCampaigns()
        const campaigns = campaignData.campaigns || []
        for (const campaign of campaigns) {
          const mapData = await getCurrentEncounterMap(campaign.id).catch(() => ({ encounter_map: null }))
          if (!mapData.encounter_map) continue
          const blob = await getEncounterMapImage(mapData.encounter_map.id)
          objectUrl = URL.createObjectURL(blob)
          if (active) {
            setMap({
              url: objectUrl,
              title: mapData.encounter_map.title,
              campaign: campaign.name,
              status: 'ready',
            })
          }
          return
        }
        if (active) setMap((current) => ({ ...current, status: 'empty' }))
      } catch {
        if (active) setMap((current) => ({ ...current, status: 'empty' }))
      }
    }

    load()
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [])

  return map
}

function PartyRail({ compact = false }) {
  return (
    <aside className={`sampler-party ${compact ? 'is-compact' : ''}`}>
      <div className="sampler-campaign-mark"><span>✦</span><div><strong>The Oath Below Emberfield</strong><small>Chapter III · Ashglass Market</small></div></div>
      <div className="sampler-party-label">At the table <span>3</span></div>
      <div className="sampler-party-list">
        {PARTY.map((member) => (
          <div className="sampler-party-member" key={member.name}>
            <span className="sampler-avatar" style={{ background: member.color }}>{member.initials}</span>
            <div><strong>{member.name}</strong><small>{member.detail}</small><em>{member.hp} HP</em></div>
          </div>
        ))}
      </div>
      <nav className="sampler-nav"><button><i className="bi bi-person-badge" /> Characters</button><button><i className="bi bi-book" /> Chronicle</button><button><i className="bi bi-gear" /> Campaign settings</button></nav>
      <div className="sampler-profile"><span>P</span><div><strong>phazedrl</strong><small>Campaign owner</small></div><i className="bi bi-three-dots" /></div>
    </aside>
  )
}

function StoryMessages({ scenario }) {
  if (scenario === 'ready') {
    return <div className="sampler-empty"><span>✦</span><small>THE NEXT CHAPTER</small><h2>Your table is ready.</h2><p>Gather the party, review the last scene, and begin when everyone is settled.</p><button>Start session <i className="bi bi-arrow-right" /></button></div>
  }

  return (
    <div className="sampler-messages">
      <article className="sampler-message dm"><span className="sampler-message-avatar">✦</span><div><header><strong>AI Dungeon Master</strong><time>6:11 PM</time></header><p>The late-morning sun bakes Ashglass Market Square, but the usual trade has ground to a halt. Every eye is fixed on the central platform where Baron Thorne points an accusing finger at the village healer.</p><blockquote><small>BARON THORNE</small>“The grain went bad in her hands. This sickness started here.”</blockquote><p>The crowd waits for someone to break the tension. What does Brixby do?</p></div></article>
      <article className="sampler-message player"><span className="sampler-avatar" style={{ background: '#cc5075' }}>BT</span><div><header><strong>Brixby Tinkertop</strong><time>6:13 PM</time></header><p>“Before we condemn anyone, perhaps we should ask what spoiled grain smells like.” I move toward the sacks and look for signs of alchemy.</p></div></article>
      {scenario === 'thinking' && <div className="sampler-thinking"><span><i /><i /><i /></span><div><strong>The AI Dungeon Master is responding</strong><small>Checking the scene, your character, and recent events</small></div></div>}
      {scenario === 'roll' && <div className="sampler-event roll"><span><i className="bi bi-dice-5-fill" /></span><div><small>INVESTIGATION CHECK</small><strong>18</strong><p>14 on the die <b>+4</b> modifier</p></div><em>Success</em></div>}
      {scenario === 'proposal' && <div className="sampler-event proposal"><span><i className="bi bi-journal-plus" /></span><div><small>SHEET UPDATE</small><strong>New clue added</strong><p>Alchemical residue found on the grain sacks.</p></div><button>Review</button></div>}
    </div>
  )
}

function MapStage({ map, immersive = false }) {
  return (
    <div className={`sampler-map ${immersive ? 'is-immersive' : ''}`}>
      {map.url ? <img src={map.url} alt={`${map.title} campaign map`} /> : <div className="sampler-map-fallback"><i className="bi bi-map" /><span>{map.status === 'loading' ? 'Finding a map from your campaigns…' : 'No active campaign map found'}</span></div>}
      <div className="sampler-map-shade" />
      <div className="sampler-map-title"><small>{map.campaign || 'CAMPAIGN MAP'}</small><strong>{map.title || 'Current encounter'}</strong></div>
      <div className="sampler-map-tools"><button aria-label="Center map"><i className="bi bi-crosshair" /></button><button aria-label="Zoom in"><i className="bi bi-plus-lg" /></button><button aria-label="Map layers"><i className="bi bi-layers" /></button></div>
      <span className="sampler-token token-a">BT</span><span className="sampler-token token-b">VA</span><span className="sampler-token token-c">OR</span>
      <div className="sampler-objective"><small>CURRENT OBJECTIVE</small><strong>Inspect the foundry floor</strong><span>Round 2 · Brixby is up</span></div>
    </div>
  )
}

function ContextRail({ scenario, map, onOpenMap }) {
  return (
    <aside className="sampler-context">
      <section className="sampler-scene"><small>CURRENT SCENE</small><h3>Ashglass Market</h3><p>Morning · Crowded · Tense</p>{scenario === 'encounter' ? <button onClick={onOpenMap}><span>{map.url ? <img src={map.url} alt="" /> : <i className="bi bi-map" />}</span><div><strong>{map.title || 'Combat map'}</strong><small>{map.campaign || 'Open tactical view'}</small></div><i className="bi bi-arrow-up-right" /></button> : <div className="sampler-scene-state"><i className="bi bi-signpost-split" /><div><strong>Exploration in progress</strong><small>No combat map active</small></div></div>}</section>
      <section><header><small>STORY THREADS</small><span>2 active</span></header><div className="sampler-thread"><i /><div><strong>The blighted grain</strong><small>Find the source before dusk</small></div></div><div className="sampler-thread"><i /><div><strong>Baron Thorne’s claim</strong><small>Unverified accusation</small></div></div></section>
      <section><header><small>RECENT ACTIVITY</small></header>{scenario === 'roll' ? <p>Brixby rolled Investigation <strong>18</strong></p> : scenario === 'proposal' ? <p>Character sheet update waiting</p> : <p className="muted">Important rolls and updates will collect here.</p>}</section>
    </aside>
  )
}

function ScenarioAccent({ scenario }) {
  if (scenario === 'thinking') return <div className="sampler-thinking"><span><i /><i /><i /></span><div><strong>The AI Dungeon Master is responding</strong><small>Checking the scene, your character, and recent events</small></div></div>
  if (scenario === 'roll') return <div className="sampler-event roll"><span><i className="bi bi-dice-5-fill" /></span><div><small>INVESTIGATION CHECK</small><strong>18</strong><p>14 on the die <b>+4</b> modifier</p></div><em>Success</em></div>
  if (scenario === 'proposal') return <div className="sampler-event proposal"><span><i className="bi bi-journal-plus" /></span><div><small>SHEET UPDATE</small><strong>New clue added</strong><p>Alchemical residue found on the grain sacks.</p></div><button>Review</button></div>
  if (scenario === 'ready') return <div className="sampler-ready-line"><i className="bi bi-cup-hot" /><div><strong>The table is ready</strong><small>Three adventurers are gathered</small></div><button>Begin</button></div>
  return null
}

function HearthWorkspace({ scenario, map }) {
  const [view, setView] = useState(scenario === 'encounter' ? 'map' : 'story')
  const showMap = view === 'map'
  if (showMap) return <AtlasWorkspace scenario={scenario} map={map} onShowStory={() => setView('story')} />
  return (
    <section className={`sampler-workspace direction-hearth ${showMap ? 'is-map' : ''}`}>
      <PartyRail />
      <main className="sampler-main">
        <header className="sampler-topbar"><div><span className={scenario === 'ready' ? '' : 'is-live'}><i />{scenario === 'ready' ? 'TABLE READY' : 'SESSION LIVE'}</span><strong>Ashglass Market</strong><small>{scenario === 'encounter' ? 'Combat' : 'Exploration'}</small></div><nav><button className={!showMap ? 'active' : ''} onClick={() => setView('story')}><i className="bi bi-chat-square-text" /> Story</button>{scenario === 'encounter' && <button className={showMap ? 'active' : ''} onClick={() => setView('map')}><i className="bi bi-map" /> Map</button>}</nav><button className="sampler-end">{scenario === 'ready' ? 'Start session' : 'End session'}</button></header>
        <div className="sampler-stage">{showMap ? <MapStage map={map} /> : <StoryMessages scenario={scenario} />}</div>
        {!showMap && scenario !== 'ready' && <footer className="sampler-composer"><button aria-label="Roll dice"><i className="bi bi-dice-5" /></button><div><span>What does Brixby do?</span><small><kbd>/</kbd> commands · <kbd>⇧↵</kbd> new line</small></div><button className="send">Send <i className="bi bi-arrow-up" /></button></footer>}
      </main>
      <ContextRail scenario={scenario} map={map} onOpenMap={() => setView('map')} />
    </section>
  )
}

function SceneBoard() {
  return (
    <div className="atlas-scene-board">
      <div className="atlas-scene-label"><small>EXPLORATION SCENE</small><strong>Ashglass Market</strong><span>4 active elements · morning</span></div>
      <i className="scene-link link-one" /><i className="scene-link link-two" /><i className="scene-link link-three" />
      <article className="scene-node scene-center"><small>LOCATION</small><strong>Market square</strong><span>Crowded · tense</span></article>
      <article className="scene-node scene-thorne"><small>NPC</small><strong>Baron Thorne</strong><span>Accusing the healer</span></article>
      <article className="scene-node scene-mira"><small>NPC</small><strong>Mira Stoneglen</strong><span>Silent · pleading</span></article>
      <article className="scene-node scene-clue"><small>CLUE</small><strong>Blighted grain</strong><span>Sharp chemical odor</span></article>
      <div className="scene-board-note"><i className="bi bi-stars" /><span>The AI Dungeon Master updates this board as the scene changes.</span></div>
    </div>
  )
}

function AtlasWorkspace({ scenario, map, onShowStory }) {
  const isEncounter = scenario === 'encounter'
  return (
    <section className="atlas-workspace">
      {isEncounter ? <MapStage map={map} immersive /> : <SceneBoard />}
      <header className="atlas-header"><div><span>✦</span><strong>The Oath Below Emberfield</strong><small>{isEncounter ? 'Combat map · Round 2' : `Scene board · ${scenario === 'ready' ? 'Table ready' : 'Exploration'}`}</small></div>{onShowStory && <nav className="atlas-hybrid-tabs"><button onClick={onShowStory}><i className="bi bi-chat-square-text" /> Story</button><button className="active"><i className="bi bi-map" /> Map</button></nav>}<button>End session</button></header>
      <nav className="atlas-tools" aria-label="Map tools"><button className="active"><i className="bi bi-cursor" /></button><button><i className="bi bi-rulers" /></button><button><i className="bi bi-layers" /></button><button><i className="bi bi-eye" /></button><span /><button><i className="bi bi-gear" /></button></nav>
      <aside className="atlas-feed"><header><div><small>LIVE FEED</small><strong>Ashglass Market</strong></div><button><i className="bi bi-layout-sidebar-reverse" /></button></header><div className="atlas-feed-scroll"><article><span>✦</span><div><small>AI DUNGEON MASTER · 6:11 PM</small><p>The square holds its breath. Baron Thorne’s accusation hangs above the crowd while a sharp chemical tang rises from the grain.</p></div></article><article><span>BT</span><div><small>BRIXBY · 6:13 PM</small><p>“Before we condemn anyone, perhaps we should inspect the evidence.”</p></div></article><ScenarioAccent scenario={scenario} /></div><footer><button><i className="bi bi-dice-5" /></button><span>Describe your move…</span><button className="send"><i className="bi bi-arrow-up" /></button></footer></aside>
      <div className="atlas-party-dock">{PARTY.map((member) => <div key={member.name}><span style={{ background: member.color }}>{member.initials}</span><small>{member.hp}</small></div>)}</div>
    </section>
  )
}

function ChronicleWorkspace({ scenario, map }) {
  const [showMap, setShowMap] = useState(scenario === 'encounter')
  return (
    <section className={`chronicle-workspace ${showMap ? 'show-map' : ''}`}>
      <header className="chronicle-masthead"><div><span>THE CHRONICLE OF</span><strong>Emberfield</strong></div><nav><button className={!showMap ? 'active' : ''} onClick={() => setShowMap(false)}>Chapter</button>{scenario === 'encounter' && <button className={showMap ? 'active' : ''} onClick={() => setShowMap(true)}>Battle map</button>}</nav><div><small>CHAPTER III</small><button>•••</button></div></header>
      <aside className="chronicle-index"><span>III</span><small>THE OATH BELOW</small><nav><button className="active">12</button><button>13</button><button>14</button></nav></aside>
      <main className="chronicle-sheet">
        {showMap ? <div className="chronicle-foldout"><MapStage map={map} /><span>PLATE VII · THE BROKEN BELL FOUNDRY</span></div> : <><div className="chronicle-chapter-kicker">SCENE TWELVE · ASHGLASS MARKET</div><h1>An accusation<br />in the morning sun.</h1><p className="chronicle-deck">The market has fallen silent. A healer stands accused while something unnatural spoils the grain.</p><div className="chronicle-rule" /><div className="chronicle-copy"><p><span>T</span>he late-morning sun bakes the market square, but the usual trade has ground to a halt. Every eye is fixed on Baron Thorne and the village healer.</p><blockquote>“The grain went bad in her hands. This sickness started here.”<small>— BARON THORNE</small></blockquote><p>Near the sacks, a sharp chemical tang cuts through dust and produce. The crowd waits for Brixby to act.</p></div><ScenarioAccent scenario={scenario} /><div className="chronicle-response"><span>Continue the chapter…</span><button><i className="bi bi-arrow-right" /></button></div></>}
      </main>
      <aside className="chronicle-margin"><section><small>AT THE TABLE</small>{PARTY.map((member) => <div key={member.name}><span style={{ background: member.color }}>{member.initials}</span><p><strong>{member.name}</strong><small>{member.hp} HP</small></p></div>)}</section><section><small>MARGIN NOTES</small><p><i /> The grain carries an alchemical odor.</p><p><i /> Thorne’s claim is still unverified.</p></section><footer>Recorded at 6:13 PM</footer></aside>
    </section>
  )
}

function TheatreWorkspace({ scenario, map }) {
  const [showMap, setShowMap] = useState(scenario === 'encounter')
  return (
    <section className={`theatre-workspace ${showMap ? 'show-map' : ''}`}>
      <div className="theatre-backdrop">{scenario === 'encounter' && map.url ? <img src={map.url} alt="Combat map backdrop" /> : <div className="theatre-scene-art"><span className="theatre-sun" /><i className="crowd crowd-one" /><i className="crowd crowd-two" /><i className="crowd crowd-three" /></div>}<i /></div>
      <header className="theatre-header"><strong>✦ Fireside</strong><div><span className={scenario === 'ready' ? '' : 'live'}><i /> {scenario === 'ready' ? 'TABLE READY' : 'LIVE'}</span>{scenario === 'encounter' && <button onClick={() => setShowMap(!showMap)}><i className={`bi bi-${showMap ? 'chat-square-text' : 'map'}`} /> {showMap ? 'Story' : 'Map'}</button>}<button className="end">End</button></div></header>
      {showMap ? <div className="theatre-map"><MapStage map={map} immersive /></div> : <main className="theatre-beat"><small>ASHGLASS MARKET · MORNING</small><h1>The square<br />holds its breath.</h1><p>Baron Thorne points toward the healer. Beneath the shouting, Brixby catches the unmistakable scent of alchemy.</p><blockquote><span>BARON THORNE</span>“This sickness started here, in her hands.”</blockquote></main>}
      {!showMap && <div className="theatre-dialogue"><div className="theatre-speaker"><span style={{ background: '#cc5075' }}>BT</span><div><small>YOUR TURN</small><strong>What does Brixby do?</strong></div></div><ScenarioAccent scenario={scenario} /><div className="theatre-input"><button><i className="bi bi-dice-5" /></button><span>Speak, act, or ask a question…</span><button><i className="bi bi-arrow-up" /></button></div></div>}
      <div className="theatre-party">{PARTY.map((member) => <span key={member.name} style={{ background: member.color }}>{member.initials}<i /></span>)}</div>
    </section>
  )
}

function CommandWorkspace({ scenario, map }) {
  const [showMap, setShowMap] = useState(scenario === 'encounter')
  return (
    <section className="command-workspace">
      <header className="command-header"><div><span className="command-mark">F</span><strong>THE OATH BELOW EMBERFIELD</strong><small>/ ASHGLASS MARKET</small></div><div className="command-status"><span><i /> SESSION LIVE</span><b>00:42:18</b>{scenario === 'encounter' && <button onClick={() => setShowMap(!showMap)}><i className={`bi bi-${showMap ? 'chat-square-text' : 'map'}`} /> {showMap ? 'FEED' : 'MAP'}</button>}<button>END</button></div></header>
      <aside className="command-left"><section><small>CURRENT OBJECTIVE</small><h3>Trace the blighted grain</h3><p>Find the source before the market closes.</p><div><span>PROGRESS</span><b>2 / 5</b></div></section><section><small>TURN ORDER</small>{PARTY.map((member, index) => <div className={index === 0 ? 'active' : ''} key={member.name}><b>0{index + 1}</b><span style={{ background: member.color }}>{member.initials}</span><p><strong>{member.name}</strong><small>{member.hp} HP</small></p><em>{17 - index * 3}</em></div>)}</section><section><small>SCENE CLOCK</small><div className="command-clock"><i /><i /><i /><i /><i /><i /></div><p>Thorne loses patience · 4 / 6</p></section></aside>
      <main className="command-center">{showMap ? <MapStage map={map} immersive /> : <><div className="command-feed-title"><span>EVENT FEED</span><div><button>ALL</button><button>STORY</button><button>ROLLS</button></div></div><StoryMessages scenario={scenario} /><footer className="command-input"><button><i className="bi bi-lightning-charge" /> ACTION</button><span>Enter a command or describe your move…</span><button><i className="bi bi-arrow-return-left" /></button></footer></>}</main>
      <aside className="command-right"><section><small>QUICK ACTIONS</small><div className="command-actions"><button><i className="bi bi-eye" /> Inspect</button><button><i className="bi bi-chat-quote" /> Persuade</button><button><i className="bi bi-person-walking" /> Move</button><button><i className="bi bi-dice-5" /> Roll</button></div></section><section><small>PARTY TELEMETRY</small>{PARTY.map((member) => <div className="command-party-row" key={member.name}><span style={{ background: member.color }}>{member.initials}</span><p><strong>{member.name}</strong><small>{member.detail}</small></p><em>{member.hp}</em></div>)}</section><section><small>LIVE SIGNAL</small><ScenarioAccent scenario={scenario} /><p className="command-log">06:13 · Chemical residue detected</p><p className="command-log">06:11 · Scene advanced</p></section></aside>
    </section>
  )
}

function WorkspacePreview({ direction, scenario, map }) {
  if (direction === 'hearth' || direction === 'atlas') {
    return <DesignLabStoryAtlasAdapter scenario={scenario} map={map} />
  }
  if (direction === 'chronicle') return <ChronicleWorkspace scenario={scenario} map={map} />
  if (direction === 'theatre') return <TheatreWorkspace scenario={scenario} map={map} />
  if (direction === 'command') return <CommandWorkspace scenario={scenario} map={map} />
  return <DesignLabStoryAtlasAdapter scenario={scenario} map={map} />
}

export default function DesignLabPage() {
  const [direction, setDirection] = useState('hearth')
  const [scenario, setScenario] = useState('encounter')
  const map = useAccountMap()
  const activeDirection = useMemo(() => DIRECTIONS.find((item) => item.id === direction), [direction])

  return (
    <div className="session-sampler-page">
      <header className="sampler-page-header"><div><span>SESSION WORKSPACE SAMPLER</span><h1>Five ways to run the table.</h1><p>Compare five distinct interaction models across the states a real campaign moves through.</p></div><div className="sampler-map-source"><i className="bi bi-map" /><div><small>COMBAT MAP SOURCE</small><strong>{map.status === 'loading' ? 'Looking through your campaigns…' : map.url ? `${map.campaign} · ${map.title}` : 'No active map available'}</strong></div></div></header>
      <div className="sampler-controls">
        <div className="sampler-direction-tabs" role="tablist" aria-label="Layout directions">{DIRECTIONS.map((item) => <button key={item.id} className={direction === item.id ? 'active' : ''} onClick={() => setDirection(item.id)} role="tab" aria-selected={direction === item.id}><span>{item.number}</span><div><strong>{item.name}</strong><small>{item.note}</small></div></button>)}</div>
        <div className="sampler-scenarios"><span>Mocked scenario</span><div>{SCENARIOS.map((item) => <button key={item.id} className={scenario === item.id ? 'active' : ''} onClick={() => setScenario(item.id)}><i className={`bi ${item.icon}`} />{item.label}</button>)}</div></div>
      </div>
      <div className="sampler-preview-header"><div><span>{activeDirection.number} / {activeDirection.name}</span><strong>{SCENARIOS.find((item) => item.id === scenario)?.label}</strong></div><p>Maps appear only in the Encounter map scenario. Exploration states use story, scene, and relationship context instead.</p></div>
      <div className="sampler-frame"><WorkspacePreview key={`${direction}-${scenario}`} direction={direction} scenario={scenario} map={map} /></div>
    </div>
  )
}

import { useState } from 'react'
import './DesignLabPage.css'

const DIRECTIONS = [
  { id: 'ember', number: '01', name: 'Ember & Ink', label: 'Cinematic campaign table', description: 'A dramatic, image-led home for an active story.' },
  { id: 'chronicle', number: '02', name: 'The Chronicle', label: 'Editorial fantasy archive', description: 'Warm paper, quiet authority, and the feeling of a treasured volume.' },
  { id: 'watch', number: '03', name: 'Night Watch', label: 'Tactical operations room', description: 'High-information play space for sessions, maps, and decisions.' },
  { id: 'wildwood', number: '04', name: 'Wildwood', label: 'Illustrated storybook', description: 'A vivid, welcoming direction with an organic living-world feel.' },
  { id: 'ledger', number: '05', name: 'The Ledger', label: 'Quiet premium minimalism', description: 'A precise, modern interface where the campaign content carries the emotion.' },
]

function LabSwitcher({ activeId, onSelect }) {
  const active = DIRECTIONS.find((direction) => direction.id === activeId)

  return (
    <aside className="design-lab-switcher" aria-label="Design directions">
      <div className="design-lab-brand">
        <span className="design-lab-mark">D</span>
        <div>
          <span>Design explorations</span>
          <strong>Adventure</strong>
        </div>
      </div>
      <div className="design-lab-intro">
        <span className="design-lab-eyebrow">Choose a direction</span>
        <p>Five complete visual systems for the next version of the product.</p>
      </div>
      <div className="design-lab-options" role="tablist" aria-label="Design direction options">
        {DIRECTIONS.map((direction) => (
          <button
            key={direction.id}
            className={`design-lab-option ${activeId === direction.id ? 'is-active' : ''}`}
            onClick={() => onSelect(direction.id)}
            role="tab"
            aria-selected={activeId === direction.id}
          >
            <span>{direction.number}</span>
            <div>
              <strong>{direction.name}</strong>
              <small>{direction.label}</small>
            </div>
          </button>
        ))}
      </div>
      <div className="design-lab-footer">
        <span>Currently viewing</span>
        <strong>{active.name}</strong>
      </div>
    </aside>
  )
}

function EmberPreview() {
  return (
    <section className="direction-preview ember-preview">
      <nav className="ember-nav">
        <div className="ember-wordmark"><span>✦</span> Tallowmere</div>
        <div className="ember-nav-links"><span className="is-current">Campaign</span><span>Characters</span><span>Archive</span></div>
        <button className="ember-profile">AK <i className="bi bi-chevron-down" /></button>
      </nav>
      <div className="ember-hero">
        <div className="ember-hero-copy">
          <span className="ember-kicker">CHAPTER IV · THE ASHEN ROAD</span>
          <h1>Where embers<br />remember names.</h1>
          <p>The party has found the abandoned relay beyond Bracken Pass. Rain starts at sundown.</p>
          <div className="ember-actions"><button>Resume session <i className="bi bi-arrow-up-right" /></button><button className="ember-ghost">View chronicle</button></div>
        </div>
        <div className="ember-vignette" />
        <div className="ember-scene-pill"><i className="bi bi-geo-alt-fill" /> Bracken Pass <span>·</span> 3 players online</div>
      </div>
      <div className="ember-bottom-row">
        <div className="ember-roster">
          <span className="ember-label">At the table</span>
          <div className="ember-avatars"><b>SD</b><b>MV</b><b>KL</b><button>+</button></div>
        </div>
        <div className="ember-note"><span className="ember-label">Last beat</span><p>“The door opens inward.”</p></div>
        <button className="ember-map-link"><i className="bi bi-map" /> Tactical view</button>
      </div>
    </section>
  )
}

function ChroniclePreview() {
  return (
    <section className="direction-preview chronicle-preview">
      <aside className="chronicle-spine"><span>TALES FROM TALLOWMERE</span><i>IV</i><small>2026</small></aside>
      <main className="chronicle-page">
        <header className="chronicle-header"><span className="chronicle-seal">T</span><div><small>The Chronicle of</small><strong>Tallowmere</strong></div><button><i className="bi bi-three-dots" /></button></header>
        <div className="chronicle-rule" />
        <div className="chronicle-grid">
          <div className="chronicle-title-area"><span>THE ADVENTURE CONTINUES</span><h1>THE ASHEN<br />ROAD</h1><p>Three companions. One sealed relay. A storm gathering across the high country.</p><button>Open chapter <i className="bi bi-arrow-right" /></button></div>
          <div className="chronicle-illustration"><div className="chronicle-sun" /><div className="chronicle-hill hill-one" /><div className="chronicle-hill hill-two" /><div className="chronicle-rider">♞</div></div>
        </div>
        <footer className="chronicle-footer"><div><small>LAST PLAYED</small><strong>July 8, 2026</strong></div><div><small>COMPANIONS</small><strong>03 of 05 gathered</strong></div><div><small>CURRENT LOCATION</small><strong>Bracken Pass</strong></div></footer>
      </main>
    </section>
  )
}

function WatchPreview() {
  return (
    <section className="direction-preview watch-preview">
      <aside className="watch-rail"><div className="watch-symbol">A</div><button className="is-active"><i className="bi bi-grid-1x2" /></button><button><i className="bi bi-chat-square-text" /></button><button><i className="bi bi-map" /></button><button><i className="bi bi-people" /></button><span /><button><i className="bi bi-gear" /></button></aside>
      <div className="watch-main">
        <header className="watch-header"><div><span>LIVE SESSION</span><strong>Tallowmere / Ashen Road</strong></div><div className="watch-live"><i /> DM responding <button>End session</button></div></header>
        <div className="watch-stage"><div className="watch-map-grid" /><div className="watch-map-label">BRACKEN PASS<span>high country relay · exterior</span></div><div className="watch-map-token token-one">S</div><div className="watch-map-token token-two">M</div><div className="watch-map-token token-three">K</div><div className="watch-objective"><small>OBJECTIVE</small><strong>Reach the relay</strong><span>Rain begins in 18 min</span></div></div>
        <div className="watch-command"><span><i className="bi bi-lightning-charge" /> Your move</span><p>Describe an action, speak in character, or type <kbd>/</kbd> for commands.</p><button><i className="bi bi-arrow-up" /></button></div>
      </div>
      <aside className="watch-inspector"><header><span>SCENE INTELLIGENCE</span><button><i className="bi bi-x" /></button></header><section><small>INITIATIVE</small><div className="watch-turn"><b>01</b><span>Seraphina Duskweaver</span><i>17</i></div><div className="watch-turn"><b>02</b><span>Milo Venn</span><i>14</i></div><div className="watch-turn muted"><b>03</b><span>Rain has not started</span><i>—</i></div></section><section><small>PARTY CONDITION</small><div className="watch-stat"><span>Momentum</span><strong>Steady</strong></div><div className="watch-stat"><span>Supplies</span><strong>Low</strong></div></section></aside>
    </section>
  )
}

function WildwoodPreview() {
  return (
    <section className="direction-preview wildwood-preview">
      <nav className="wildwood-nav"><strong><i>✺</i> Fireside</strong><div><span>My stories</span><span>Characters</span><span>Discover</span></div><button>+ New story</button></nav>
      <div className="wildwood-hero"><div className="wildwood-spark spark-one">✦</div><div className="wildwood-spark spark-two">✧</div><div className="wildwood-copy"><span>WELCOME BACK, ADELINE</span><h1>There’s a story<br />waiting for you.</h1><p>Tallowmere is paused at Bracken Pass, just before the rain.</p><button>Step back in <i className="bi bi-arrow-right" /></button></div><div className="wildwood-portal"><div className="wildwood-moon" /><div className="wildwood-trees" /><div className="wildwood-figure">♙</div></div></div>
      <div className="wildwood-shelf"><div><span>Your stories</span><h2>Choose a world</h2></div><div className="wildwood-story is-featured"><i>✺</i><strong>Tallowmere</strong><small>Chapter IV · Continue</small></div><div className="wildwood-story"><i>☾</i><strong>Blood &amp; Briar</strong><small>Chapter I · Paused</small></div><button className="wildwood-more">View all <i className="bi bi-arrow-right" /></button></div>
    </section>
  )
}

function LedgerPreview() {
  return (
    <section className="direction-preview ledger-preview">
      <header className="ledger-header"><strong>ADVENTURE</strong><nav><span className="is-current">Campaigns</span><span>Characters</span><span>Automation</span></nav><div><button className="ledger-search"><i className="bi bi-search" /> Search</button><span className="ledger-user">AK</span></div></header>
      <main className="ledger-main"><header><div><span>CAMPAIGNS / 03</span><h1>Good evening, Adeline.</h1></div><button className="ledger-new">New campaign <i className="bi bi-plus" /></button></header><div className="ledger-divider" />
        <div className="ledger-list"><article className="ledger-row is-primary"><div className="ledger-index">01</div><div className="ledger-name"><span className="ledger-dot" /> <strong>Tallowmere</strong><small>Chapter IV · The Ashen Road</small></div><p>The relay at Bracken Pass is open. The party is sheltering from the storm.</p><div className="ledger-status"><i /> In progress</div><button><i className="bi bi-arrow-up-right" /></button></article><article className="ledger-row"><div className="ledger-index">02</div><div className="ledger-name"><span className="ledger-dot blue" /> <strong>Blood &amp; Briar</strong><small>Chapter I · The Unquiet Orchard</small></div><p>A quiet village, a crooked harvest, and a promise made after midnight.</p><div className="ledger-status is-paused">Paused</div><button><i className="bi bi-arrow-up-right" /></button></article><article className="ledger-row"><div className="ledger-index">03</div><div className="ledger-name"><span className="ledger-dot grey" /> <strong>The Long Return</strong><small>Prologue · The Fjord</small></div><p>A shipwrecked crew makes landfall under a pale northern sun.</p><div className="ledger-status is-draft">Draft</div><button><i className="bi bi-arrow-up-right" /></button></article></div>
      </main>
      <footer className="ledger-footer"><span>ALL SYSTEMS NOMINAL</span><span>3 campaigns · 11 characters · 28 archived sessions</span><span>⌘K for command menu</span></footer>
    </section>
  )
}

function Preview({ activeId }) {
  if (activeId === 'chronicle') return <ChroniclePreview />
  if (activeId === 'watch') return <WatchPreview />
  if (activeId === 'wildwood') return <WildwoodPreview />
  if (activeId === 'ledger') return <LedgerPreview />
  return <EmberPreview />
}

export default function DesignLabPage() {
  const [activeId, setActiveId] = useState('ember')
  const active = DIRECTIONS.find((direction) => direction.id === activeId)

  return (
    <div className="design-lab-page">
      <LabSwitcher activeId={activeId} onSelect={setActiveId} />
      <main className="design-lab-canvas">
        <header className="design-lab-canvas-header"><div><span className="design-lab-eyebrow">{active.number} / {active.label}</span><h1>{active.name}</h1></div><p>{active.description}</p></header>
        <div className={`design-lab-frame design-lab-frame-${activeId}`} role="tabpanel"><Preview activeId={activeId} /></div>
        <p className="design-lab-hint"><i className="bi bi-arrow-left" /> Use the switcher to compare five complete directions.</p>
      </main>
    </div>
  )
}

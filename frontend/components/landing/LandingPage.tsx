'use client'

import Link from 'next/link'
import './landing.css'

export default function LandingPage() {
  return (
    <div className="v4">

      {/* ── Hero: asymmetric 50/50 cascade ── */}
      <section className="v4-hero" aria-labelledby="v4-hero-title">
        <div className="v4-hero-grid">
          <div className="v4-hero-copy">
            <h1 id="v4-hero-title">
              Friends around
              <br />
              the fire.
              <br />
              <span className="v4-accent">Adventure everywhere else.</span>
            </h1>

            <p className="v4-lede">
              Fireside is the AI tabletop workspace where your campaign lives - persistent worlds, a DM who
              remembers every chapter, and a table that&apos;s always ready.
            </p>

            <div className="v4-hero-actions">
              <Link href="/login" className="btn btn-primary v4-cta-primary">
                <i className="bi bi-stars" aria-hidden="true" /> Start your campaign
              </Link>
              <Link href="/login" className="btn btn-secondary">Sign in</Link>
            </div>

            <div className="v4-hero-meta">
              <span>D&amp;D 5e ready</span>
              <span className="v4-meta-dot" aria-hidden="true">·</span>
              <span>1–6 players + AI companions</span>
            </div>
          </div>

          <div className="v4-hero-stack" aria-hidden="true">
            <div className="v4-hero-card v4-hero-card--top">
              <div className="v4-hero-card-img v4-hero-card-img--campaign" />
              <div className="v4-hero-card-overlay">
                <div className="v4-hero-card-head">
                  <span className="v4-hero-card-kicker"><span className="v4-kicker-dot" /> LIVE SESSION</span>
                  <span className="v4-hero-card-live"><i className="bi bi-record-circle" /> Encounter</span>
                </div>
                <div className="v4-hero-card-lines">
                  <div className="v4-hc-line dm"><span className="v4-hc-who">DM</span><span>The cavern exhales cold. Beyond the embers, something moves.</span></div>
                  <div className="v4-hc-line player"><span className="v4-hc-who">You</span><span>I light my torch and step forward.</span></div>
                </div>
                <div className="v4-hero-card-foot">
                  <span className="v4-hc-dice"><i className="bi bi-dice-5" /> Waiting for roll</span>
                  <span className="v4-hc-memory">Remembers every chapter</span>
                </div>
              </div>
            </div>

            <div className="v4-hero-card v4-hero-card--bottom">
              <div className="v4-hero-card-img v4-hero-card-img--fireside" />
              <div className="v4-hero-card-overlay v4-hero-card-overlay--plain">
                <div className="v4-hero-card-head">
                  <span className="v4-hero-card-kicker"><span className="v4-kicker-dot" style={{ background: 'var(--moss)' }} /> FIRESIDE · LOBBY</span>
                  <span className="v4-hero-card-live" style={{ background: 'var(--surface-canvas)' }}>3 / 6 at table</span>
                </div>
                <p className="v4-hero-card-desc">Gather in the lobby. The fire is lit, the map waits - the AI DM holds the door.</p>
                <div className="v4-hero-card-avatars" aria-hidden="true"><span>TH</span><span>MI</span><span>BR</span><span className="ghost">+3</span></div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Features: masonry waterfall */}
      <section className="v4-section v4-features" id="features" aria-labelledby="v4-features-title">
        <div className="v4-section-head">
          <div>
            <span className="v4-kicker"><span className="v4-kicker-dot" aria-hidden="true" /> WHY FIRESIDE</span>
            <h2 id="v4-features-title">The campaign remembers, so you don&apos;t have to.</h2>
          </div>
          <p>Stop losing notes between sessions. One workspace for your world, your characters, and every decision - with a DM that knows what happened last time. Now laid like paper on a table - slightly scattered, completely held.</p>
        </div>

        <div className="v4-masonry">
          <article className="v4-mason-card v4-mason-tall v4-rotate-a">
            <div className="v4-mason-top"><span className="v4-mason-kicker">01 - THE DM</span><span className="v4-mason-icon ember"><i className="bi bi-stars" /></span></div>
            <h3>The AI Dungeon Master</h3>
            <p>Cinematic, rules-aware, and always present. No scheduling nightmares.</p>
            <ul><li>Knows 5e mechanics &amp; waits for rolls</li><li>Never spoils, never railroads</li><li>Auto summaries pick up where you left off</li></ul>
            <div className="v4-mason-mock">
              <span className="v4-mason-mock-label">Session preview - transcript</span>
              <div className="v4-mock-row"><span className="v4-role dm">DM</span><span>“The bridge groans under the party’s weight… the water below is black glass.”</span></div>
              <div className="v4-mock-row"><span className="v4-role pl">Mira</span><span>“I cast Detect Magic - softly, so it doesn’t hear us.”</span></div>
              <div className="v4-mock-row"><span className="v4-role dm">DM</span><span>“You feel a faint amber thrum beneath the arch… roll Arcana?”</span></div>
              <div className="v4-mock-row muted">→ DM waits for your roll before resolving</div>
              <div className="v4-mock-dice"><i className="bi bi-dice-5" /> 1d20 + 3 - waiting</div>
            </div>
          </article>

          <article className="v4-mason-card v4-rotate-b">
            <div className="v4-mason-top"><span className="v4-mason-kicker">02 - CAMPAIGNS</span><span className="v4-mason-icon moss"><i className="bi bi-book-half" /></span></div>
            <h3>Persistent campaigns</h3>
            <p>One workspace per story. Invite code, history, atlas - one table.</p>
            <ul><li>Invite code sharing</li><li>Recaps &amp; living world state</li><li>Maps, loot, shops</li></ul>
            <div className="v4-mason-mini"><span><i className="bi bi-link-45deg" /> invite: fireside.gg/ember-7a3</span><span><i className="bi bi-people" /> 4 / 6 · 1 AI companion</span></div>
          </article>

          <article className="v4-mason-card v4-mason-short v4-rotate-a">
            <div className="v4-mason-top"><span className="v4-mason-kicker">03 - CHARACTERS</span><span className="v4-mason-icon ink"><i className="bi bi-person-badge" /></span></div>
            <h3>Bring your characters</h3>
            <p>Full folio that travels across campaigns.</p>
            <ul><li>5-step wizard</li><li>Import once, play anywhere</li><li>Proposals keep sheets in sync</li></ul>
          </article>

          <article className="v4-mason-card v4-mason-wide v4-rotate-b">
            <div className="v4-mason-top"><span className="v4-mason-kicker">04 - LIVE PLAY</span><span className="v4-mason-icon gold"><i className="bi bi-lightning-charge" /></span></div>
            <h3>Live at the table</h3>
            <p>Narrative center, party &amp; world at your sides - with turns, spells, and tactical maps.</p>
            <ul><li>Structured turns &amp; spell actions</li><li>Loot &amp; merchants in-fiction</li><li>Solo, co-op, or AI companions</li></ul>
            <div className="v4-mason-stats">
              <div className="v4-mstat"><strong>01:23</strong><span>avg. time to start</span></div>
              <div className="v4-mstat"><strong>∞</strong><span>chapters remembered</span></div>
              <div className="v4-mstat ink"><strong>0</strong><span>human DM needed</span></div>
            </div>
          </article>
        </div>
      </section>

      {/* Breakout: deck of session cards fanned */}
      <section className="v4-breakout" aria-labelledby="v4-breakout-title">
        <div className="v4-breakout-grid">
          <div className="v4-breakout-copy">
            <span className="v4-kicker"><span className="v4-kicker-dot" aria-hidden="true" /> AT THE TABLE</span>
            <h2 id="v4-breakout-title">A table that feels like a table.</h2>
            <p>Not a chat log - a hand of paper. Three sessions fanned across warm light, each one a different chapter held open. Pick one up, the others wait - the DM keeps your place.</p>
            <ul className="v4-breakout-list">
              <li><i className="bi bi-layout-split" aria-hidden="true" /> Three-pane session: party / narrative / world</li>
              <li><i className="bi bi-map" aria-hidden="true" /> Encounter maps with placements &amp; turn order</li>
              <li><i className="bi bi-journal-text" aria-hidden="true" /> Running summary - no “what happened last time?”</li>
            </ul>
            <Link href="/login" className="btn btn-primary v4-breakout-cta"><i className="bi bi-eye" aria-hidden="true" /> Peek at a session</Link>
          </div>

          <div className="v4-deck" aria-hidden="true">
            <div className="v4-deck-card v4-deck-1">
              <div className="v4-deck-bar">fireside.session - The Ember Hollow · Ch. 3</div>
              <div className="v4-deck-body">
                <div className="v4-deck-bubble dm">The hearth spits. From the dark, a low growl answers your torch.</div>
                <div className="v4-deck-bubble me">I hold my ground and call out.</div>
                <div className="v4-deck-bubble dm">Perception check - DC 14. Your call?</div>
              </div>
              <div className="v4-deck-foot"><span><i className="bi bi-people" /> Thorn · Mira · Bram (AI)</span><span className="v4-deck-live"><span className="v4-deck-dot" /> LIVE</span></div>
            </div>

            <div className="v4-deck-card v4-deck-2">
              <div className="v4-deck-bar">fireside.session - The Gilded Library · Ch. 7</div>
              <div className="v4-deck-body">
                <div className="v4-deck-bubble dm">Dust motes turn in amber light. The ledger on the desk is warm to touch.</div>
                <div className="v4-deck-bubble me">I whisper the inscription - does it answer?</div>
                <div className="v4-deck-meta"><span className="v4-role dm">DM</span> waiting for Arcana - DC 15</div>
              </div>
              <div className="v4-deck-foot"><span><i className="bi bi-map" /> Grid 18×12 · no fog</span><span>Turn: Mira</span></div>
            </div>

            <div className="v4-deck-card v4-deck-3">
              <div className="v4-deck-bar">fireside.session - The Ashen Quay · Ch. 1</div>
              <div className="v4-deck-body">
                <div className="v4-deck-bubble dm">Cold brine and rope-smoke. A bell swings though no wind moves.</div>
                <div className="v4-deck-map">MAP - harbor · 3 boats · tide low</div>
                <div className="v4-deck-bubble me">I tie off and listen for footsteps on the dock.</div>
              </div>
              <div className="v4-deck-foot"><span><i className="bi bi-bag" /> Loot nearby</span><span>Party: 2 · solo ok</span></div>
            </div>
          </div>
        </div>
      </section>

      {/* How it works: stack left + text right */}
      <section className="v4-section v4-how" id="how-it-works" aria-labelledby="v4-how-title">
        <div className="v4-section-head">
          <span className="v4-kicker"><span className="v4-kicker-dot" aria-hidden="true" /> HOW IT WORKS</span>
          <h2 id="v4-how-title">From zero to “roll initiative” in minutes.</h2>
        </div>
        <div className="v4-how-grid">
          <div className="v4-cascade">
            <ol className="v4-cascade-steps">
              <li className="v4-cascade-step v4-cascade-1"><span className="v4-cascade-num" aria-hidden="true">01</span><span className="v4-step-badge">STEP 01</span><h3>Create or join</h3><p>Start a campaign or tap an invite code. Name your world, open the table.</p><span className="v4-step-tag">invite · lobby · atlas</span></li>
              <li className="v4-cascade-step v4-cascade-2"><span className="v4-cascade-num" aria-hidden="true">02</span><span className="v4-step-badge">STEP 02</span><h3>Bring a character</h3><p>Build a new hero or pick one you own. Every choice becomes DM memory.</p><span className="v4-step-tag">5-step wizard · folio</span></li>
              <li className="v4-cascade-step v4-cascade-3"><span className="v4-cascade-num" aria-hidden="true">03</span><span className="v4-step-badge">STEP 03</span><h3>Begin the adventure</h3><p>Gather in the lobby, step into a living session. The world reacts and remembers.</p><span className="v4-step-tag">turns · spells · maps</span></li>
            </ol>
          </div>
          <div className="v4-how-copy">
            <h3>Three moves. One table.</h3>
            <p>No rulebooks to wrangle, no scheduling purgatory. Fireside keeps the thread - you just show up and play. The DM remembers your last decision, your last roll, and your last promise.</p>
            <ul className="v4-how-list">
              <li><i className="bi bi-check-circle" aria-hidden="true" /> Takes ~90 seconds to open a table</li>
              <li><i className="bi bi-check-circle" aria-hidden="true" /> Invite code works for any campaign</li>
              <li><i className="bi bi-check-circle" aria-hidden="true" /> Characters live across stories, not just one</li>
            </ul>
            <Link href="/login" className="btn btn-primary small" style={{ marginTop: 14 }}><i className="bi bi-stars" aria-hidden="true" /> Start your campaign</Link>
          </div>
        </div>
      </section>

      <footer className="v4-footer">
        <span className="v4-footer-mark" aria-hidden="true">✺ Fireside</span>
        <span>Friends around the fire. Adventure everywhere else. · The only DM at Fireside is the AI - no human moderator.</span>
      </footer>
    </div>
  )
}

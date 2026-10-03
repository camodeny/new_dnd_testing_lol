'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import {
  conditionMeaning,
  healthText,
  healthy,
  isProjectionError,
  rechargeSentence,
  signed,
  statusSentence,
  type PanelTab,
  type ProjectionError,
  type TableCharacter,
  type TableJournal,
  type TablePartyMember,
} from '@/lib/table'
import type { CampaignMember } from '@/types'

const TAB_LABEL: Record<PanelTab, string> = { character: 'Character', journal: 'Journal', party: 'Party' }

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function Unavailable() {
  return <p className="tv-muted">This isn’t available right now. The story carries on.</p>
}

// ── Character ──────────────────────────────────────────────────────────────

function CharacterView({ character }: { character: TableCharacter | ProjectionError | null }) {
  const [allSkills, setAllSkills] = useState(false)
  if (character === null) return <p className="tv-muted">You don’t have a character seated at this table.</p>
  if (isProjectionError(character) || character.error) return <Unavailable />
  const subtitle = [character.race, character.class_label?.toLowerCase(), character.level ? `level ${character.level}` : null]
    .filter(Boolean).join(' · ')
  const hp = character.hp
  const skills = character.skills ?? []
  const shownSkills = allSkills ? skills : skills.slice(0, 3)
  return (
    <>
      <div className="tv-who">
        <span className="tv-face large" aria-hidden="true">{initials(character.name)}</span>
        <div><h3>{character.name}</h3>{subtitle && <p>{subtitle}</p>}</div>
      </div>
      <p className="tv-plain">{statusSentence(character)}</p>

      <section className="tv-section">
        <h4>Health</h4>
        {hp && (
          <div className="tv-health" role="meter" aria-label="Hit points" aria-valuemin={0} aria-valuemax={hp.max} aria-valuenow={hp.current}>
            <div className={`fill ${healthText(character.health).replace(' ', '-')}`} style={{ width: `${hp.max > 0 ? Math.min(100, (hp.current / hp.max) * 100) : 0}%` }} />
          </div>
        )}
        <dl className="tv-rows">
          {hp && <div><dt>Hit points</dt><dd>{hp.current} of {hp.max}{hp.temp > 0 ? ` (+${hp.temp} extra)` : ''}</dd></div>}
          {typeof character.armor_class === 'number' && <div><dt>How hard you are to hit</dt><dd>{character.armor_class}</dd></div>}
          {typeof character.speed === 'number' && <div><dt>Speed</dt><dd>{character.speed} feet a turn</dd></div>}
        </dl>
      </section>

      {character.conditions.length > 0 && (
        <section className="tv-section">
          <h4>Affecting you</h4>
          {character.conditions.map((name) => (
            <div className="tv-entry" key={name}>
              <b className="cap">{name}</b>
              {conditionMeaning(name) && <p>{conditionMeaning(name)}</p>}
            </div>
          ))}
        </section>
      )}

      {((character.resources?.length ?? 0) > 0 || (character.attacks?.length ?? 0) > 0) && (
        <section className="tv-section">
          <h4>What you can do</h4>
          {character.resources?.map((r) => (
            <div className="tv-entry" key={r.name}>
              <div className="tv-entry-top">
                <b>{r.name}</b>
                <span>{r.current > 0 ? `${r.current} ${r.current === 1 ? 'use' : 'uses'} left` : 'Used'}</span>
              </div>
              {rechargeSentence(r.recharge) && <p>{rechargeSentence(r.recharge)}</p>}
            </div>
          ))}
          {character.attacks?.map((a) => (
            <div className="tv-entry" key={a.name}>
              <div className="tv-entry-top">
                <b>{a.name}</b>
                {typeof a.to_hit === 'number' && <span>{signed(a.to_hit)} to hit</span>}
              </div>
              {a.damage && <p>Deals {a.damage}{a.damage_type ? ` ${a.damage_type}` : ''} damage.</p>}
            </div>
          ))}
        </section>
      )}

      {skills.length > 0 && (
        <section className="tv-section">
          <h4>What you’re good at</h4>
          <dl className="tv-rows">
            {shownSkills.map((s) => <div key={s.name}><dt>{s.name}</dt><dd>{signed(s.modifier)}</dd></div>)}
          </dl>
          {skills.length > 3 && (
            <button type="button" className="tv-link" onClick={() => setAllSkills((v) => !v)}>
              {allSkills ? 'Show fewer' : 'Show all skills'}
            </button>
          )}
        </section>
      )}
      <Link className="tv-link" href={`/characters/${character.character_id}`}>Open full character sheet</Link>
    </>
  )
}

// ── Journal ────────────────────────────────────────────────────────────────

function JournalView({ journal, fresh }: { journal: TableJournal | null; fresh: Set<string> }) {
  if (!journal) return <Unavailable />
  if (journal.people.length === 0 && journal.facts.length === 0) {
    return <p className="tv-muted">Nothing here yet. People you meet and things you learn will collect here.</p>
  }
  const New = ({ id }: { id: string }) => (fresh.has(id) ? <span className="tv-new">New</span> : null)
  return (
    <>
      {journal.people.length > 0 && (
        <section className="tv-section first">
          <h4>People you know of</h4>
          {journal.people.map((p) => (
            <div className="tv-entry" key={p.entity_id}>
              <b>{p.name}<New id={p.entity_id} /></b>
              {p.role && <p className="cap">{p.role}</p>}
              {p.summary && <p>{p.summary}</p>}
              {p.facts.length > 0 && (
                <ul className="tv-facts">{p.facts.map((f) => <li key={f.id}>{f.content}</li>)}</ul>
              )}
            </div>
          ))}
        </section>
      )}
      {journal.facts.length > 0 && (
        <section className="tv-section">
          <h4>Things you’ve learned</h4>
          {journal.facts.map((f) => (
            <div className="tv-entry" key={f.id}><p className="tv-fact">{f.content}<New id={f.id} /></p></div>
          ))}
        </section>
      )}
    </>
  )
}

// ── Party ──────────────────────────────────────────────────────────────────

function PartyView({ party, members }: { party: TablePartyMember[] | ProjectionError | null; members: CampaignMember[] }) {
  if (party === null || isProjectionError(party)) return <Unavailable />
  return (
    <section className="tv-section first">
      <h4>At the table</h4>
      {party.map((m) => {
        const player = members.find((member) => member.user_id === m.user_id)?.username
        const bits = [m.class_label, healthText(m.health), ...m.conditions.map((c) => c.toLowerCase())].filter(Boolean)
        return (
          <div className="tv-entry tv-party" key={m.character_id}>
            <span className="tv-face" aria-hidden="true">{initials(m.name)}</span>
            <div>
              <b>{m.name}{m.is_self && <span className="tv-muted"> (you)</span>}</b>
              <p>{bits.join(' · ')}{player && !m.is_self ? ` · played by ${player}` : ''}</p>
            </div>
          </div>
        )
      })}
    </section>
  )
}

// ── Panel ──────────────────────────────────────────────────────────────────

function journalIds(journal: TableJournal | null): string[] {
  if (!journal) return []
  return [...journal.people.map((p) => p.entity_id), ...journal.facts.map((f) => f.id)]
}

function seenKey(campaignId: string, userId: string) {
  return `fireside:journal-seen:${campaignId}:${userId}`
}

function readSeen(key: string): Set<string> | null {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? new Set(JSON.parse(raw) as string[]) : null
  } catch {
    return null
  }
}

interface SidePanelProps {
  campaignId: string
  userId: string | null
  tabs: PanelTab[]
  tab: PanelTab
  onTab: (tab: PanelTab) => void
  onClose: () => void
  character: TableCharacter | ProjectionError | null
  party: TablePartyMember[] | ProjectionError | null
  journal: TableJournal | ProjectionError | null
  members: CampaignMember[]
}

export default function SidePanel({
  campaignId, userId, tabs, tab, onTab, onClose, character, party, journal, members,
}: SidePanelProps) {
  const journalData = healthy(journal)
  const ids = useMemo(() => journalIds(journalData), [journalData])
  const key = userId ? seenKey(campaignId, userId) : null
  // Journal "New" marks are a per-device reading aid only; they never
  // affect what the server lets this player see.
  const [seen, setSeen] = useState<Set<string> | null>(null)
  const [fresh, setFresh] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!key) return
    const stored = readSeen(key)
    if (stored === null) {
      // First visit on this device: everything so far counts as read.
      const all = new Set(ids)
      window.localStorage.setItem(key, JSON.stringify([...all]))
      setSeen(all)
    } else {
      setSeen(stored)
    }
    // Load once per campaign/user; later journal growth is diffed against it.
  }, [key])

  const unseen = seen ? ids.filter((id) => !seen.has(id)) : []

  useEffect(() => {
    if (tab !== 'journal' || !key || !seen || unseen.length === 0) return
    setFresh((current) => new Set([...current, ...unseen]))
    const next = new Set([...seen, ...unseen])
    window.localStorage.setItem(key, JSON.stringify([...next]))
    setSeen(next)
  }, [tab, key, seen, unseen])

  const switchTab = (next: PanelTab) => {
    if (next !== 'journal') setFresh(new Set())
    onTab(next)
  }

  return (
    <aside className="tv-panel" aria-label="Side panel">
      <div className="tv-tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t} type="button" role="tab" aria-selected={tab === t}
            className={`tv-tab${tab === t ? ' on' : ''}`} onClick={() => switchTab(t)}
          >
            {TAB_LABEL[t]}
            {t === 'journal' && tab !== 'journal' && unseen.length > 0 && <span className="tv-dot" aria-label="New entries" />}
          </button>
        ))}
        <button type="button" className="tv-icon-btn tv-tabs-close" onClick={onClose} aria-label="Close side panel">
          <i className="bi bi-x-lg" aria-hidden="true" />
        </button>
      </div>
      <div className="tv-view" role="tabpanel">
        {tab === 'character' && <CharacterView character={character} />}
        {tab === 'journal' && <JournalView journal={journalData} fresh={fresh} />}
        {tab === 'party' && <PartyView party={party} members={members} />}
      </div>
    </aside>
  )
}

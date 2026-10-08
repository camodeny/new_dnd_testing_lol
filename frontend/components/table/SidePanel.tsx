'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import LootBoxCard from '@/components/table/LootBoxCard'
import { playerNotes, type PlayerNote } from '@/lib/api'
import {
  conditionMeaning,
  healthText,
  healthy,
  isProjectionError,
  rarityLabel,
  rechargeSentence,
  signed,
  statusSentence,
  type PanelTab,
  type ProjectionError,
  type TableCharacter,
  type TableInventory,
  type TableJournal,
  type TablePartyMember,
  type TableShop,
} from '@/lib/table'
import type { CampaignMember } from '@/types'

const TAB_LABEL: Record<'character' | 'journal' | 'party', string> = { character: 'Character', journal: 'Journal', party: 'Party' }

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function Unavailable() {
  return <p className="tv-muted">This isn’t available right now. The story carries on.</p>
}

// ── Character ──────────────────────────────────────────────────────────────

function coinLine(coins: TableInventory['coins']): string | null {
  const parts = (['pp', 'gp', 'ep', 'sp', 'cp'] as const)
    .filter((k) => coins[k] > 0)
    .map((k) => `${coins[k]} ${{ pp: 'platinum', gp: 'gold', ep: 'electrum', sp: 'silver', cp: 'copper' }[k]}`)
  return parts.length ? parts.join(', ') : null
}

function InventoryView({ campaignId, inventory, refresh }: {
  campaignId: string; inventory: TableInventory; refresh?: () => Promise<unknown>
}) {
  const sealed = inventory.loot_boxes.filter((b) => b.status === 'sealed')
  const recent = inventory.loot_boxes.filter((b) => b.status === 'opened').slice(0, 2)
  const coins = coinLine(inventory.coins)
  return (
    <>
      {(sealed.length > 0 || recent.length > 0) && (
        <section className="tv-section">
          <h4>Loot</h4>
          {[...sealed, ...recent].map((box) => <LootBoxCard key={box.id} campaignId={campaignId} box={box} refresh={refresh} />)}
        </section>
      )}
      {(coins || inventory.items.length > 0) && (
        <section className="tv-section">
          <h4>What you carry</h4>
          {coins && <p className="tv-plain">{coins}</p>}
          {inventory.items.map((item, index) => (
            <div className="tv-entry" key={`${item.name}-${index}`}>
              <div className="tv-entry-top">
                <b>{item.quantity && item.quantity > 1 ? `${item.quantity} × ` : ''}{item.name}</b>
                {item.rarity && item.rarity !== 'common' && <span className={`tv-rarity-tag ${item.rarity}`}>{rarityLabel(item.rarity)}</span>}
              </div>
              {item.description && <p>{item.description}</p>}
            </div>
          ))}
        </section>
      )}
    </>
  )
}

function CharacterView({ campaignId, character, refresh }: {
  campaignId: string; character: TableCharacter | ProjectionError | null; refresh?: () => Promise<unknown>
}) {
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
      {character.inventory && <InventoryView campaignId={campaignId} inventory={character.inventory} refresh={refresh} />}
      <Link className="tv-link" href={`/characters/${character.character_id}`}>Open full character sheet</Link>
    </>
  )
}

// ── Journal ────────────────────────────────────────────────────────────────

/** Placeholder until goals are tracked: there is no quest model yet. */
function GoalsPlaceholder() {
  return (
    <section className="tv-section first">
      <h4>What you’re trying to do</h4>
      <p className="tv-muted tv-soon-note">Coming soon: your goals will collect here as the story sets them.</p>
    </section>
  )
}

function PlayerNotes({ campaignId }: { campaignId: string }) {
  const [notes, setNotes] = useState<PlayerNote[]>([])
  const [draft, setDraft] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingText, setEditingText] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    playerNotes.list(campaignId)
      .then((data) => { if (!cancelled) setNotes(data?.notes ?? []) })
      .catch(() => { if (!cancelled) setNotes([]) })
    return () => { cancelled = true }
  }, [campaignId])

  const add = async () => {
    const content = draft.trim()
    if (!content || saving) return
    setSaving(true)
    setError('')
    try {
      const { note } = await playerNotes.create(campaignId, content)
      setNotes((current) => [...current, note])
      setDraft('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const saveEdit = async (id: string) => {
    const content = editingText.trim()
    if (!content || saving) return
    setSaving(true)
    setError('')
    try {
      const { note } = await playerNotes.update(campaignId, id, content)
      setNotes((current) => current.map((n) => (n.id === id ? note : n)))
      setEditingId(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (id: string) => {
    setError('')
    try {
      await playerNotes.remove(campaignId, id)
      setNotes((current) => current.filter((n) => n.id !== id))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <section className="tv-section first">
      <h4>My notes</h4>
      <p className="tv-muted">Only you can see these.</p>
      {notes.map((n) => (
        <div className="tv-entry" key={n.id}>
          {editingId === n.id ? (
            <>
              <textarea aria-label="Edit note" value={editingText} maxLength={2000}
                onChange={(e) => setEditingText(e.target.value)} rows={2} />
              <div>
                <button type="button" className="tv-btn ghost small" disabled={saving || !editingText.trim()}
                  onClick={() => void saveEdit(n.id)}>Save</button>{' '}
                <button type="button" className="tv-link" onClick={() => setEditingId(null)}>Cancel</button>
              </div>
            </>
          ) : (
            <>
              <p className="tv-fact">{n.content}</p>
              <div>
                <button type="button" className="tv-link" onClick={() => { setEditingId(n.id); setEditingText(n.content) }}>Edit</button>{' '}
                <button type="button" className="tv-link" onClick={() => void remove(n.id)}>Delete</button>
              </div>
            </>
          )}
        </div>
      ))}
      <textarea aria-label="New note" placeholder="Jot something down…" value={draft} maxLength={2000}
        onChange={(e) => setDraft(e.target.value)} rows={2} />
      <div>
        <button type="button" className="tv-btn ghost small" disabled={saving || !draft.trim()} onClick={() => void add()}>
          Add note
        </button>
      </div>
      {error && <p className="tv-error" role="alert">{error}</p>}
    </section>
  )
}

function JournalView({ campaignId, journal, fresh }: { campaignId: string; journal: TableJournal | null; fresh: Set<string> }) {
  if (!journal) return <Unavailable />
  if (journal.people.length === 0 && journal.facts.length === 0) {
    return (
      <>
        <GoalsPlaceholder />
        <PlayerNotes campaignId={campaignId} />
        <p className="tv-muted tv-section">Nothing else here yet. People you meet and things you learn will collect here.</p>
      </>
    )
  }
  const New = ({ id }: { id: string }) => (fresh.has(id) ? <span className="tv-new">New</span> : null)
  return (
    <>
      <GoalsPlaceholder />
      <PlayerNotes campaignId={campaignId} />
      {journal.people.length > 0 && (
        <section className="tv-section">
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

// ── Shop (placeholder) ─────────────────────────────────────────────────────

/** The shop is real (the scene references it); browsing and buying are not
 *  built yet (#464), so the story is the way to trade for now. */
function ShopView({ shop, onSuggest }: { shop: TableShop; onSuggest: (text: string) => void }) {
  return (
    <>
      <div className="tv-who">
        <span className="tv-face large tv-shop-glyph" aria-hidden="true"><i className="bi bi-shop" /></span>
        <div><h3>{shop.name}</h3>{shop.summary && <p className="tv-plain-case">{shop.summary}</p>}</div>
      </div>
      <div className="tv-soon">
        <b>Browsing wares here is coming soon</b>
        <p>For now, trade through the story. Tell the DM what you’d like to buy or sell.</p>
        <button type="button" className="tv-btn ghost small" onClick={() => onSuggest('I ask what they have for sale.')}>
          Ask what’s for sale
        </button>
      </div>
    </>
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
  shops: TableShop[]
  /** Prefill the composer from a panel action. */
  onSuggest: (text: string) => void
  /** Reload the table projection (e.g. after opening a loot box). */
  onRefresh?: () => Promise<unknown>
}

export default function SidePanel({
  campaignId, userId, tabs, tab, onTab, onClose, character, party, journal, members, shops, onSuggest, onRefresh,
}: SidePanelProps) {
  const shopFor = (t: PanelTab) => shops.find((shop) => `shop:${shop.entity_id}` === t) ?? null
  const activeShop = shopFor(tab)
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
        {tabs.map((t, index) => {
          const shop = shopFor(t)
          const startsContext = shop && !shopFor(tabs[index - 1] ?? 'character')
          return (
            <span key={t} className="tv-tab-slot">
              {startsContext && <span className="tv-tab-sep" aria-hidden="true" />}
              <button
                type="button" role="tab" aria-selected={tab === t}
                className={`tv-tab${tab === t ? ' on' : ''}${shop ? ' context' : ''}`} onClick={() => switchTab(t)}
              >
                {shop ? <><i className="bi bi-shop" aria-hidden="true" /> {shop.name}</> : TAB_LABEL[t as keyof typeof TAB_LABEL]}
                {t === 'journal' && tab !== 'journal' && unseen.length > 0 && <span className="tv-dot" aria-label="New entries" />}
              </button>
            </span>
          )
        })}
        <button type="button" className="tv-icon-btn tv-tabs-close" onClick={onClose} aria-label="Close side panel">
          <i className="bi bi-x-lg" aria-hidden="true" />
        </button>
      </div>
      <div className="tv-view" role="tabpanel">
        {tab === 'character' && <CharacterView campaignId={campaignId} character={character} refresh={onRefresh} />}
        {tab === 'journal' && <JournalView campaignId={campaignId} journal={journalData} fresh={fresh} />}
        {tab === 'party' && <PartyView party={party} members={members} />}
        {activeShop && <ShopView shop={activeShop} onSuggest={onSuggest} />}
      </div>
    </aside>
  )
}

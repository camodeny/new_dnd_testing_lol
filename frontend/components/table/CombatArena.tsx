'use client'

import type { CSSProperties } from 'react'
import { orderedParticipants, type TableCharacter, type TableEncounter } from '@/lib/table'

interface CombatArenaProps {
  encounter: TableEncounter
  userId: string | null
  character: TableCharacter | null
  /** Prefill the composer — the DM resolves what the player describes. */
  onSuggest: (text: string) => void
}

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

/**
 * The fight takes over the screen: turn order, the battle map, and what you
 * can do on your turn. Display only — turn order, legal movement, and
 * resources are the server's; actions are described in the story.
 */
export default function CombatArena({ encounter, userId, character, onSuggest }: CombatArenaProps) {
  const ordered = orderedParticipants(encounter)
  const activeId = encounter.turn?.active_participant_id ?? encounter.active_participant_id ?? null
  const active = ordered.find((p) => p.id === activeId) ?? null
  const mine = (p: { controller_user_id?: string | null }) => Boolean(userId) && p.controller_user_id === userId
  const myTurn = Boolean(active && mine(active))
  const myParticipant = ordered.find(mine) ?? null
  const resources = myParticipant ? encounter.turn?.resources?.[myParticipant.id] : undefined
  const rollingInitiative = (encounter.my_pending_initiative?.length ?? 0) > 0
  const round = encounter.turn?.round ?? encounter.round

  return (
    <div className="tv-arena">
      <div className="tv-turns" aria-label="Turn order">
        <span className="tv-round">{round ? `Round ${round}` : 'Rolling initiative'}</span>
        <ol>
          {ordered.map((p) => {
            const isActive = p.id === activeId
            const label = mine(p) ? 'You' : p.display_name
            return (
              <li
                key={p.id}
                className={['tv-turn', p.kind === 'pc' ? 'ally' : 'foe', isActive ? 'now' : '', mine(p) ? 'mine' : ''].filter(Boolean).join(' ')}
                aria-current={isActive ? 'true' : undefined}
              >
                <span className="tv-face" aria-hidden="true">{initials(p.display_name)}</span>
                {label}
                {isActive && mine(p) && <small>· your turn</small>}
              </li>
            )
          })}
        </ol>
      </div>

      <div className="tv-map-wrap">
        {encounter.map ? <BattleMap encounter={encounter} userId={userId} showReach={myTurn} /> : (
          <p className="tv-muted tv-no-map">There’s no battle map for this fight. Follow along in the story.</p>
        )}
      </div>

      <div className="tv-yourturn">
        {rollingInitiative ? (
          <div className="tv-yt-top"><h3>Roll for initiative</h3><p>Your roll decides when you act. Check the story for the roll.</p></div>
        ) : myTurn ? (
          <>
            <div className="tv-yt-top">
              <h3>Your turn</h3>
              <p>Move up to {resources?.movement_remaining ?? character?.speed ?? 30} feet and do one main thing.{encounter.map ? ' The green squares are where you can reach.' : ''}</p>
              {character?.hp && (
                <div className="tv-hp-mini">
                  Your health
                  <span className="bar"><span style={{ width: `${character.hp.max > 0 ? Math.min(100, (character.hp.current / character.hp.max) * 100) : 0}%` }} /></span>
                  {character.hp.current} of {character.hp.max}
                </div>
              )}
            </div>
            <div className="tv-yt-bottom">
              {resources && (
                <div className="tv-budget" aria-label="What you have left this turn">
                  <span className={resources.action_available ? '' : 'used'}>Action</span>
                  <span className={resources.bonus_action_available ? '' : 'used'}>Bonus action</span>
                  <span className={(resources.movement_remaining ?? 0) > 0 ? '' : 'used'}>{resources.movement_remaining ?? 0} ft to move</span>
                </div>
              )}
              <div className="tv-moves">
                {character?.attacks?.slice(0, 1).map((a) => (
                  <button key={a.name} type="button" className="tv-btn primary" onClick={() => onSuggest(`I attack with my ${a.name.toLowerCase()} `)}>
                    Attack with {a.name.toLowerCase()}
                  </button>
                ))}
                {character?.resources?.filter((r) => r.current > 0).slice(0, 1).map((r) => (
                  <button key={r.name} type="button" className="tv-btn ghost" onClick={() => onSuggest(`I use ${r.name}`)}>{r.name}</button>
                ))}
                <button type="button" className="tv-btn ghost" onClick={() => onSuggest('I take the Dodge action')}>Dodge</button>
                <button type="button" className="tv-btn ghost" onClick={() => onSuggest('')}>Something else…</button>
              </div>
            </div>
          </>
        ) : (
          <div className="tv-yt-top">
            <h3>{active ? `${active.display_name}’s turn` : 'Waiting for the next turn'}</h3>
            <p>You can still talk and plan in the story while you wait.</p>
          </div>
        )}
      </div>
    </div>
  )
}

function BattleMap({ encounter, userId, showReach }: { encounter: TableEncounter; userId: string | null; showReach: boolean }) {
  const map = encounter.map!
  const zoneAt = new Map<string, string>()
  // Later zones win, matching server terrain order.
  for (const zone of map.zones) {
    for (let c = zone.rect.col; c < zone.rect.col + zone.rect.width; c++) {
      for (let r = zone.rect.row; r < zone.rect.row + zone.rect.height; r++) zoneAt.set(`${c}:${r}`, zone.kind)
    }
  }
  const reach = new Set(showReach ? (encounter.reachable?.cells ?? []).map((cell) => `${cell.col}:${cell.row}`) : [])
  const byId = new Map(encounter.participants.map((p) => [p.id, p]))
  const cells = []
  for (let r = 0; r < map.height; r++) {
    for (let c = 0; c < map.width; c++) {
      const kind = zoneAt.get(`${c}:${r}`) ?? 'open'
      cells.push(
        <div
          key={`${c}:${r}`}
          className={`tv-cell ${kind}${reach.has(`${c}:${r}`) ? ' reach' : ''}`}
          style={{ gridColumn: c + 1, gridRow: r + 1 }}
        />,
      )
    }
  }
  return (
    <div
      className="tv-map"
      style={{ '--cols': map.width, '--rows': map.height } as CSSProperties}
      role="img"
      aria-label={`Battle map, ${map.width} by ${map.height} squares`}
    >
      {cells}
      {map.placements.map((placement) => {
        const p = byId.get(placement.participant_id)
        const name = p?.display_name ?? placement.display_name ?? '?'
        const isMine = Boolean(userId) && p?.controller_user_id === userId
        const kind = (p?.kind ?? placement.kind) === 'pc' ? 'ally' : 'foe'
        return (
          <div
            key={placement.participant_id}
            className={`tv-token ${isMine ? 'me' : kind}${placement.participant_id === encounter.active_participant_id ? ' active' : ''}`}
            style={{ gridColumn: placement.col + 1, gridRow: placement.row + 1 }}
            title={name}
          >
            <span>{initials(name)}</span>
          </div>
        )
      })}
    </div>
  )
}

'use client'

import { useState } from 'react'
import { apiFetch } from '@/lib/api'
import { rarityLabel, type LootRarity, type TableLootBox } from '@/lib/table'

const RARITY_ORDER: LootRarity[] = ['legendary', 'very_rare', 'rare', 'uncommon', 'common']

interface LootBoxCardProps {
  campaignId: string
  box: TableLootBox
  refresh?: () => Promise<unknown>
}

/**
 * A loot box the AI DM awarded this character (#463). Sealed, it shows how
 * many finds are inside and the odds; opening asks the server, which draws
 * the contents and puts them on the sheet. Opened, it shows the haul.
 */
export default function LootBoxCard({ campaignId, box, refresh }: LootBoxCardProps) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [opened, setOpened] = useState<TableLootBox | null>(null)
  const shown = opened ?? box
  // A box opens once, so its key is the box itself: a retry after a lost
  // response (even from a remounted panel) replays the same opening.
  const key = `loot-box-open:${box.id}`

  const open = async () => {
    if (pending) return
    setPending(true)
    setError('')
    try {
      const result = await apiFetch<{ loot_box: TableLootBox }>(`/campaigns/${campaignId}/loot-boxes/${box.id}/open`, {
        method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ operation_id: key }),
      })
      setOpened(result.loot_box)
      await refresh?.()
    } catch {
      setError('The box would not open. Try again.')
    } finally {
      setPending(false)
    }
  }

  if (shown.status === 'opened' && shown.contents) {
    const { items, gp } = shown.contents
    return (
      <div className={`tv-entry tv-loot opened${opened ? ' fresh' : ''}`} aria-live="polite">
        <b>{shown.title}</b>
        <ul className="tv-loot-haul">
          {items.map((item, index) => (
            <li key={`${item.name}-${index}`} className={`tv-rarity ${item.rarity ?? 'common'}`} style={{ animationDelay: `${index * 140}ms` }}>
              <span>{item.quantity && item.quantity > 1 ? `${item.quantity} × ` : ''}{item.name}</span>
              <em>{rarityLabel(item.rarity)}</em>
            </li>
          ))}
          {gp > 0 && <li className="tv-rarity coins" style={{ animationDelay: `${items.length * 140}ms` }}><span>{gp} gold pieces</span></li>}
        </ul>
      </div>
    )
  }

  const odds = RARITY_ORDER.filter((r) => shown.pool_rarities[r]).map((r) => `${shown.pool_rarities[r]} ${rarityLabel(r)}`)
  return (
    <div className="tv-entry tv-loot sealed">
      <div className="tv-entry-top">
        <b>{shown.title}</b>
        <button type="button" className="tv-btn primary small" onClick={open} disabled={pending}>
          {pending ? 'Opening…' : 'Open'}
        </button>
      </div>
      <p>
        You’ll find {shown.draws} of {shown.pool_size} possible {shown.pool_size === 1 ? 'item' : 'items'}, plus coin.
        {odds.length > 0 && ` Inside: ${odds.join(', ')}.`}
      </p>
      {error && <p className="tv-error" role="alert">{error}</p>}
    </div>
  )
}

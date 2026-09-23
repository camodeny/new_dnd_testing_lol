'use client'

import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useCallback } from 'react'
import CharacterFormLayout from '@/components/character/CharacterFormLayout'
import type { Character } from '@/types'

function CharacterCreateInner() {
  const router = useRouter()
  // Optional lobby campaign (?campaign=<id>) for party-aware creator
  // advice — the backend resolves public composition itself (#244).
  const searchParams = useSearchParams()
  const campaignId = searchParams.get('campaign')
  const handleDraftCreated = useCallback((character: Character) => {
    const query = new URLSearchParams()
    if (campaignId) query.set('campaign', campaignId)
    const suffix = query.toString()
    router.replace(`/characters/${character.id}/edit${suffix ? `?${suffix}` : ''}`)
  }, [campaignId, router])
  // Created from a campaign lobby: return there on save/cancel instead of
  // stranding the user in the character library. On save, ask the lobby to
  // select the new character via ?selectCharacter=.
  const lobbyUrl = campaignId ? `/campaigns/${encodeURIComponent(campaignId)}` : null
  return (
    <CharacterFormLayout
      characterId="new"
      campaignId={campaignId}
      onDraftCreated={handleDraftCreated}
      onSaved={(character) => router.push(
        lobbyUrl ? `${lobbyUrl}?selectCharacter=${encodeURIComponent(character.id)}` : `/characters/${character.id}`,
      )}
      onCancel={() => router.push(lobbyUrl ?? '/characters')}
    />
  )
}

export default function CharacterCreatePage() {
  return (
    <Suspense>
      <CharacterCreateInner />
    </Suspense>
  )
}

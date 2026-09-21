'use client'

import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense } from 'react'
import CharacterFormLayout from '@/components/character/CharacterFormLayout'

function CharacterCreateInner() {
  const router = useRouter()
  // Optional lobby campaign (?campaign=<id>) for party-aware creator
  // advice — the backend resolves public composition itself (#244).
  const searchParams = useSearchParams()
  const campaignId = searchParams.get('campaign')
  return (
    <CharacterFormLayout
      characterId="new"
      campaignId={campaignId}
      onSaved={(character) => router.push(`/characters/${character.id}`)}
      onCancel={() => router.push('/characters')}
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

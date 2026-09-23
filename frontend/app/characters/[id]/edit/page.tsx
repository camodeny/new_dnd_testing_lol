'use client'

import { Suspense, useEffect, useState } from 'react'
import { useParams, useRouter, useSearchParams } from 'next/navigation'
import { characters as charactersApi } from '@/lib/api'
import CharacterFormLayout from '@/components/character/CharacterFormLayout'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Character } from '@/types'

function CharacterEditPageContent() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const campaignId = useSearchParams().get('campaign')
  const [character, setCharacter] = useState<Character | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!id) return
    charactersApi
      .get(id)
      .then((data) => setCharacter(data.character))
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [id])

  if (loading) return <Loading />
  if (error) return <ErrorMessage message={error} />
  if (!character) return <ErrorMessage message="Character not found." />
  const lobbyUrl = campaignId ? `/campaigns/${encodeURIComponent(campaignId)}` : null

  return (
    <CharacterFormLayout
      characterId={String(id)}
      initial={character}
      campaignId={campaignId}
      onSaved={(saved) => router.push(
        lobbyUrl ? `${lobbyUrl}?selectCharacter=${encodeURIComponent(saved.id)}` : `/characters/${id}`,
      )}
      onCancel={() => router.push(
        lobbyUrl ? lobbyUrl : character.status === 'draft' ? '/characters' : `/characters/${id}`,
      )}
    />
  )
}

export default function CharacterEditPage() {
  return (
    <Suspense fallback={<Loading />}>
      <CharacterEditPageContent />
    </Suspense>
  )
}

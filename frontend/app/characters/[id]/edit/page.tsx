'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import { characters as charactersApi } from '@/lib/api'
import CharacterFormLayout from '@/components/character/CharacterFormLayout'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Character } from '@/types'

export default function CharacterEditPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
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

  return (
    <CharacterFormLayout
      characterId={String(id)}
      initial={character}
      onSaved={() => router.push(`/characters/${id}`)}
      onCancel={() => router.push(`/characters/${id}`)}
    />
  )
}

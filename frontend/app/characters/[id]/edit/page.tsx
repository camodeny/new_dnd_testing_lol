'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import { characters as charactersApi } from '@/lib/api'
import CharacterFormPage from '@/components/character/CharacterFormPage'
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
    <div className="page character-edit-page">
      <div className="character-form-header">
        <div>
          <span className="character-form-wizard__eyebrow">EDITING</span>
          <h1>{character.name}</h1>
        </div>
        <Link href={`/characters/${id}`} className="character-back-btn">
          <i className="bi bi-arrow-left" aria-hidden="true" /> View character
        </Link>
      </div>
      <CharacterFormPage
        initial={character}
        onSaved={() => router.push(`/characters/${id}`)}
        onCancel={() => router.push(`/characters/${id}`)}
      />
    </div>
  )
}

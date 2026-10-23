'use client'

import { useEffect, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { characters as charactersApi } from '@/lib/api'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Character } from '@/types'

const ABILITY_LABELS: [keyof Character, string][] = [
  ['strength', 'Strength'],
  ['dexterity', 'Dexterity'],
  ['constitution', 'Constitution'],
  ['intelligence', 'Intelligence'],
  ['wisdom', 'Wisdom'],
  ['charisma', 'Charisma'],
]

function abilityMod(score?: number): string {
  if (score == null) return '—'
  const mod = Math.floor((score - 10) / 2)
  return mod >= 0 ? `+${mod}` : String(mod)
}

export default function CharacterViewPage() {
  const { id } = useParams<{ id: string }>()
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

  const classLabel = character.classes?.map((c) => `${c.class_name} ${c.level}`).join(' / ') ?? ''

  return (
    <div className="page character-view-page">
      <Link href="/characters" className="character-back-btn">
        <i className="bi bi-arrow-left" aria-hidden="true" /> Characters
      </Link>

      <div className="character-sheet-view card">
        <div className="sheet-header character-sheet-view__header">
          <div className="character-sheet-view__title-row">
            <div className="character-sheet-view__title-copy">
              <h2 className="character-sheet-view__name">{character.name}</h2>
              <p className="sheet-subtitle character-sheet-view__subtitle">
                {[character.race, character.alignment, character.background].filter(Boolean).join(' · ')}
              </p>
              {classLabel && (
                <p className="character-sheet-view__class">{classLabel}</p>
              )}
            </div>
            <Link href={`/characters/${id}/edit`} className="btn btn-secondary small">
              <i className="bi bi-pencil" aria-hidden="true" /> Edit
            </Link>
          </div>
        </div>

        {/* Core stats */}
        <div className="character-sheet-view__quick-stats">
          {character.hit_points != null && (
            <div className="sheet-field">
              <div className="sheet-field__label">HP</div>
              <div className="sheet-field__value">{character.hit_points}</div>
            </div>
          )}
          {character.armor_class != null && (
            <div className="sheet-field">
              <div className="sheet-field__label">AC</div>
              <div className="sheet-field__value">{character.armor_class}</div>
            </div>
          )}
        </div>

        {/* Ability scores */}
        <div className="sheet-section character-sheet-view__abilities">
          <h3 className="sheet-section-title character-sheet-view__abilities-title">
            Ability Scores
          </h3>
          <div className="character-sheet-view__ability-grid">
            {ABILITY_LABELS.map(([key, label]) => {
              const score = character[key] as number | undefined
              return (
                <div
                  key={key}
                  className="ability-box"
                >
                  <div className="ability-box__label">
                    {label.slice(0, 3)}
                  </div>
                  <div className="ability-box__score">
                    {score ?? '—'}
                  </div>
                  <div className="ability-mod ability-box__mod">
                    {abilityMod(score)}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

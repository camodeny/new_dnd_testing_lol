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
    charactersApi
      .get(Number(id))
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

      <div className="character-sheet-view card" style={{ padding: 'clamp(24px, 4vw, 40px)' }}>
        <div className="sheet-header" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 20, marginBottom: 24 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
            <div>
              <h2 style={{ margin: '0 0 6px', fontSize: 'clamp(2.2rem, 4vw, 3rem)' }}>{character.name}</h2>
              <p className="sheet-subtitle" style={{ margin: 0, color: 'var(--ink-muted)', fontSize: '0.88rem' }}>
                {[character.race, character.alignment, character.background].filter(Boolean).join(' · ')}
              </p>
              {classLabel && (
                <p style={{ margin: '6px 0 0', color: 'var(--ink-muted)', fontSize: '0.84rem' }}>{classLabel}</p>
              )}
            </div>
            <Link href={`/characters/${id}/edit`} className="btn btn-secondary small">
              <i className="bi bi-pencil" aria-hidden="true" /> Edit
            </Link>
          </div>
        </div>

        {/* Core stats */}
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 24 }}>
          {character.hit_points != null && (
            <div className="sheet-field" style={{ padding: '10px 14px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--surface-paper)' }}>
              <div style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.1em', color: 'var(--ink-faint)', textTransform: 'uppercase', marginBottom: 2 }}>HP</div>
              <div style={{ fontSize: '1.4rem', fontWeight: 600, color: 'var(--ink-strong)' }}>{character.hit_points}</div>
            </div>
          )}
          {character.armor_class != null && (
            <div className="sheet-field" style={{ padding: '10px 14px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--surface-paper)' }}>
              <div style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.1em', color: 'var(--ink-faint)', textTransform: 'uppercase', marginBottom: 2 }}>AC</div>
              <div style={{ fontSize: '1.4rem', fontWeight: 600, color: 'var(--ink-strong)' }}>{character.armor_class}</div>
            </div>
          )}
        </div>

        {/* Ability scores */}
        <div className="sheet-section" style={{ borderTop: '1px solid var(--line)', paddingTop: 20 }}>
          <h3 className="sheet-section-title" style={{ margin: '0 0 16px', fontSize: '0.78rem', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--ink-faint)' }}>
            Ability Scores
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 10 }}>
            {ABILITY_LABELS.map(([key, label]) => {
              const score = character[key] as number | undefined
              return (
                <div
                  key={key}
                  className="ability-box"
                  style={{
                    padding: '12px 8px',
                    border: '1px solid var(--line)',
                    borderRadius: 8,
                    background: 'var(--surface-paper)',
                    textAlign: 'center',
                  }}
                >
                  <div style={{ fontSize: '0.58rem', fontWeight: 700, letterSpacing: '0.09em', color: 'var(--ink-faint)', textTransform: 'uppercase', marginBottom: 4 }}>
                    {label.slice(0, 3)}
                  </div>
                  <div style={{ fontSize: '1.4rem', fontWeight: 600, color: 'var(--ink-strong)', lineHeight: 1 }}>
                    {score ?? '—'}
                  </div>
                  <div className="ability-mod" style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--ink-muted)', marginTop: 2 }}>
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

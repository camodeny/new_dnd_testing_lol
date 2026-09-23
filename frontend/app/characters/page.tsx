'use client'

import { useState, useEffect } from 'react'
import Link from 'next/link'
import { characters as charactersApi } from '@/lib/api'
import CharacterCard from '@/components/character/CharacterCard'
import Modal from '@/components/common/Modal'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Character } from '@/types'

const LAYOUT_STORAGE_KEY = 'fireside:character-layout'

const LAYOUTS = [
  { id: 'strip', label: 'Compact' },
  { id: 'grid', label: 'Grid' },
  { id: 'spotlight', label: 'Spotlight' },
  { id: 'sheet', label: 'Sheet' },
  { id: 'bars', label: 'Bars' },
  { id: 'feature', label: 'Feature' },
] as const

type CharacterLayout = (typeof LAYOUTS)[number]['id']

function CharacterHeroLock() {
  useEffect(() => {
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [])
  return null
}

export default function CharactersListPage() {
  const [characterList, setCharacterList] = useState<Character[]>([])
  const [loading, setLoading] = useState(true)
  const [deleteTarget, setDeleteTarget] = useState<Character | null>(null)
  const [deleteLoading, setDeleteLoading] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [layout, setLayout] = useState<CharacterLayout>(() => {
    if (typeof window === 'undefined') return 'strip'
    const saved = window.localStorage.getItem(LAYOUT_STORAGE_KEY)
    return LAYOUTS.some((l) => l.id === saved) ? (saved as CharacterLayout) : 'strip'
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, layout)
  }, [layout])

  useEffect(() => {
    let mounted = true
    charactersApi
      .list()
      .then((data) => { if (mounted) setCharacterList(data.characters ?? []) })
      .catch(() => { /* show the empty hero when the backend is unavailable */ })
      .finally(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [])

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return
    setDeleteLoading(true)
    setDeleteError('')
    try {
      await charactersApi.delete(deleteTarget.id)
      setCharacterList((prev) => prev.filter((c) => c.id !== deleteTarget.id))
      setDeleteTarget(null)
    } catch (err) {
      setDeleteError((err as Error).message)
    } finally {
      setDeleteLoading(false)
    }
  }

  const renderActions = (c: Character) => (
    <div className="spotlight-actions">
      <Link href={`/characters/${c.id}`} className="btn btn-secondary small">
        View
      </Link>
      <Link
        href={`/characters/${c.id}/edit`}
        className="btn btn-secondary small character-card-edit-link"
        aria-label={`Edit ${c.name}`}
        title={`Edit ${c.name}`}
      >
        <i className="bi bi-pencil" aria-hidden="true" />
      </Link>
      <button
        type="button"
        className="btn btn-danger small character-card-delete-button"
        onClick={() => { setDeleteError(''); setDeleteTarget(c) }}
        aria-label={`Delete ${c.name}`}
        title={`Delete ${c.name}`}
      >
        <i className="bi bi-trash" aria-hidden="true" />
      </button>
    </div>
  )

  const renderRow = (c: Character, rl: 'strip' | 'grid' | 'sheet' | 'bars') => (
    <article
      key={c.id}
      className="card-wrapper"
      role="listitem"
      aria-labelledby={`character-${c.id}-name`}
    >
      <Link href={`/characters/${c.id}`} className={`character-card-link character-card-link--${rl}`} tabIndex={-1} aria-hidden="true">
        <CharacterCard character={c} layout={rl} />
      </Link>
      <div className="card-actions">
        <Link href={`/characters/${c.id}`} className="btn btn-secondary small">
          View
        </Link>
        <Link
          href={`/characters/${c.id}/edit`}
          className="btn btn-secondary small character-card-edit-link"
          aria-label={`Edit ${c.name}`}
          title={`Edit ${c.name}`}
        >
          <i className="bi bi-pencil" aria-hidden="true" />
        </Link>
        <button
          type="button"
          className="btn btn-danger small character-card-delete-button"
          onClick={() => { setDeleteError(''); setDeleteTarget(c) }}
          aria-label={`Delete ${c.name}`}
          title={`Delete ${c.name}`}
        >
          <i className="bi bi-trash" aria-hidden="true" />
        </button>
      </div>
    </article>
  )

  const selectedCharacter =
    characterList.find((c) => c.id === selectedId && c.status !== 'draft') ??
    characterList.find((c) => c.status !== 'draft') ?? null
  const draftCharacters = characterList.filter((c) => c.status === 'draft')
  const completedCharacters = characterList.filter((c) => c.status !== 'draft')
  const featuredCharacter = completedCharacters[0] ?? null
  const rosterLayout: 'strip' | 'grid' | 'sheet' | 'bars' =
    layout === 'grid' || layout === 'sheet' || layout === 'bars' ? layout : 'strip'

  if (loading) return <Loading />

  if (characterList.length === 0) {
    return (
      <>
        <CharacterHeroLock />
        <section className="campaign-hero character-library-hero" style={{ minHeight: 'calc(100svh - 64px)', marginBottom: 0 }}>
          <div className="campaign-hero-copy">
            <span className="section-kicker">YOUR COMPANIONS</span>
            <h1>Every party needs a first name.</h1>
            <p>Create a character here, then bring them into any campaign you join.</p>
            <div className="campaign-hero-actions">
              <Link href="/characters/new" className="btn btn-primary">
                <i className="bi bi-plus-lg" aria-hidden="true" /> Create your first character
              </Link>
            </div>
          </div>
          <div className="campaign-hero-art" aria-hidden="true">
            <span className="campaign-hero-grain" />
          </div>
        </section>
      </>
    )
  }

  return (
    <div className="page characters-list-page">
      <header className="character-library-header">
        <div>
          <span className="wildwood-kicker">YOUR COMPANIONS</span>
          <h1>Your characters</h1>
          <p>Keep the people who carry your stories close at hand.</p>
        </div>
        <div className="character-library-tools">
          {completedCharacters.length > 0 && (
            <div className="layout-switcher" role="group" aria-label="Character list layout">
              {LAYOUTS.map((l) => (
                <button
                  key={l.id}
                  type="button"
                  aria-pressed={layout === l.id}
                  onClick={() => setLayout(l.id)}
                >
                  {l.label}
                </button>
              ))}
            </div>
          )}
          <Link href="/characters/new" className="btn btn-primary">
            <i className="bi bi-plus-lg" aria-hidden="true" /> Create character
          </Link>
        </div>
      </header>

      {draftCharacters.length > 0 && (
        <section className="character-drafts" aria-labelledby="character-drafts-title">
          <div className="character-drafts__heading">
            <div>
              <span className="wildwood-kicker">IN PROGRESS</span>
              <h2 id="character-drafts-title">Character drafts</h2>
            </div>
            <p>Your unfinished sheets are saved here automatically.</p>
          </div>
          <div className="character-drafts__list" role="list" aria-label="Character drafts">
            {draftCharacters.map((character) => {
              const step = character.creator_step?.replaceAll('_', ' ') ?? 'identity'
              const details = [character.race, character.classes?.map((cl) => cl.class_name).filter(Boolean).join(' / ')]
                .filter(Boolean)
                .join(' · ')
              return (
                <article className="character-draft-row" role="listitem" key={character.id}>
                  <div className="character-draft-row__copy">
                    <h3>{character.name || 'Untitled Character'}</h3>
                    <p>{details || `Last step: ${step}`}</p>
                  </div>
                  <div className="character-draft-row__actions">
                    <Link href={`/characters/${character.id}/edit`} className="btn btn-primary small">
                      Continue draft
                    </Link>
                    <button
                      type="button"
                      className="btn btn-danger small character-card-delete-button"
                      onClick={() => { setDeleteError(''); setDeleteTarget(character) }}
                      aria-label={`Delete draft ${character.name || 'Untitled Character'}`}
                      title="Delete draft"
                    >
                      <i className="bi bi-trash" aria-hidden="true" />
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        </section>
      )}

      {completedCharacters.length > 0 && (layout === 'feature' && featuredCharacter ? (
        <div className="character-feature">
          <div className="feature-stage">
            <CharacterCard character={featuredCharacter} layout="feature" />
            {renderActions(featuredCharacter)}
          </div>
          {completedCharacters.length > 1 && (
            <div className="character-roster" data-layout="strip" role="list" aria-label="More characters">
              {completedCharacters.slice(1).map((c) => renderRow(c, 'strip'))}
            </div>
          )}
        </div>
      ) : layout === 'spotlight' && selectedCharacter ? (
        <div className="character-spotlight">
          <div className="spotlight-list" role="listbox" aria-label="Characters">
            {completedCharacters.map((c) => {
              const itemLabel = c.classes?.map((cl) => `${cl.class_name} ${cl.level}`).join(' / ') ?? ''
              const isActive = c.id === selectedCharacter.id
              return (
                <button
                  key={c.id}
                  type="button"
                  role="option"
                  aria-selected={isActive}
                  className="spotlight-item"
                  onClick={() => setSelectedId(c.id)}
                >
                  <span className="spotlight-item-name">{c.name}</span>
                  {itemLabel && <span className="spotlight-item-sub">{itemLabel}</span>}
                </button>
              )
            })}
          </div>
          <div className="spotlight-detail">
            <CharacterCard character={selectedCharacter} layout="spotlight" />
            {renderActions(selectedCharacter)}
          </div>
        </div>
      ) : (
      <div className="character-roster" data-layout={rosterLayout} role="list" aria-label="Characters">
        {completedCharacters.map((c) => renderRow(c, rosterLayout))}
      </div>
      ))}

      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={deleteTarget?.status === 'draft' ? 'Delete character draft' : 'Delete character'}
      >
        <div style={{ display: 'grid', gap: 16 }}>
          <p style={{ margin: 0, lineHeight: 1.6 }}>
            Delete <strong>{deleteTarget?.name || 'Untitled Character'}</strong>?{' '}
            {deleteTarget?.status === 'draft'
              ? 'This permanently removes the unfinished sheet from your library.'
              : 'This permanently removes the character from your library and unassigns them from any campaigns.'}
          </p>
          {deleteError && <ErrorMessage message={deleteError} />}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="btn btn-secondary" onClick={() => setDeleteTarget(null)} disabled={deleteLoading}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" onClick={handleConfirmDelete} disabled={deleteLoading}>
              {deleteLoading ? 'Deleting…' : 'Delete'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

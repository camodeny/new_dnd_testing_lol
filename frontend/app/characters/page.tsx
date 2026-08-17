'use client'

import { useState, useEffect } from 'react'
import Link from 'next/link'
import { characters as charactersApi } from '@/lib/api'
import CharacterCard from '@/components/character/CharacterCard'
import Modal from '@/components/common/Modal'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Character } from '@/types'

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
        <Link href="/characters/new" className="btn btn-primary">
          <i className="bi bi-plus-lg" aria-hidden="true" /> Create character
        </Link>
      </header>

      <div className="character-roster-labels" aria-hidden="true">
        <span>Character</span>
        <span>Class</span>
        <span>Abilities</span>
        <span />
      </div>
      <div className="character-roster" role="list" aria-label="Characters">
        {characterList.map((c) => (
          <article
            key={c.id}
            className="card-wrapper"
            role="listitem"
            aria-labelledby={`character-${c.id}-name`}
          >
            <Link href={`/characters/${c.id}`} className="character-card-link" tabIndex={-1} aria-hidden="true">
              <CharacterCard character={c} />
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
        ))}
      </div>

      <Modal
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete character"
      >
        <div style={{ display: 'grid', gap: 16 }}>
          <p style={{ margin: 0, lineHeight: 1.6 }}>
            Delete <strong>{deleteTarget?.name}</strong>? This permanently removes the character from your library and unassigns them from any campaigns.
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

'use client'

import { useRouter } from 'next/navigation'
import Link from 'next/link'
import CharacterFormPage from '@/components/character/CharacterFormPage'

export default function CharacterCreatePage() {
  const router = useRouter()
  return (
    <div className="page character-create-page">
      <div className="character-form-header">
        <div>
          <span className="wildwood-kicker">CHARACTER FOLIO</span>
          <h1>Create a character</h1>
          <p>Start with the essentials. You can refine every detail later.</p>
        </div>
        <Link href="/characters" className="character-back-btn">
          <i className="bi bi-arrow-left" aria-hidden="true" /> Back
        </Link>
      </div>
      <CharacterFormPage
        onSaved={(character) => router.push(`/characters/${character.id}`)}
        onCancel={() => router.push('/characters')}
      />
    </div>
  )
}

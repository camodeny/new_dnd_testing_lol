'use client'

import { useRouter } from 'next/navigation'
import CharacterFormLayout from '@/components/character/CharacterFormLayout'

export default function CharacterCreatePage() {
  const router = useRouter()
  return (
    <CharacterFormLayout
      characterId="new"
      onSaved={(character) => router.push(`/characters/${character.id}`)}
      onCancel={() => router.push('/characters')}
    />
  )
}

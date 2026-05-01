import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createDevCharacter } from '../api/client'
import ErrorMessage from '../components/common/ErrorMessage'

export default function DevCharacterPage() {
  const navigate = useNavigate()
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false

    async function makeCharacter() {
      try {
        const data = await createDevCharacter()
        if (!cancelled && data.character?.id) {
          navigate(`/characters/${data.character.id}`)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || 'Failed to create dev character')
        }
      }
    }

    makeCharacter()

    return () => {
      cancelled = true
    }
  }, [navigate])

  if (error) {
    return (
      <div className="dev-character-page">
        <h2>Dev Character</h2>
        <ErrorMessage message={error} />
      </div>
    )
  }

  return (
    <div className="dev-character-page">
      <h2>Creating dev character...</h2>
    </div>
  )
}

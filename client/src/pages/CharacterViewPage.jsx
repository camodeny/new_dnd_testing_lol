import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getCharacter, deleteCharacter } from '../api/client'
import CharacterSheetView from '../components/character/CharacterSheetView'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

export default function CharacterViewPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [character, setCharacter] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    getCharacter(id)
      .then((data) => setCharacter(data.character))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [id])

  const handleDelete = async () => {
    if (!window.confirm('Are you sure you want to delete this character?')) return
    try {
      await deleteCharacter(id)
      navigate('/characters')
    } catch (err) {
      setError(err.message)
    }
  }

  if (loading) return <Loading />
  if (error) return <ErrorMessage message={error} />

  return (
    <div className="page character-view-page">
      <button className="character-back-btn" onClick={() => navigate('/characters')}><i className="bi bi-arrow-left" /> All characters</button>
      <CharacterSheetView
        character={character}
        onEdit={() => navigate(`/characters/${id}/edit`)}
        onDelete={handleDelete}
      />
    </div>
  )
}

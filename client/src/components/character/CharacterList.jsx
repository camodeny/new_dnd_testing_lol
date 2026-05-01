import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getCharacters, deleteCharacter } from '../../api/client'
import Button from '../common/Button'
import Loading from '../common/Loading'
import ErrorMessage from '../common/ErrorMessage'
import CharacterCard from './CharacterCard'

export default function CharacterList() {
  const [characters, setCharacters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const navigate = useNavigate()

  const fetchCharacters = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await getCharacters()
      setCharacters(data.characters || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchCharacters()
  }, [])

  const handleDelete = async (id) => {
    if (!window.confirm('Are you sure you want to delete this character?')) return
    try {
      await deleteCharacter(id)
      setCharacters((prev) => prev.filter((c) => c.id !== id))
    } catch (err) {
      setError(err.message)
    }
  }

  if (loading) return <Loading />

  return (
    <div className="character-list">
      <div className="list-header">
        <h2>Your Characters</h2>
        <Button onClick={() => navigate('/characters/new')} variant="primary">Create Character</Button>
      </div>
      <ErrorMessage message={error} />
      {characters.length === 0 ? (
        <p className="empty-state">No characters yet. Create your first adventurer!</p>
      ) : (
        <div className="card-grid">
          {characters.map((c) => (
            <div key={c.id} className="card-wrapper">
              <CharacterCard character={c} />
              <div className="card-actions">
                <Button onClick={() => navigate(`/characters/${c.id}`)} variant="secondary" className="small">View</Button>
                <Button onClick={() => navigate(`/characters/${c.id}/edit`)} variant="secondary" className="small">Edit</Button>
                <Button onClick={() => handleDelete(c.id)} variant="danger" className="small">Delete</Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

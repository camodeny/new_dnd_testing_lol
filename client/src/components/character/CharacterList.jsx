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

  useEffect(() => {
    let isMounted = true

    getCharacters()
      .then((data) => {
        if (isMounted) setCharacters(data.characters || [])
      })
      .catch((err) => {
        if (isMounted) setError(err.message)
      })
      .finally(() => {
        if (isMounted) setLoading(false)
      })

    return () => {
      isMounted = false
    }
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
      <header className="character-library-header">
        <div>
          <span className="wildwood-kicker">YOUR COMPANIONS</span>
          <h1>Your characters</h1>
          <p>Keep the people who carry your stories close at hand.</p>
        </div>
        <Button onClick={() => navigate('/characters/new')} variant="primary"><i className="bi bi-plus-lg" /> Create character</Button>
      </header>
      {characters.length > 0 && (
        <div className="character-roster-labels" aria-hidden="true">
          <span>Character</span><span>Condition</span><span>Abilities</span><span />
        </div>
      )}
      <ErrorMessage message={error} />
      {characters.length === 0 ? (
        <div className="character-empty-state">
          <span className="character-empty-mark">✺</span>
          <h2>Every party needs a first name.</h2>
          <p>Create a character here, then bring them into any campaign you join.</p>
          <Button onClick={() => navigate('/characters/new')} variant="primary">Create your first character</Button>
        </div>
      ) : (
        <div className="card-grid character-roster">
          {characters.map((c) => (
            <div key={c.id} className="card-wrapper">
              <CharacterCard character={c} />
              <div className="card-actions">
                <Button onClick={() => navigate(`/characters/${c.id}`)} variant="secondary" className="small">View</Button>
                <Button onClick={() => navigate(`/characters/${c.id}/edit`)} variant="secondary" className="small"><i className="bi bi-pencil" /></Button>
                <Button onClick={() => handleDelete(c.id)} variant="danger" className="small"><i className="bi bi-trash" /></Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

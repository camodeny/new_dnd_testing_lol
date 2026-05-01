import { useNavigate } from 'react-router-dom'
import CharacterForm from '../components/character/CharacterForm'
import { createCharacter } from '../api/client'

export default function CharacterCreatePage() {
  const navigate = useNavigate()

  const handleSubmit = async (payload) => {
    await createCharacter(payload)
    navigate('/characters')
  }

  return (
    <div className="page character-create-page">
      <h2>Create Character</h2>
      <CharacterForm onSubmit={handleSubmit} onCancel={() => navigate('/characters')} submitLabel="Create Character" />
    </div>
  )
}

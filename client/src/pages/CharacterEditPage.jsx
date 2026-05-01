import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getCharacter, updateCharacter } from '../api/client'
import CharacterForm from '../components/character/CharacterForm'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

export default function CharacterEditPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [character, setCharacter] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    getCharacter(id)
      .then((data) => {
        const c = data.character
        setCharacter({
          ...c,
          ability_scores: c.ability_scores || {},
          combat: c.combat || {},
          general: c.general || {},
          spellcasting: c.spellcasting || { spell_slots: {} },
          currency: c.currency || {},
          personality: c.personality || {},
          appearance: c.appearance || {},
          background_details: c.background_details || {},
          classes: c.classes || [],
          skills: c.skills || [],
          saving_throws: c.saving_throws || [],
          proficiencies: c.proficiencies || [],
          features: c.features || [],
          weapons: c.weapons || [],
          equipment: c.equipment || [],
          spells: c.spells || [],
          notes: c.notes || [],
          resources: c.resources || [],
          companions: c.companions || [],
          conditions: c.conditions || [],
        })
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [id])

  const handleSubmit = async (payload) => {
    await updateCharacter(id, payload)
    navigate(`/characters/${id}`)
  }

  if (loading) return <Loading />
  if (error) return <ErrorMessage message={error} />

  return (
    <div className="page character-edit-page">
      <h2>Edit Character</h2>
      <CharacterForm
        initialCharacter={character}
        onSubmit={handleSubmit}
        onCancel={() => navigate(`/characters/${id}`)}
        submitLabel="Save Changes"
      />
    </div>
  )
}

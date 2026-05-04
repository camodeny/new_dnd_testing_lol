import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import CharacterForm from '../components/character/CharacterForm'
import { mergeCharacterDraft } from '../components/character/characterFormConfig'
import { createCharacter } from '../api/client'

export default function CharacterCreatePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const campaignId = searchParams.get('campaign')
  const returnTo = location.state?.returnTo || (campaignId ? `/campaigns/${campaignId}` : '/characters')
  const draft = location.state?.draft

  const initialCharacter = draft ? mergeCharacterDraft(draft) : undefined

  const handleSubmit = async (payload) => {
    await createCharacter({
      ...payload,
      ...(campaignId ? { campaign_id: Number(campaignId) } : {}),
    })
    navigate(returnTo)
  }

  return (
    <div className="page character-create-page">
      <h2>{draft ? 'Review Character Draft' : 'Create Character'}</h2>
      <CharacterForm
        initialCharacter={initialCharacter}
        onSubmit={handleSubmit}
        onCancel={() => navigate(returnTo)}
        submitLabel={campaignId ? 'Save for Campaign' : 'Create Character'}
      />
    </div>
  )
}

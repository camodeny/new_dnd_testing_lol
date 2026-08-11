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
      <header className="character-form-header">
        <div>
          <span className="wildwood-kicker">CHARACTER FOLIO</span>
          <h1>{draft ? 'Review your character' : 'Create a character'}</h1>
          <p>{draft ? 'Check the details before adding this companion to your story.' : 'Start with the essentials. You can refine every detail later.'}</p>
        </div>
        <button className="character-back-btn" onClick={() => navigate(returnTo)}><i className="bi bi-arrow-left" /> Back</button>
      </header>
      <CharacterForm
        initialCharacter={initialCharacter}
        onSubmit={handleSubmit}
        onCancel={() => navigate(returnTo)}
        submitLabel={campaignId ? 'Save for Campaign' : 'Create Character'}
      />
    </div>
  )
}

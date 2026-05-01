import FormGroup from '../../common/FormGroup'
import TextArea from '../../common/TextArea'

export default function PersonalitySection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Personality</h3>
      <div className="form-grid two-col">
        <FormGroup label="Personality Traits" htmlFor="per-traits">
          <TextArea id="per-traits" value={data.personality_traits || ''} onChange={(e) => set('personality_traits', e.target.value)} />
        </FormGroup>
        <FormGroup label="Ideals" htmlFor="per-ideals">
          <TextArea id="per-ideals" value={data.ideals || ''} onChange={(e) => set('ideals', e.target.value)} />
        </FormGroup>
        <FormGroup label="Bonds" htmlFor="per-bonds">
          <TextArea id="per-bonds" value={data.bonds || ''} onChange={(e) => set('bonds', e.target.value)} />
        </FormGroup>
        <FormGroup label="Flaws" htmlFor="per-flaws">
          <TextArea id="per-flaws" value={data.flaws || ''} onChange={(e) => set('flaws', e.target.value)} />
        </FormGroup>
      </div>
    </div>
  )
}

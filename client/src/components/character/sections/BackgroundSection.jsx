import FormGroup from '../../common/FormGroup'
import TextArea from '../../common/TextArea'

export default function BackgroundSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Background Details</h3>
      <FormGroup label="Backstory" htmlFor="bg-story">
        <TextArea id="bg-story" value={data.backstory || ''} onChange={(e) => set('backstory', e.target.value)} rows={5} />
      </FormGroup>
      <FormGroup label="Allies & Organizations" htmlFor="bg-allies">
        <TextArea id="bg-allies" value={data.allies_organizations || ''} onChange={(e) => set('allies_organizations', e.target.value)} rows={3} />
      </FormGroup>
      <FormGroup label="Additional Features & Traits" htmlFor="bg-features">
        <TextArea id="bg-features" value={data.additional_features_traits || ''} onChange={(e) => set('additional_features_traits', e.target.value)} rows={3} />
      </FormGroup>
      <FormGroup label="Treasure" htmlFor="bg-treasure">
        <TextArea id="bg-treasure" value={data.treasure || ''} onChange={(e) => set('treasure', e.target.value)} rows={3} />
      </FormGroup>
    </div>
  )
}

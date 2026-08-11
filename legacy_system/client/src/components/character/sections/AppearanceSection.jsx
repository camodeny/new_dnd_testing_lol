import FormGroup from '../../common/FormGroup'
import Input from '../../common/Input'
import TextArea from '../../common/TextArea'

export default function AppearanceSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Appearance</h3>
      <div className="form-grid three-col">
        <FormGroup label="Age" htmlFor="app-age">
          <Input id="app-age" value={data.age || ''} onChange={(e) => set('age', e.target.value)} />
        </FormGroup>
        <FormGroup label="Height" htmlFor="app-height">
          <Input id="app-height" value={data.height || ''} onChange={(e) => set('height', e.target.value)} />
        </FormGroup>
        <FormGroup label="Weight" htmlFor="app-weight">
          <Input id="app-weight" value={data.weight || ''} onChange={(e) => set('weight', e.target.value)} />
        </FormGroup>
        <FormGroup label="Eyes" htmlFor="app-eyes">
          <Input id="app-eyes" value={data.eyes || ''} onChange={(e) => set('eyes', e.target.value)} />
        </FormGroup>
        <FormGroup label="Skin" htmlFor="app-skin">
          <Input id="app-skin" value={data.skin || ''} onChange={(e) => set('skin', e.target.value)} />
        </FormGroup>
        <FormGroup label="Hair" htmlFor="app-hair">
          <Input id="app-hair" value={data.hair || ''} onChange={(e) => set('hair', e.target.value)} />
        </FormGroup>
      </div>
      <FormGroup label="Character Appearance Description" htmlFor="app-desc">
        <TextArea id="app-desc" value={data.character_appearance || ''} onChange={(e) => set('character_appearance', e.target.value)} rows={4} />
      </FormGroup>
    </div>
  )
}

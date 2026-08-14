import FormGroup from '@/components/common/FormGroup'
import Input from '@/components/common/Input'
import TextArea from '@/components/common/TextArea'

const FIELDS = ['age', 'height', 'weight', 'eyes', 'skin', 'hair'] as const

interface Props {
  data: Record<string, string>
  onChange: (data: Record<string, string>) => void
}

export default function AppearanceSection({ data, onChange }: Props) {
  const set = (field: string, value: string) => onChange({ ...data, [field]: value })
  return (
    <div className="form-section">
      <h3>Appearance</h3>
      <div className="form-grid three-col">
        {FIELDS.map((field) => (
          <FormGroup key={field} label={`${field[0].toUpperCase()}${field.slice(1)}`} htmlFor={`app-${field}`}>
            <Input id={`app-${field}`} value={data[field]} onChange={(event) => set(field, event.target.value)} />
          </FormGroup>
        ))}
      </div>
      <FormGroup label="Character Appearance Description" htmlFor="app-description">
        <TextArea
          id="app-description"
          value={data.character_appearance}
          onChange={(event) => set('character_appearance', event.target.value)}
          rows={4}
        />
      </FormGroup>
    </div>
  )
}

import FormGroup from '@/components/common/FormGroup'
import TextArea from '@/components/common/TextArea'

const FIELDS = [
  ['personality_traits', 'Personality Traits'],
  ['ideals', 'Ideals'],
  ['bonds', 'Bonds'],
  ['flaws', 'Flaws'],
] as const

interface Props {
  data: Record<string, string>
  onChange: (data: Record<string, string>) => void
}

export default function PersonalitySection({ data, onChange }: Props) {
  return (
    <div className="form-section">
      <h3>Personality</h3>
      <div className="form-grid two-col">
        {FIELDS.map(([key, label]) => (
          <FormGroup key={key} label={label} htmlFor={`per-${key}`}>
            <TextArea
              id={`per-${key}`}
              value={data[key] ?? ''}
              onChange={(event) => onChange({ ...data, [key]: event.target.value })}
            />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

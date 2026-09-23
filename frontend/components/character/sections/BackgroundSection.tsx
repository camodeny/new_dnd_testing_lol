import FormGroup from '@/components/common/FormGroup'
import TextArea from '@/components/common/TextArea'

const FIELDS = [
  ['backstory', 'Backstory', 5],
  ['allies_organizations', 'Allies & Organizations', 3],
  ['additional_features_traits', 'Additional Features & Traits', 3],
  ['treasure', 'Treasure', 3],
] as const

interface Props {
  data: Record<string, string>
  onChange: (data: Record<string, string>) => void
  onOpenAI?: () => void
}

export default function BackgroundSection({ data, onChange, onOpenAI }: Props) {
  return (
    <div className="form-section">
      <div className="background-section-heading">
        <div>
          <h3>Background Details</h3>
          <p>Build the story your character brings into the adventure. You can shape it with the AI Helper, then edit the draft here.</p>
        </div>
        {onOpenAI && <button type="button" className="btn btn-secondary" onClick={onOpenAI}><i className="bi bi-stars" aria-hidden="true" /> Shape with AI</button>}
      </div>
      {FIELDS.map(([key, label, rows]) => (
        <FormGroup key={key} label={label} htmlFor={`bg-${key}`}>
          <TextArea
            id={`bg-${key}`}
            value={data[key] ?? ''}
            onChange={(event) => onChange({ ...data, [key]: event.target.value })}
            rows={rows}
          />
        </FormGroup>
      ))}
    </div>
  )
}

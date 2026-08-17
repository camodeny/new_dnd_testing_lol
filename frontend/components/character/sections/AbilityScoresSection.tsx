import FormGroup from '@/components/common/FormGroup'
import NumberInput from '@/components/common/NumberInput'

const ABILITIES = ['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma']

interface Props {
  data: Record<string, number>
  onChange: (data: Record<string, number>) => void
}

export default function AbilityScoresSection({ data, onChange }: Props) {
  return (
    <div className="form-section">
      <h3>Ability Scores</h3>
      <div className="form-grid six-col">
        {ABILITIES.map((ability) => (
          <FormGroup key={ability} label={`${ability[0].toUpperCase()}${ability.slice(1)}`} htmlFor={`abl-${ability}`}>
            <NumberInput
              id={`abl-${ability}`}
              value={data[ability]}
              onChange={(value) => onChange({ ...data, [ability]: value ?? 10 })}
              min={1}
              max={30}
            />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

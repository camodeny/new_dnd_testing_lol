import FormGroup from '../../common/FormGroup'
import NumberInput from '../../common/NumberInput'

const ABILITIES = ['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma']

export default function AbilityScoresSection({ data, onChange }) {
  const set = (ability, value) => onChange({ ...data, [ability]: value })

  return (
    <div className="form-section">
      <h3>Ability Scores</h3>
      <div className="form-grid six-col">
        {ABILITIES.map((ability) => (
          <FormGroup key={ability} label={ability.charAt(0).toUpperCase() + ability.slice(1)} htmlFor={`abl-${ability}`}>
            <NumberInput
              id={`abl-${ability}`}
              value={data[ability]}
              onChange={(v) => set(ability, v)}
              min={1}
              max={30}
            />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

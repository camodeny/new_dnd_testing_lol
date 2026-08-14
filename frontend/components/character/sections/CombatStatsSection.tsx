import FormGroup from '@/components/common/FormGroup'
import NumberInput from '@/components/common/NumberInput'

const FIELDS = [
  ['max_hp', 'Max HP', 1, undefined],
  ['current_hp', 'Current HP', 0, undefined],
  ['temp_hp', 'Temp HP', 0, undefined],
  ['armor_class', 'Armor Class', 1, undefined],
  ['initiative_bonus', 'Initiative Bonus', undefined, undefined],
  ['speed', 'Speed', 0, undefined],
  ['death_save_successes', 'Death Save Successes', 0, 3],
  ['death_save_failures', 'Death Save Failures', 0, 3],
] as const

interface Props {
  data: Record<string, number>
  onChange: (data: Record<string, number>) => void
}

export default function CombatStatsSection({ data, onChange }: Props) {
  return (
    <div className="form-section">
      <h3>Combat Stats</h3>
      <div className="form-grid three-col">
        {FIELDS.map(([key, label, min, max]) => (
          <FormGroup key={key} label={label} htmlFor={`cmb-${key}`}>
            <NumberInput
              id={`cmb-${key}`}
              value={data[key]}
              onChange={(value) => onChange({ ...data, [key]: value ?? 0 })}
              min={min}
              max={max}
            />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

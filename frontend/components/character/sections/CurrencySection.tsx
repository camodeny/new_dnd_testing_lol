import FormGroup from '@/components/common/FormGroup'
import NumberInput from '@/components/common/NumberInput'

const COINS = [
  ['cp', 'Copper (CP)'],
  ['sp', 'Silver (SP)'],
  ['ep', 'Electrum (EP)'],
  ['gp', 'Gold (GP)'],
  ['pp', 'Platinum (PP)'],
] as const

interface Props {
  data: Record<string, number>
  onChange: (data: Record<string, number>) => void
}

export default function CurrencySection({ data, onChange }: Props) {
  return (
    <div className="form-section">
      <h3>Currency</h3>
      <div className="form-grid five-col">
        {COINS.map(([key, label]) => (
          <FormGroup key={key} label={label} htmlFor={`cur-${key}`}>
            <NumberInput
              id={`cur-${key}`}
              value={data[key]}
              onChange={(value) => onChange({ ...data, [key]: value ?? 0 })}
              min={0}
            />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

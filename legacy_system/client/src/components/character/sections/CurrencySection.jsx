import FormGroup from '../../common/FormGroup'
import NumberInput from '../../common/NumberInput'

const COINS = [
  { key: 'cp', label: 'Copper (CP)' },
  { key: 'sp', label: 'Silver (SP)' },
  { key: 'ep', label: 'Electrum (EP)' },
  { key: 'gp', label: 'Gold (GP)' },
  { key: 'pp', label: 'Platinum (PP)' },
]

export default function CurrencySection({ data, onChange }) {
  const set = (key, value) => onChange({ ...data, [key]: value })

  return (
    <div className="form-section">
      <h3>Currency</h3>
      <div className="form-grid five-col">
        {COINS.map((coin) => (
          <FormGroup key={coin.key} label={coin.label} htmlFor={`cur-${coin.key}`}>
            <NumberInput id={`cur-${coin.key}`} value={data[coin.key]} onChange={(v) => set(coin.key, v)} min={0} />
          </FormGroup>
        ))}
      </div>
    </div>
  )
}

import FormGroup from '@/components/common/FormGroup'
import Input from '@/components/common/Input'
import NumberInput from '@/components/common/NumberInput'
import type { CharacterDraft } from '../characterFormConfig'

type Spellcasting = CharacterDraft['spellcasting']

interface Props {
  data: Spellcasting
  onChange: (data: Spellcasting) => void
}

export default function SpellcastingSection({ data, onChange }: Props) {
  const set = <K extends keyof Spellcasting>(field: K, value: Spellcasting[K]) =>
    onChange({ ...data, [field]: value })

  const setSlot = (level: number, field: 'max' | 'used', value: number | null) => {
    const key = String(level)
    set('spell_slots', {
      ...data.spell_slots,
      [key]: { ...data.spell_slots[key], [field]: value ?? 0 },
    })
  }

  return (
    <div className="form-section">
      <h3>Spellcasting</h3>
      <div className="form-grid three-col">
        <FormGroup label="Spellcasting Ability" htmlFor="spc-ability">
          <Input
            id="spc-ability"
            value={data.spellcasting_ability}
            onChange={(event) => set('spellcasting_ability', event.target.value)}
            placeholder="INT, WIS, CHA"
          />
        </FormGroup>
        <FormGroup label="Spell Save DC" htmlFor="spc-dc">
          <NumberInput id="spc-dc" value={data.spell_save_dc} onChange={(value) => set('spell_save_dc', value)} />
        </FormGroup>
        <FormGroup label="Spell Attack Bonus" htmlFor="spc-attack">
          <NumberInput id="spc-attack" value={data.spell_attack_bonus} onChange={(value) => set('spell_attack_bonus', value)} />
        </FormGroup>
      </div>
      <h4 className="spell-slots-heading">Spell Slots</h4>
      <div className="form-grid five-col spell-slots-grid">
        {Array.from({ length: 9 }, (_, index) => index + 1).map((level) => (
          <div key={level} className="slot-box">
            <span className="slot-box__label">Lvl {level}</span>
            <div className="slot-inputs">
              <NumberInput
                aria-label={`Level ${level} maximum slots`}
                value={data.spell_slots[String(level)]?.max ?? 0}
                onChange={(value) => setSlot(level, 'max', value)}
                min={0}
                placeholder="Max"
              />
              <NumberInput
                aria-label={`Level ${level} used slots`}
                value={data.spell_slots[String(level)]?.used ?? 0}
                onChange={(value) => setSlot(level, 'used', value)}
                min={0}
                placeholder="Used"
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

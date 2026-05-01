import FormGroup from '../../common/FormGroup'
import Input from '../../common/Input'
import NumberInput from '../../common/NumberInput'

export default function SpellcastingSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })
  const setSlot = (level, key, value) => {
    const next = { ...data.spell_slots, [level]: { ...data.spell_slots[level], [key]: value } }
    onChange({ ...data, spell_slots: next })
  }

  return (
    <div className="form-section">
      <h3>Spellcasting</h3>
      <div className="form-grid three-col">
        <FormGroup label="Spellcasting Ability" htmlFor="spc-ability">
          <Input id="spc-ability" value={data.spellcasting_ability || ''} onChange={(e) => set('spellcasting_ability', e.target.value)} placeholder="INT, WIS, CHA" />
        </FormGroup>
        <FormGroup label="Spell Save DC" htmlFor="spc-dc">
          <NumberInput id="spc-dc" value={data.spell_save_dc} onChange={(v) => set('spell_save_dc', v)} />
        </FormGroup>
        <FormGroup label="Spell Attack Bonus" htmlFor="spc-atk">
          <NumberInput id="spc-atk" value={data.spell_attack_bonus} onChange={(v) => set('spell_attack_bonus', v)} />
        </FormGroup>
      </div>
      <h4>Spell Slots</h4>
      <div className="form-grid five-col">
        {Array.from({ length: 9 }, (_, i) => i + 1).map((lvl) => (
          <div key={lvl} className="slot-box">
            <label>Lvl {lvl}</label>
            <div className="slot-inputs">
              <NumberInput
                value={data.spell_slots?.[lvl]?.max ?? 0}
                onChange={(v) => setSlot(lvl, 'max', v)}
                placeholder="Max"
              />
              <NumberInput
                value={data.spell_slots?.[lvl]?.used ?? 0}
                onChange={(v) => setSlot(lvl, 'used', v)}
                placeholder="Used"
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

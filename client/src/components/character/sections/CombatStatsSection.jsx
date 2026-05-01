import FormGroup from '../../common/FormGroup'
import NumberInput from '../../common/NumberInput'

export default function CombatStatsSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Combat Stats</h3>
      <div className="form-grid three-col">
        <FormGroup label="Max HP" htmlFor="cmb-max-hp">
          <NumberInput id="cmb-max-hp" value={data.max_hp} onChange={(v) => set('max_hp', v)} min={1} />
        </FormGroup>
        <FormGroup label="Current HP" htmlFor="cmb-cur-hp">
          <NumberInput id="cmb-cur-hp" value={data.current_hp} onChange={(v) => set('current_hp', v)} min={0} />
        </FormGroup>
        <FormGroup label="Temp HP" htmlFor="cmb-temp-hp">
          <NumberInput id="cmb-temp-hp" value={data.temp_hp} onChange={(v) => set('temp_hp', v)} min={0} />
        </FormGroup>
        <FormGroup label="Armor Class" htmlFor="cmb-ac">
          <NumberInput id="cmb-ac" value={data.armor_class} onChange={(v) => set('armor_class', v)} min={1} />
        </FormGroup>
        <FormGroup label="Initiative Bonus" htmlFor="cmb-init">
          <NumberInput id="cmb-init" value={data.initiative_bonus} onChange={(v) => set('initiative_bonus', v)} />
        </FormGroup>
        <FormGroup label="Speed" htmlFor="cmb-speed">
          <NumberInput id="cmb-speed" value={data.speed} onChange={(v) => set('speed', v)} min={0} />
        </FormGroup>
        <FormGroup label="Death Save Successes" htmlFor="cmb-ds-succ">
          <NumberInput id="cmb-ds-succ" value={data.death_save_successes} onChange={(v) => set('death_save_successes', v)} min={0} max={3} />
        </FormGroup>
        <FormGroup label="Death Save Failures" htmlFor="cmb-ds-fail">
          <NumberInput id="cmb-ds-fail" value={data.death_save_failures} onChange={(v) => set('death_save_failures', v)} min={0} max={3} />
        </FormGroup>
      </div>
    </div>
  )
}

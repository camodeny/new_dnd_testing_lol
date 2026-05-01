import FormGroup from '../../common/FormGroup'
import Input from '../../common/Input'
import NumberInput from '../../common/NumberInput'

export default function BasicInfoSection({ data, onChange }) {
  const set = (field, value) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Basic Info</h3>
      <div className="form-grid two-col">
        <FormGroup label="Character Name" htmlFor="char-name">
          <Input id="char-name" value={data.name} onChange={(e) => set('name', e.target.value)} required />
        </FormGroup>
        <FormGroup label="Player Name" htmlFor="char-player">
          <Input id="char-player" value={data.player_name || ''} onChange={(e) => set('player_name', e.target.value)} />
        </FormGroup>
        <FormGroup label="Race" htmlFor="char-race">
          <Input id="char-race" value={data.race} onChange={(e) => set('race', e.target.value)} required />
        </FormGroup>
        <FormGroup label="Subrace" htmlFor="char-subrace">
          <Input id="char-subrace" value={data.subrace || ''} onChange={(e) => set('subrace', e.target.value)} />
        </FormGroup>
        <FormGroup label="Alignment" htmlFor="char-alignment">
          <Input id="char-alignment" value={data.alignment || ''} onChange={(e) => set('alignment', e.target.value)} />
        </FormGroup>
        <FormGroup label="Background" htmlFor="char-background">
          <Input id="char-background" value={data.background || ''} onChange={(e) => set('background', e.target.value)} />
        </FormGroup>
        <FormGroup label="Experience Points" htmlFor="char-xp">
          <NumberInput id="char-xp" value={data.experience_points} onChange={(v) => set('experience_points', v)} min={0} />
        </FormGroup>
        <FormGroup label="Total Level" htmlFor="char-level">
          <NumberInput id="char-level" value={data.total_level} onChange={(v) => set('total_level', v)} min={1} max={20} />
        </FormGroup>
      </div>
    </div>
  )
}

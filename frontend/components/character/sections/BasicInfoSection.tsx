import FormGroup from '@/components/common/FormGroup'
import Input from '@/components/common/Input'
import NumberInput from '@/components/common/NumberInput'
import type { CharacterDraft } from '../characterFormConfig'

interface Props {
  data: CharacterDraft
  onChange: (data: CharacterDraft) => void
}

export default function BasicInfoSection({ data, onChange }: Props) {
  const set = (field: keyof CharacterDraft, value: unknown) => onChange({ ...data, [field]: value })

  return (
    <div className="form-section">
      <h3>Basic Info</h3>
      <div className="form-grid two-col">
        <FormGroup label="Character Name" htmlFor="char-name">
          <Input id="char-name" value={(data.name as string) ?? ''} onChange={(event) => set('name', event.target.value)} required />
        </FormGroup>
        <FormGroup label="Player Name" htmlFor="char-player">
          <Input id="char-player" value={(data.player_name as string) ?? ''} onChange={(event) => set('player_name', event.target.value)} />
        </FormGroup>
        <FormGroup label="Race" htmlFor="char-race">
          <Input id="char-race" value={(data.race as string) ?? ''} onChange={(event) => set('race', event.target.value)} required />
        </FormGroup>
        <FormGroup label="Subrace" htmlFor="char-subrace">
          <Input id="char-subrace" value={(data.subrace as string) ?? ''} onChange={(event) => set('subrace', event.target.value)} />
        </FormGroup>
        <FormGroup label="Alignment" htmlFor="char-alignment">
          <Input id="char-alignment" value={(data.alignment as string) ?? ''} onChange={(event) => set('alignment', event.target.value)} />
        </FormGroup>
        <FormGroup label="Background" htmlFor="char-background">
          <Input id="char-background" value={(data.background as string) ?? ''} onChange={(event) => set('background', event.target.value)} />
        </FormGroup>
        <FormGroup label="Experience Points" htmlFor="char-xp">
          <NumberInput id="char-xp" value={data.experience_points} onChange={(value) => set('experience_points', value ?? 0)} min={0} />
        </FormGroup>
        <FormGroup label="Total Level" htmlFor="char-level">
          <NumberInput id="char-level" value={data.total_level} onChange={(value) => set('total_level', value ?? 1)} min={1} max={20} />
        </FormGroup>
      </div>
    </div>
  )
}

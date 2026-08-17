import type { Character } from '@/types'

const ABILITY_LABELS: [keyof Character, string][] = [
  ['strength', 'STR'],
  ['dexterity', 'DEX'],
  ['constitution', 'CON'],
  ['intelligence', 'INT'],
  ['wisdom', 'WIS'],
  ['charisma', 'CHA'],
]

function abilityMod(score?: number): string {
  if (score == null) return '—'
  const mod = Math.floor((score - 10) / 2)
  return mod >= 0 ? `+${mod}` : String(mod)
}

interface CharacterCardProps {
  character: Character
}

export default function CharacterCard({ character }: CharacterCardProps) {
  const classLabel = character.classes?.map((c) => `${c.class_name} ${c.level}`).join(' / ') ?? ''
  const subtitle = [character.race, character.background].filter(Boolean).join(' · ')

  return (
    <div className="character-card">
      <div>
        <h3 id={`character-${character.id}-name`}>{character.name}</h3>
        <div className="card-subtitle">{subtitle}</div>
      </div>
      {classLabel && (
        <div className="card-stats">
          <span>{classLabel}</span>
          {character.hit_points != null && (
            <span>{character.hit_points} HP</span>
          )}
        </div>
      )}
      <div className="card-abilities">
        {ABILITY_LABELS.map(([key, label]) => (
          <div key={key} className="ability-pill">
            {label} {abilityMod(character[key] as number | undefined)}
          </div>
        ))}
      </div>
    </div>
  )
}

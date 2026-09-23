import Link from 'next/link'
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

function modValue(score?: number): number | null {
  if (score == null) return null
  return Math.floor((score - 10) / 2)
}

interface CharacterCardProps {
  character: Character
  layout?: 'strip' | 'grid' | 'sheet' | 'spotlight' | 'bars' | 'feature'
}

export default function CharacterCard({ character, layout = 'strip' }: CharacterCardProps) {
  const classLabel = character.classes?.map((c) => `${c.class_name} ${c.level}`).join(' / ') ?? ''
  const subtitle = [character.race, character.background].filter(Boolean).join(' · ')

  if (layout === 'sheet') {
    return (
      <div className="character-card character-card--sheet">
        <div className="card-identity">
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
        <div className="sheet-scores">
          {ABILITY_LABELS.map(([key, label]) => (
            <div key={key} className="score-box">
              <span className="score-label">{label}</span>
              <span className="score-mod">{abilityMod(character[key] as number | undefined)}</span>
              <span className="score-value">{(character[key] as number | undefined) ?? '—'}</span>
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (layout === 'bars') {
    return (
      <div className="character-card character-card--bars">
        <div className="card-identity">
          <h3 id={`character-${character.id}-name`}>{character.name}</h3>
          <div className="card-subtitle">{subtitle}</div>
          {classLabel && (
            <div className="card-stats card-stats--inline">
              <span>{classLabel}</span>
              {character.hit_points != null && (
                <span>{character.hit_points} HP</span>
              )}
            </div>
          )}
        </div>
        <div className="bars-block">
          {ABILITY_LABELS.map(([key, label]) => {
            const mod = modValue(character[key] as number | undefined)
            const width = mod == null ? 0 : Math.max(0, Math.min(100, ((mod + 5) / 11) * 100))
            return (
              <div key={key} className="bar-row">
                <span className="bar-label">{label}</span>
                <span className="bar-track">
                  <span className="bar-fill" style={{ width: `${width}%` }} />
                </span>
                <span className="bar-mod">{mod == null ? '—' : mod >= 0 ? `+${mod}` : String(mod)}</span>
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  if (layout === 'feature') {
    return (
      <div className="character-card character-card--feature">
        <div className="card-identity">
          <h3 id={`character-${character.id}-name`}>
            <Link href={`/characters/${character.id}`}>{character.name}</Link>
          </h3>
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

  return (
    <div className={`character-card character-card--${layout}`}>
      <div className="card-identity">
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

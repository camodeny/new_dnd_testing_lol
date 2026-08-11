import { Link } from 'react-router-dom'

export default function CharacterCard({ character }) {
  const combat = character.combat || {}
  const abilityScores = character.ability_scores || {}

  return (
    <Link
      to={`/characters/${character.id}`}
      className="card character-card character-card-link"
      aria-label={`View character ${character.name}`}
    >
      <h3 id={`character-${character.id}-name`}>{character.name}</h3>
      <p className="card-subtitle">
        {character.race} &middot; Level {character.total_level} &middot; {character.alignment}
      </p>
      <div className="card-stats">
        <span>HP {combat.current_hp ?? '?'}/{combat.max_hp ?? '?'}</span>
        <span>AC {combat.armor_class ?? '?'}</span>
      </div>
      <div className="card-abilities">
        {['str', 'dex', 'con', 'int', 'wis', 'cha'].map((short, i) => {
          const full = ['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma'][i]
          const val = abilityScores[full]
          const mod = val !== undefined ? Math.floor((val - 10) / 2) : '?'
          return (
            <span key={short} className="ability-pill">
              {short.toUpperCase()} {val ?? '?'} ({mod >= 0 ? `+${mod}` : mod})
            </span>
          )
        })}
      </div>
    </Link>
  )
}

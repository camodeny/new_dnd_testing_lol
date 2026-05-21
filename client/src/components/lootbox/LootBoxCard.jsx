export default function LootBoxCard({ box, isOwner, onOpen, disabled }) {
  if (!box) return null

  const name = box.name || 'Mysterious Chest'
  const description = box.description || ''
  const isUnopened = box.status === 'unopened'
  const playerCount = box.player_count || 0
  const itemCount = box.item_count || 0
  const draws = box.draws || {}
  const drawEntries = Object.entries(draws)

  const rarityColors = {
    common: '#8a8a8a',
    uncommon: '#1aab2e',
    rare: '#1a7aab',
    very_rare: '#9b2e9b',
  }

  return (
    <div className="loot-box-card">
      <div className="loot-box-card-header">
        <i className={`bi ${isUnopened ? 'bi-box-seam' : 'bi-box'}`}></i>
        <span className="loot-box-card-name">{name}</span>
      </div>

      {description && (
        <div className="loot-box-card-desc">{description}</div>
      )}

      {isUnopened && (
        <div className="loot-box-card-meta">
          {playerCount > 0 && <span>{playerCount} players</span>}
          {itemCount > 0 && <span>{itemCount} items</span>}
        </div>
      )}

      {isUnopened && isOwner && (
        <button
          className="btn btn-primary small loot-box-open-btn"
          onClick={() => onOpen?.(box.id)}
          disabled={disabled}
        >
          <i className="bi bi-dice-6"></i> {disabled ? 'Opening...' : 'Open Loot Box'}
        </button>
      )}

      {!isUnopened && drawEntries.length > 0 && (
        <div className="loot-box-draws">
          {drawEntries.map(([charId, draw]) => {
            if (!draw || !draw.item_name) return null
            const color = rarityColors[draw.item_rarity] || rarityColors.common
            return (
              <div key={charId} className="loot-box-draw-item">
                <span className="loot-box-draw-dot" style={{ backgroundColor: color }}></span>
                <span className="loot-box-draw-name">{draw.item_name}</span>
              </div>
            )
          })}
        </div>
      )}

      {!isUnopened && drawEntries.length === 0 && (
        <div className="loot-box-card-meta">No draws recorded.</div>
      )}
    </div>
  )
}

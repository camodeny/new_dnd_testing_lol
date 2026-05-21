import { useState, useRef } from 'react'
import './LootBoxOpening.css'

// Thematic D&D filler items to populate the roulette track with a rich variety of loot
const FILLER_ITEMS = [
  { name: 'Potion of Healing', rarity: 'common' },
  { name: 'Spell Scroll (1st Level)', rarity: 'common' },
  { name: 'Vial of Acid', rarity: 'common' },
  { name: 'Alchemist\'s Fire', rarity: 'common' },
  { name: '+1 Longsword', rarity: 'uncommon' },
  { name: 'Bag of Holding', rarity: 'uncommon' },
  { name: 'Cloak of Elvenkind', rarity: 'uncommon' },
  { name: 'Immovable Rod', rarity: 'uncommon' },
  { name: 'Broom of Flying', rarity: 'uncommon' },
  { name: 'Lantern of Revealing', rarity: 'uncommon' },
  { name: '+2 Greatsword', rarity: 'rare' },
  { name: 'Ring of Protection', rarity: 'rare' },
  { name: 'Cloak of Displacement', rarity: 'rare' },
  { name: 'Flame Tongue Sword', rarity: 'rare' },
  { name: 'Armor of Vulnerability', rarity: 'rare' },
  { name: '+3 Plate Armor', rarity: 'very_rare' },
  { name: 'Staff of Power', rarity: 'very_rare' },
  { name: 'Belt of Giant Strength', rarity: 'very_rare' },
  { name: 'Ring of Regeneration', rarity: 'very_rare' },
]

const RARITY_COLORS = {
  common: '#8a8a8a',
  uncommon: '#1aab2e',
  rare: '#1a7aab',
  very_rare: '#9b2e9b',
  legendary: '#d4a017',
}

// Web Audio API Synthesizer to create a tactile mechanical clicking noise
function playTickSound() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext
    if (!AudioContextClass) return
    
    const audioCtx = new AudioContextClass()
    const osc = audioCtx.createOscillator()
    const gainNode = audioCtx.createGain()
    
    osc.connect(gainNode)
    gainNode.connect(audioCtx.destination)
    
    // Quick mechanical tick/click sound
    osc.type = 'sine'
    osc.frequency.setValueAtTime(900, audioCtx.currentTime)
    osc.frequency.exponentialRampToValueAtTime(120, audioCtx.currentTime + 0.04)
    
    gainNode.gain.setValueAtTime(0.08, audioCtx.currentTime)
    gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.04)
    
    osc.start()
    osc.stop(audioCtx.currentTime + 0.04)
  } catch {
    // Ignore audio failures if context is blocked/unsupported
  }
}

function getInitials(name) {
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

function getGradientSeed(str) {
  let hash = 0
  for (let i = 0; i < str.length; i++) hash = str.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

export default function LootBoxOpeningModal({ box, characters, onClose }) {
  const [phase, setPhase] = useState('LOCKED') // LOCKED -> OPENING -> SPINNING -> REVEALED -> FINISHED
  const [revealIndex, setRevealIndex] = useState(0)
  const [track, setTrack] = useState([])
  const [translateStyle, setTranslateStyle] = useState({ transform: 'translateX(0px)', transition: 'none' })
  const [flashActive, setFlashActive] = useState(false)
  const [recapItems, setRecapItems] = useState([])

  const trackRef = useRef(null)
  const animFrameId = useRef(null)
  const containerRef = useRef(null)

  // Generate sparks background elements configuration deterministically to stay pure and lint-friendly
  const sparksConfig = Array.from({ length: 15 }).map((_, idx) => {
    const delay = `${((idx * 0.13) % 2).toFixed(2)}s`
    const xOffset = `${((idx * 17) % 80 - 40).toFixed(0)}px`
    const left = `${((idx * 23) % 100).toFixed(0)}%`
    return { id: idx, delay, xOffset, left }
  })

  if (!box) return null

  const name = box.name || 'Mysterious Chest'
  const draws = box.draws || {}
  const pools = box.pools || {}
  const characterIds = Object.keys(draws)

  const activeCharId = characterIds[revealIndex]
  const activeDraw = activeCharId ? draws[activeCharId] : null
  const activeChar = characters.find((c) => String(c.id) === activeCharId)
  const activeCharName = activeChar ? activeChar.name : 'Adventurer'

  // Generate sparks background elements for chest ceremony
  const sparks = sparksConfig.map((s) => (
    <div
      key={s.id}
      className="lootbox-spark"
      style={{
        left: s.left,
        animationDelay: s.delay,
        '--spark-x': s.xOffset,
      }}
    />
  ))

  // Start the opening ceremony (shaking chest -> burst)
  const handleUnlock = () => {
    // Unlock Audio Context immediately on click
    playTickSound()
    
    setPhase('OPENING')
    setTimeout(() => {
      setFlashActive(true)
      setTimeout(() => {
        setFlashActive(false)
        if (characterIds.length > 0) {
          startSpinForCharacter(0)
        } else {
          setPhase('FINISHED')
        }
      }, 300) // Flash length
    }, 1200) // Shaking duration
  }

  // Prepares the scrolling tape and triggers CSS transition
  const startSpinForCharacter = (index) => {
    const charId = characterIds[index]
    const wonItem = draws[charId]
    const charPool = pools[charId] || []
    
    // Compile roulette track items: 35 cards, index 28 is the winning item
    const compiledTrack = []
    const sourcePool = [...charPool, ...FILLER_ITEMS]
    
    for (let i = 0; i < 35; i++) {
      if (i === 28) {
        compiledTrack.push({
          name: wonItem.item_name,
          rarity: wonItem.item_rarity || 'common',
          isWinner: true,
        })
      } else {
        const rand = sourcePool[Math.floor(Math.random() * sourcePool.length)]
        compiledTrack.push({
          name: rand.name || rand.item_name,
          rarity: rand.rarity || rand.item_rarity || 'common',
        })
      }
    }

    setTrack(compiledTrack)
    setRevealIndex(index)
    setPhase('SPINNING')
    
    // Reset tape position instantly
    setTranslateStyle({ transform: 'translateX(0px)', transition: 'none' })

    // Set transition with decelerating cubic-bezier curve
    setTimeout(() => {
      if (!containerRef.current) return
      
      const windowWidth = containerRef.current.querySelector('.lootbox-roulette-window').offsetWidth
      const cardWidth = 120
      const cardGap = 8
      const step = cardWidth + cardGap // 128px
      
      // Calculate random offset to land slightly off-center for realism
      const randomOffset = Math.random() * 80 - 40 // +/- 40px
      const targetIndex = 28
      const finalX = -(targetIndex * step + cardWidth / 2 - windowWidth / 2 + randomOffset)

      setTranslateStyle({
        transform: `translateX(${finalX}px)`,
        transition: 'transform 5.5s cubic-bezier(0.1, 0.8, 0.15, 1)',
      })

      // Audio ticks tracking
      let lastTickIndex = -1
      const checkTicks = () => {
        if (!trackRef.current) return
        
        // Get dynamic TranslateX value
        const style = window.getComputedStyle(trackRef.current)
        const matrix = new DOMMatrixReadOnly(style.transform)
        const currentX = matrix.m41

        // Determine current card index passing the center line
        const currentCardIndex = Math.floor((-currentX + windowWidth / 2) / step)
        
        if (currentCardIndex !== lastTickIndex && currentCardIndex >= 0 && currentCardIndex < 35) {
          lastTickIndex = currentCardIndex
          playTickSound()
        }
        
        animFrameId.current = requestAnimationFrame(checkTicks)
      }

      // Start ticker animation tracking
      animFrameId.current = requestAnimationFrame(checkTicks)

      // Conclude spin after transition ends
      setTimeout(() => {
        cancelAnimationFrame(animFrameId.current)
        setPhase('REVEALED')
        
        // Record item in the recap array
        setRecapItems((prev) => [
          ...prev,
          {
            charName: characters.find((c) => String(c.id) === charId)?.name || 'Adventurer',
            itemName: wonItem.item_name,
            itemRarity: wonItem.item_rarity,
            itemDesc: wonItem.item_description,
          },
        ])
      }, 5500)
    }, 50)
  }

  // Go to next character or finish
  const handleNext = () => {
    const nextIdx = revealIndex + 1
    if (nextIdx < characterIds.length) {
      startSpinForCharacter(nextIdx)
    } else {
      setPhase('FINISHED')
    }
  }

  // Instantly reveal all and show recap summary
  const handleSkipAll = () => {
    cancelAnimationFrame(animFrameId.current)
    const skippedRecap = characterIds.map((charId) => {
      const won = draws[charId]
      return {
        charName: characters.find((c) => String(c.id) === charId)?.name || 'Adventurer',
        itemName: won.item_name,
        itemRarity: won.item_rarity,
        itemDesc: won.item_description,
      }
    })
    setRecapItems(skippedRecap)
    setPhase('FINISHED')
  }

  // Avatar variables for active character
  const activeCharBg = activeChar ? getGradientSeed(activeCharName) : 'var(--color-primary)'
  const activeCharInitials = activeChar ? getInitials(activeCharName) : '?'

  return (
    <div className="lootbox-opening-overlay">
      <div className="lootbox-opening-container" ref={containerRef}>
        {/* Flash Overlay Effect */}
        <div className={`lootbox-flash-overlay ${flashActive ? 'active' : ''}`} />

        {/* Header */}
        <div className="lootbox-opening-title-row">
          <div className="lootbox-opening-subtitle">Loot Drop Opening</div>
          <h2 className="lootbox-opening-title">{name}</h2>
        </div>

        {/* Phase: Locked */}
        {phase === 'LOCKED' && (
          <div className="lootbox-chest-ceremony" onClick={handleUnlock}>
            <div className="lootbox-particle-container">{sparks}</div>
            <div className="lootbox-chest-glow" />
            <i className="bi bi-box-seam lootbox-chest-sprite"></i>
            <div className="lootbox-chest-hint">Click the chest to unlock the party loot!</div>
          </div>
        )}

        {/* Phase: Shaking & Opening */}
        {phase === 'OPENING' && (
          <div className="lootbox-chest-ceremony">
            <div className="lootbox-particle-container">{sparks}</div>
            <div className="lootbox-chest-glow" style={{ animationDuration: '1s' }} />
            <i className="bi bi-box-seam lootbox-chest-sprite shaking"></i>
            <div className="lootbox-chest-hint" style={{ color: 'var(--text-gold)', fontWeight: 600 }}>Unlocking items...</div>
          </div>
        )}

        {/* Phase: Spinning */}
        {phase === 'SPINNING' && (
          <div className="lootbox-roulette-panel">
            <div className="lootbox-active-character-banner">
              <div className="lootbox-character-avatar-initials" style={{ background: activeCharBg }}>
                {activeCharInitials}
              </div>
              <div style={{ textAlign: 'left' }}>
                <div className="lootbox-character-name-label">{activeCharName}</div>
                <div className="lootbox-character-sub-label">Drawing Loot</div>
              </div>
            </div>

            <div className="lootbox-roulette-window">
              <div className="lootbox-roulette-pointer" />
              <div className="lootbox-roulette-track" ref={trackRef} style={translateStyle}>
                {track.map((item, idx) => (
                  <div key={idx} className={`lootbox-roulette-card lootbox-rarity-${item.rarity}`}>
                    <div className="lootbox-roulette-card-rarity">{item.rarity}</div>
                    <div className="lootbox-roulette-card-name">{item.name}</div>
                  </div>
                ))}
              </div>
            </div>

            <button className="btn btn-secondary small" onClick={handleSkipAll}>
              Skip Animation
            </button>
            <div className="lootbox-opening-step-indicator">
              Character {revealIndex + 1} of {characterIds.length}
            </div>
          </div>
        )}

        {/* Phase: Revealed item detail card */}
        {phase === 'REVEALED' && activeDraw && (
          <div className="lootbox-roulette-panel">
            <div className="lootbox-active-character-banner" style={{ border: 'none', background: 'transparent', marginBottom: 10 }}>
              <div className="lootbox-character-avatar-initials" style={{ background: activeCharBg }}>
                {activeCharInitials}
              </div>
              <div style={{ textAlign: 'left' }}>
                <div className="lootbox-character-name-label">{activeCharName}</div>
                <div className="lootbox-character-sub-label">Landed on</div>
              </div>
            </div>

            <div className={`lootbox-reveal-card ${activeDraw.item_rarity || 'common'}`}>
              <div className="lootbox-reveal-rarity-tag">{activeDraw.item_rarity || 'common'}</div>
              <h3 className="lootbox-reveal-item-name">{activeDraw.item_name}</h3>
              {activeDraw.item_description && (
                <div className="lootbox-reveal-item-desc">{activeDraw.item_description}</div>
              )}
              <div className="lootbox-reveal-recipient">
                <i className="bi bi-person-fill"></i> Bound to {activeCharName}
              </div>
            </div>

            <div className="lootbox-opening-controls">
              {revealIndex + 1 < characterIds.length ? (
                <>
                  <button className="btn btn-secondary" onClick={handleSkipAll}>
                    Skip Rest
                  </button>
                  <button className="btn btn-primary" onClick={handleNext}>
                    Next Spin <i className="bi bi-chevron-right"></i>
                  </button>
                </>
              ) : (
                <button className="btn btn-primary" onClick={() => setPhase('FINISHED')}>
                  Show Stash Summary
                </button>
              )}
            </div>
            <div className="lootbox-opening-step-indicator">
              Revealed {revealIndex + 1} of {characterIds.length}
            </div>
          </div>
        )}

        {/* Phase: Finished / Recap Screen */}
        {phase === 'FINISHED' && (
          <div style={{ animation: 'fadeIn 0.4s ease-out' }}>
            <h3 style={{ fontFamily: 'var(--sans)', color: 'var(--text-gold)', marginBottom: 24, fontSize: '1.4rem' }}>
              Loot Unlocked!
            </h3>
            
            <div style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 12,
              maxHeight: 300,
              overflowY: 'auto',
              marginBottom: 30,
              paddingRight: 6,
              textAlign: 'left',
            }}>
              {recapItems.map((item, idx) => {
                const col = RARITY_COLORS[item.itemRarity] || '#8a8a8a'
                return (
                  <div key={idx} style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '14px 18px',
                    background: 'rgba(255, 255, 255, 0.02)',
                    border: '1px solid var(--border-color)',
                    borderRadius: 'var(--radius-sm)',
                    gap: 16,
                  }}>
                    <div>
                      <div style={{ fontWeight: 600, color: 'var(--text-bright)', fontSize: '0.95rem' }}>{item.itemName}</div>
                      <div style={{ fontSize: '0.75rem', color: col, textTransform: 'uppercase', fontWeight: 800, marginTop: 2, letterSpacing: '0.5px' }}>
                        {item.itemRarity}
                      </div>
                    </div>
                    <div style={{
                      fontSize: '0.8rem',
                      color: 'var(--text-gold)',
                      background: 'rgba(245, 158, 11, 0.05)',
                      border: '1px solid rgba(245, 158, 11, 0.15)',
                      padding: '4px 12px',
                      borderRadius: 'var(--radius-xl)',
                      whiteSpace: 'nowrap',
                    }}>
                      {item.charName}
                    </div>
                  </div>
                )
              })}
            </div>

            <button className="btn btn-primary" onClick={onClose} style={{ minWidth: 150 }}>
              Close & Update Sheets
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

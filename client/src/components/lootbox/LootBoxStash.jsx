import { useState, useEffect } from 'react'
import { getLootBoxes, openLootBox } from '../../api/client'
import LootBoxCard from './LootBoxCard'
import LootBoxOpeningModal from './LootBoxOpeningModal'

export default function LootBoxStash({ campaignId, isOwner, onLootBoxOpened, characters = [] }) {
  const [boxes, setBoxes] = useState([])
  const [opening, setOpening] = useState(null)
  const [error, setError] = useState('')
  const [activeOpeningBox, setActiveOpeningBox] = useState(null)

  useEffect(() => {
    if (!campaignId) return

    let active = true
    const fetchBoxes = async () => {
      try {
        const data = await getLootBoxes(campaignId)
        if (active) {
          setBoxes((data.loot_boxes || []).filter((b) => b.status === 'unopened'))
        }
      } catch {
        // silently fail — the stash panel is non-critical
      }
    }

    fetchBoxes()
    const interval = setInterval(fetchBoxes, 15000)
    return () => {
      active = false
      clearInterval(interval)
    }
  }, [campaignId])

  const handleOpen = async (lootBoxId) => {
    setOpening(lootBoxId)
    setError('')
    try {
      const data = await openLootBox(lootBoxId)
      // Open the animation modal with the drawn details
      setActiveOpeningBox(data.loot_box)
    } catch (err) {
      setError(err.message || 'Failed to open loot box.')
    } finally {
      setOpening(null)
    }
  }

  const handleOpeningModalClose = async () => {
    if (!activeOpeningBox) return
    const openedId = activeOpeningBox.id
    
    // Remove from the local unopened boxes list
    setBoxes((prev) => prev.filter((b) => b.id !== openedId))
    
    // Refresh parent campaign details (proposals, messages)
    if (onLootBoxOpened) {
      await onLootBoxOpened()
    }
    
    setActiveOpeningBox(null)
  }

  return (
    <>
      {boxes.length === 0 ? (
        <div className="dashboard-sidebar-panel loot-box-stash-empty">
          <div className="loot-box-stash-title">
            <i className="bi bi-box-seam"></i> Loot Stash
          </div>
          <div className="loot-box-stash-empty-text">
            No loot boxes yet. The DM may drop one during the adventure.
          </div>
        </div>
      ) : (
        <div className="dashboard-sidebar-panel loot-box-stash">
          <div className="loot-box-stash-title">
            <i className="bi bi-box-seam"></i> Loot Stash ({boxes.length})
          </div>
          {error && <div className="error-banner">{error}</div>}
          <div className="loot-box-stash-list">
            {boxes.map((box) => (
              <LootBoxCard
                key={box.id}
                box={box}
                isOwner={isOwner}
                onOpen={handleOpen}
                disabled={opening === box.id}
              />
            ))}
          </div>
        </div>
      )}

      {activeOpeningBox && (
        <LootBoxOpeningModal
          box={activeOpeningBox}
          characters={characters}
          onClose={handleOpeningModalClose}
        />
      )}
    </>
  )
}

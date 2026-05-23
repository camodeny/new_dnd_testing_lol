import { useState, useEffect, useCallback } from 'react'
import { getShops, buyShopItem } from '../../api/client'
import './CampaignShops.css'

export default function CampaignShops({ campaignId, currentCharacter, onPurchaseSuccess }) {
  const [shops, setShops] = useState([])
  const [selectedShop, setSelectedShop] = useState(null)
  const [currentScene, setCurrentScene] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [buyingItem, setBuyingItem] = useState(null)

  const fetchShops = useCallback(async () => {
    try {
      const data = await getShops(campaignId)
      const list = data.shops || []
      setCurrentScene(data.current_scene || null)
      setShops(list)
      if (list.length > 0) {
        // Preserve selected shop if it still exists
        setSelectedShop((prev) => {
          if (prev) {
            const found = list.find((s) => s.id === prev.id)
            if (found) return found
          }
          return list[0]
        })
      } else {
        setSelectedShop(null)
      }
    } catch (err) {
      setError(err.message || 'Failed to fetch shops.')
    } finally {
      setLoading(false)
    }
  }, [campaignId])

  useEffect(() => {
    if (!campaignId) return
    const initialLoad = setTimeout(fetchShops, 0)
    // Poll for new shops every 10 seconds in case the AI DM creates one during active play
    const interval = setInterval(fetchShops, 10000)
    return () => {
      clearTimeout(initialLoad)
      clearInterval(interval)
    }
  }, [campaignId, fetchShops])

  const handleBuy = async (item) => {
    if (!currentCharacter) {
      setError('You must select a character in this campaign to buy items.')
      return
    }
    if (!selectedShop) return

    setBuyingItem(item.name)
    setError('')
    try {
      const data = await buyShopItem(selectedShop.id, currentCharacter.id, item.name)
      
      // Update local shops state with decremented quantity if applicable
      if (data.shop) {
        setShops((prev) =>
          prev.map((s) => (s.id === data.shop.id ? data.shop : s))
        )
        setSelectedShop(data.shop)
      }

      // Notify parent to refresh campaign logs/characters
      if (onPurchaseSuccess && data.character) {
        onPurchaseSuccess(data.character)
      }
    } catch (err) {
      setError(err.message || 'Failed to complete purchase.')
    } finally {
      setBuyingItem(null)
    }
  }

  if (loading) {
    return (
      <div className="shops-container loading-state">
        <div className="spinner"></div>
        <p>Browsing the local market...</p>
      </div>
    )
  }

  if (shops.length === 0) {
    const locationName = currentScene?.location_name
    return (
      <div className="shops-container empty-state">
        <div className="empty-icon"><i className="bi bi-shop"></i></div>
        <h3>No Local Merchants</h3>
        <p>
          {locationName
            ? `There are no available merchants at ${locationName}.`
            : "There are no available merchants at the party's current location."}
        </p>
      </div>
    )
  }

  return (
    <div className="shops-layout">
      <aside className="shops-sidebar">
        <div className="sidebar-title">Local Merchants</div>
        <div className="shop-selector-list">
          {shops.map((shop) => (
            <button
              key={shop.id}
              className={`shop-selector-btn ${selectedShop?.id === shop.id ? 'active' : ''}`}
              onClick={() => { setSelectedShop(shop); setError(''); }}
            >
              <div className="shop-avatar-icon"><i className="bi bi-storefront"></i></div>
              <div className="shop-btn-meta">
                <span className="shop-btn-name">{shop.name}</span>
                <span className="shop-btn-count">{shop.items?.length || 0} items</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      <main className="shops-main">
        {selectedShop && (
          <>
            <div className="shop-view-header">
              <div>
                <h3 className="shop-view-title">{selectedShop.name}</h3>
                {selectedShop.location_name && (
                  <p className="shop-view-location"><i className="bi bi-geo-alt"></i> {selectedShop.location_name}</p>
                )}
                <p className="shop-view-desc">{selectedShop.description}</p>
              </div>

              {currentCharacter && (
                <div className="wallet-card">
                  <span className="wallet-label">Your Purse</span>
                  <span className="wallet-amount">
                    <i className="bi bi-coin gold-coin"></i> {currentCharacter.gp ?? 0} gp
                  </span>
                  <span className="wallet-char-name">({currentCharacter.name})</span>
                </div>
              )}
            </div>

            {error && <div className="shops-error-banner"><i className="bi bi-exclamation-triangle-fill"></i> {error}</div>}

            <div className="shop-items-grid">
              {selectedShop.items?.map((item, idx) => {
                const isOutOfStock = item.quantity !== null && item.quantity <= 0
                const isAffordable = currentCharacter && (currentCharacter.gp ?? 0) >= item.cost_gp
                const canBuy = currentCharacter && !isOutOfStock && isAffordable
                const isProcessing = buyingItem === item.name

                return (
                  <div key={idx} className={`shop-item-card ${isOutOfStock ? 'out-of-stock' : ''}`}>
                    <div className="shop-item-header">
                      <h4 className="shop-item-name">{item.name}</h4>
                      <span className="shop-item-cost">
                        <i className="bi bi-coin gold-coin"></i> {item.cost_gp} gp
                      </span>
                    </div>

                    <p className="shop-item-desc">{item.description}</p>

                    <div className="shop-item-footer">
                      <span className={`stock-status ${isOutOfStock ? 'empty' : item.quantity !== null ? 'limited' : 'infinite'}`}>
                        {isOutOfStock ? (
                          'Out of Stock'
                        ) : item.quantity !== null ? (
                          `${item.quantity} left`
                        ) : (
                          'Available'
                        )}
                      </span>

                      <button
                        className="btn btn-primary small buy-btn"
                        disabled={!canBuy || isProcessing}
                        onClick={() => handleBuy(item)}
                      >
                        {isProcessing ? (
                          <>
                            <span className="buy-spinner"></span> Buying...
                          </>
                        ) : !currentCharacter ? (
                          'No Character'
                        ) : isOutOfStock ? (
                          'Sold Out'
                        ) : !isAffordable ? (
                          'Insufficient Gold'
                        ) : (
                          'Purchase'
                        )}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          </>
        )}
      </main>
    </div>
  )
}

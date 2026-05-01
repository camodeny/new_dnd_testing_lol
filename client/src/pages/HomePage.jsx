import { useState, useEffect, useCallback } from 'react'
import CampaignForm from '../CampaignForm'
import CampaignCard from '../components/campaign/CampaignCard'
import { getCampaigns } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

export default function HomePage() {
  const [campaigns, setCampaigns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [animatingOut, setAnimatingOut] = useState(false)

  useEffect(() => {
    getCampaigns()
      .then((data) => setCampaigns(data.campaigns || []))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  const openModal = () => {
    setAnimatingOut(false)
    setModalOpen(true)
    document.body.style.overflow = 'hidden'
  }

  const closeModal = () => {
    setAnimatingOut(true)
    setTimeout(() => {
      setModalOpen(false)
      document.body.style.overflow = ''
    }, 250)
  }

  const handleCampaignCreated = useCallback((newCampaign) => {
    setCampaigns((prev) => [newCampaign, ...prev])
    closeModal()
  }, [])

  if (loading) return <Loading />

  const hasCampaigns = campaigns.length > 0

  return (
    <div className="home-page-v2">
      <header className="campaigns-header">
        <div>
          <h1 className="campaigns-title">Your Campaigns</h1>
          <p className="campaigns-subtitle">
            {hasCampaigns
              ? `${campaigns.length} adventure${campaigns.length !== 1 ? 's' : ''} underway`
              : 'Where will your next adventure take you?'}
          </p>
        </div>
        {!hasCampaigns && (
          <button className="btn btn-primary create-first-btn" onClick={openModal}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New Campaign
          </button>
        )}
      </header>

      {hasCampaigns ? (
        <div className="campaigns-grid">
          {campaigns.map((campaign) => (
            <CampaignCard key={campaign.id} campaign={campaign} />
          ))}
        </div>
      ) : (
        <div className="empty-state-v2">
          <div className="empty-illustration">
            <svg viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="60" cy="60" r="50" stroke="currentColor" strokeWidth="1.5" opacity="0.15" />
              <circle cx="60" cy="60" r="35" stroke="currentColor" strokeWidth="1.5" opacity="0.25" />
              <path d="M60 30 L60 25 M60 95 L60 90 M30 60 L25 60 M95 60 L90 60" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.3" />
              <path d="M42 42 L38 38 M78 78 L82 82 M42 78 L38 82 M78 42 L82 38" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.3" />
              <path d="M45 60 Q52 45 60 55 Q68 65 75 60" stroke="currentColor" strokeWidth="2" strokeLinecap="round" opacity="0.5" />
              <circle cx="60" cy="60" r="3" fill="currentColor" opacity="0.6" />
            </svg>
          </div>
          <h3>No campaigns yet</h3>
          <p>Every great story starts with a single step. Create your first campaign to begin.</p>
        </div>
      )}

      <ErrorMessage message={error} />

      {/* Floating Action Button */}
      {hasCampaigns && (
        <button className="fab" onClick={openModal} aria-label="Create campaign">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
        </button>
      )}

      {/* Modal */}
      {modalOpen && (
        <div className={`modal-overlay ${animatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeModal}>
          <div className={`modal-panel ${animatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>New Campaign</h2>
              <button className="modal-close" onClick={closeModal} aria-label="Close">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <CampaignForm onCampaignCreated={handleCampaignCreated} onCancel={closeModal} />
          </div>
        </div>
      )}
    </div>
  )
}

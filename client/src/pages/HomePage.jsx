import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import CampaignForm from '../CampaignForm'
import CampaignCard from '../components/campaign/CampaignCard'
import { getCampaigns } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

export default function HomePage() {
  const navigate = useNavigate()
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
            <i className="bi bi-plus-lg"></i>
            New Campaign
          </button>
        )}
      </header>

      {hasCampaigns ? (
        <div className="campaigns-grid">
          {campaigns.map((campaign) => (
            <CampaignCard
              key={campaign.id}
              campaign={campaign}
              onClick={() => navigate(`/campaigns/${campaign.id}`)}
            />
          ))}
        </div>
      ) : (
        <div className="empty-state-v2">
          <i className="bi bi-book empty-illustration"></i>
          <h3>No campaigns yet</h3>
          <p>Every great story starts with a single step. Create your first campaign to begin.</p>
        </div>
      )}

      <ErrorMessage message={error} />

      {/* Floating Action Button */}
      {hasCampaigns && (
        <button className="fab" onClick={openModal} aria-label="Create campaign">
          <i className="bi bi-plus-lg" style={{ fontSize: 24 }}></i>
        </button>
      )}

      {/* Modal */}
      {modalOpen && (
        <div className={`modal-overlay ${animatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeModal}>
          <div className={`modal-panel ${animatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>New Campaign</h2>
              <button className="modal-close" onClick={closeModal} aria-label="Close">
                <i className="bi bi-x-lg"></i>
              </button>
            </div>
            <CampaignForm onCampaignCreated={handleCampaignCreated} onCancel={closeModal} />
          </div>
        </div>
      )}
    </div>
  )
}

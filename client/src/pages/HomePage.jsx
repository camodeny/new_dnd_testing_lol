import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import CampaignForm from '../CampaignForm'
import CampaignCard from '../components/campaign/CampaignCard'
import CombatSandboxForm from '../components/campaign/CombatSandboxForm'
import AutomationKeyManager from '../components/dashboard/AutomationKeyManager'
import { getCampaigns, lookupInvite, deleteCampaign } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

export default function HomePage({ user }) {
  const navigate = useNavigate()
  const [campaigns, setCampaigns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [animatingOut, setAnimatingOut] = useState(false)
  const [sandboxModalOpen, setSandboxModalOpen] = useState(false)
  const [sandboxAnimatingOut, setSandboxAnimatingOut] = useState(false)

  // Join Campaign Modal States
  const [joinModalOpen, setJoinModalOpen] = useState(false)
  const [joinAnimatingOut, setJoinAnimatingOut] = useState(false)
  const [inviteCode, setInviteCode] = useState('')
  const [joinError, setJoinError] = useState('')
  const [joinLoading, setJoinLoading] = useState(false)

  // Delete Campaign Modal States
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [deleteAnimatingOut, setDeleteAnimatingOut] = useState(false)
  const [campaignToDelete, setCampaignToDelete] = useState(null)
  const [deleteLoading, setDeleteLoading] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [automationModalOpen, setAutomationModalOpen] = useState(false)
  const [automationAnimatingOut, setAutomationAnimatingOut] = useState(false)

  useEffect(() => {
    const hasModalOpen = modalOpen || sandboxModalOpen || joinModalOpen || deleteModalOpen || automationModalOpen
    document.body.style.overflow = hasModalOpen ? 'hidden' : ''
    return () => {
      document.body.style.overflow = ''
    }
  }, [modalOpen, sandboxModalOpen, joinModalOpen, deleteModalOpen, automationModalOpen])

  const openDeleteModal = (campaign) => {
    setDeleteAnimatingOut(false)
    setDeleteError('')
    setCampaignToDelete(campaign)
    setDeleteModalOpen(true)
  }

  const closeDeleteModal = () => {
    setDeleteAnimatingOut(true)
    setTimeout(() => {
      setDeleteModalOpen(false)
      setCampaignToDelete(null)
    }, 250)
  }

  const handleConfirmDelete = async () => {
    if (!campaignToDelete) return
    setDeleteLoading(true)
    setDeleteError('')
    try {
      await deleteCampaign(campaignToDelete.id)
      setCampaigns((prev) => prev.filter((c) => c.id !== campaignToDelete.id))
      closeDeleteModal()
    } catch (err) {
      setDeleteError(err.message || 'Failed to delete campaign.')
    } finally {
      setDeleteLoading(false)
    }
  }

  useEffect(() => {
    getCampaigns()
      .then((data) => setCampaigns(data.campaigns || []))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [])

  const openModal = () => {
    setAnimatingOut(false)
    setModalOpen(true)
  }

  const openSandboxModal = () => {
    setSandboxAnimatingOut(false)
    setSandboxModalOpen(true)
  }

  const closeModal = () => {
    setAnimatingOut(true)
    setTimeout(() => {
      setModalOpen(false)
    }, 250)
  }

  const closeSandboxModal = () => {
    setSandboxAnimatingOut(true)
    setTimeout(() => {
      setSandboxModalOpen(false)
    }, 250)
  }

  const handleCampaignCreated = useCallback((newCampaign) => {
    setCampaigns((prev) => [newCampaign, ...prev])
    closeModal()
  }, [])

  const handleSandboxCreated = useCallback((newCampaign) => {
    setCampaigns((prev) => [newCampaign, ...prev.filter((campaign) => campaign.id !== newCampaign.id)])
    closeSandboxModal()
    navigate(`/campaigns/${newCampaign.id}`)
  }, [navigate])

  const openJoinModal = () => {
    setJoinAnimatingOut(false)
    setInviteCode('')
    setJoinError('')
    setJoinModalOpen(true)
  }

  const closeJoinModal = () => {
    setJoinAnimatingOut(true)
    setTimeout(() => {
      setJoinModalOpen(false)
    }, 250)
  }

  const openAutomationModal = () => {
    setAutomationAnimatingOut(false)
    setAutomationModalOpen(true)
  }

  const closeAutomationModal = () => {
    setAutomationAnimatingOut(true)
    setTimeout(() => {
      setAutomationModalOpen(false)
    }, 250)
  }

  const handleJoinSubmit = async (e) => {
    e.preventDefault()
    const cleanCode = inviteCode.trim()
    if (!cleanCode) {
      setJoinError('Please enter an invite code.')
      return
    }
    setJoinLoading(true)
    setJoinError('')
    try {
      const data = await lookupInvite(cleanCode)
      closeJoinModal()
      navigate(`/join/${data.campaign_id}?code=${encodeURIComponent(cleanCode.toUpperCase())}`)
    } catch (err) {
      setJoinError(err.message || 'Failed to locate campaign. Check your code and try again.')
    } finally {
      setJoinLoading(false)
    }
  }

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
        {hasCampaigns && (
          <div className="campaigns-header-actions">
            <button className="btn btn-secondary" onClick={() => navigate('/automation')}>
              <i className="bi bi-activity"></i>
              Automation
            </button>
            <button className="btn btn-secondary" onClick={openAutomationModal}>
              <i className="bi bi-cpu"></i>
              Automation Keys
            </button>
            <button className="btn btn-secondary join-btn-header" onClick={openJoinModal}>
              <i className="bi bi-key-fill"></i>
              Join with Code
            </button>
            <button className="btn btn-secondary" onClick={openSandboxModal}>
              <i className="bi bi-bullseye"></i>
              Combat Sandbox
            </button>
            <button className="btn btn-primary create-btn-header" onClick={openModal}>
              <i className="bi bi-plus-lg"></i>
              New Campaign
            </button>
          </div>
        )}
      </header>

      {hasCampaigns ? (
        <div className="campaigns-grid">
          {campaigns.map((campaign) => (
            <CampaignCard
              key={campaign.id}
              campaign={campaign}
              onClick={() => navigate(`/campaigns/${campaign.id}`)}
              onDelete={user?.id === campaign.user_id ? () => openDeleteModal(campaign) : null}
            />
          ))}
        </div>
      ) : (
        <div className="empty-state-v2">
          <i className="bi bi-book empty-illustration"></i>
          <h3>No campaigns yet</h3>
          <p>Every great story starts with a single step. Create your first campaign or join one with a code.</p>
          <div className="empty-state-actions">
            <button className="btn btn-primary" onClick={openModal}>
              <i className="bi bi-plus-lg"></i> New Campaign
            </button>
            <button className="btn btn-secondary" onClick={openJoinModal}>
              <i className="bi bi-key-fill"></i> Join with Code
            </button>
            <button className="btn btn-secondary" onClick={openSandboxModal}>
              <i className="bi bi-bullseye"></i> Combat Sandbox
            </button>
          </div>
        </div>
      )}

      <ErrorMessage message={error} />


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

      {sandboxModalOpen && (
        <div className={`modal-overlay ${sandboxAnimatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeSandboxModal}>
          <div className={`modal-panel ${sandboxAnimatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Combat Sandbox</h2>
              <button className="modal-close" onClick={closeSandboxModal} aria-label="Close">
                <i className="bi bi-x-lg"></i>
              </button>
            </div>
            <CombatSandboxForm onCreated={handleSandboxCreated} onCancel={closeSandboxModal} />
          </div>
        </div>
      )}

      {automationModalOpen && (
        <div className={`modal-overlay ${automationAnimatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeAutomationModal}>
          <div className={`modal-panel ${automationAnimatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Automation Keys</h2>
              <button className="modal-close" onClick={closeAutomationModal} aria-label="Close">
                <i className="bi bi-x-lg"></i>
              </button>
            </div>
            <AutomationKeyManager />
          </div>
        </div>
      )}

      {/* Join Modal */}
      {joinModalOpen && (
        <div className={`modal-overlay ${joinAnimatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeJoinModal}>
          <div className={`modal-panel ${joinAnimatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>Join Campaign</h2>
              <button className="modal-close" onClick={closeJoinModal} aria-label="Close">
                <i className="bi bi-x-lg"></i>
              </button>
            </div>
            <form onSubmit={handleJoinSubmit} className="campaign-form-compact join-modal-form">
              <div className="form-field">
                <label htmlFor="invite-code-input">Invite Code</label>
                <input
                  id="invite-code-input"
                  type="text"
                  placeholder="e.g., ABCDEFGH"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                  maxLength={20}
                  required
                  autoFocus
                  disabled={joinLoading}
                />
                <p className="form-help-text" style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '6px' }}>
                  Ask the campaign owner for the invite code.
                </p>
              </div>

              {joinError && (
                <div className="error-message" style={{ padding: '12px', margin: '0' }}>
                  <i className="bi bi-exclamation-triangle-fill" style={{ marginRight: '8px' }}></i>
                  {joinError}
                </div>
              )}

              <div className="form-actions-compact">
                <button type="button" className="btn btn-secondary" onClick={closeJoinModal} disabled={joinLoading}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={joinLoading || !inviteCode.trim()}>
                  {joinLoading ? 'Checking...' : 'Find Campaign'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteModalOpen && (
        <div className={`modal-overlay ${deleteAnimatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeDeleteModal}>
          <div className={`modal-panel ${deleteAnimatingOut ? 'slide-down' : 'slide-up'}`} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 style={{ color: 'var(--color-danger)' }}>Delete Campaign</h2>
              <button className="modal-close" onClick={closeDeleteModal} aria-label="Close">
                <i className="bi bi-x-lg"></i>
              </button>
            </div>
            <div className="campaign-form-compact">
              <p style={{ margin: 0, lineHeight: '1.6', color: 'var(--text-main)' }}>
                Are you sure you want to delete the campaign <strong>{campaignToDelete?.name}</strong>?
              </p>
              <p style={{ margin: 0, fontSize: '0.9rem', color: 'var(--color-danger)', fontWeight: '500' }}>
                <i className="bi bi-exclamation-triangle-fill" style={{ marginRight: '8px' }}></i>
                This action is permanent and will delete all associated sessions, characters, and campaign data.
              </p>
              
              {deleteError && (
                <div className="error-message" style={{ padding: '12px', margin: '0' }}>
                  <i className="bi bi-exclamation-triangle-fill" style={{ marginRight: '8px' }}></i>
                  {deleteError}
                </div>
              )}

              <div className="form-actions-compact">
                <button type="button" className="btn btn-secondary" onClick={closeDeleteModal} disabled={deleteLoading}>
                  Cancel
                </button>
                <button type="button" className="btn btn-danger" onClick={handleConfirmDelete} disabled={deleteLoading}>
                  {deleteLoading ? 'Deleting...' : 'Delete Campaign'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

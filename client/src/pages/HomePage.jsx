import { useState, useEffect, useCallback, useRef } from 'react'
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
  const dialogRef = useRef(null)
  const returnFocusRef = useRef(null)

  const rememberDialogTrigger = (event) => {
    returnFocusRef.current = event?.currentTarget instanceof HTMLElement
      ? event.currentTarget
      : document.activeElement
  }

  useEffect(() => {
    const hasModalOpen = modalOpen || sandboxModalOpen || joinModalOpen || deleteModalOpen || automationModalOpen
    document.body.style.overflow = hasModalOpen ? 'hidden' : ''
    return () => {
      document.body.style.overflow = ''
    }
  }, [modalOpen, sandboxModalOpen, joinModalOpen, deleteModalOpen, automationModalOpen])

  const openDeleteModal = (campaign, event) => {
    rememberDialogTrigger(event)
    setDeleteAnimatingOut(false)
    setDeleteError('')
    setCampaignToDelete(campaign)
    setDeleteModalOpen(true)
  }

  const closeDeleteModal = useCallback(() => {
    setDeleteAnimatingOut(true)
    setTimeout(() => {
      setDeleteModalOpen(false)
      setCampaignToDelete(null)
    }, 250)
  }, [])

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

  const openModal = (event) => {
    rememberDialogTrigger(event)
    setAnimatingOut(false)
    setModalOpen(true)
  }

  const openSandboxModal = (event) => {
    rememberDialogTrigger(event)
    setSandboxAnimatingOut(false)
    setSandboxModalOpen(true)
  }

  const closeModal = useCallback(() => {
    setAnimatingOut(true)
    setTimeout(() => {
      setModalOpen(false)
    }, 250)
  }, [])

  const closeSandboxModal = useCallback(() => {
    setSandboxAnimatingOut(true)
    setTimeout(() => {
      setSandboxModalOpen(false)
    }, 250)
  }, [])

  const handleCampaignCreated = useCallback((newCampaign) => {
    setCampaigns((prev) => [newCampaign, ...prev])
    closeModal()
  }, [closeModal])

  const handleSandboxCreated = useCallback((newCampaign) => {
    setCampaigns((prev) => [newCampaign, ...prev.filter((campaign) => campaign.id !== newCampaign.id)])
    closeSandboxModal()
    navigate(`/campaigns/${newCampaign.id}`)
  }, [closeSandboxModal, navigate])

  const openJoinModal = (event) => {
    rememberDialogTrigger(event)
    setJoinAnimatingOut(false)
    setInviteCode('')
    setJoinError('')
    setJoinModalOpen(true)
  }

  const closeJoinModal = useCallback(() => {
    setJoinAnimatingOut(true)
    setTimeout(() => {
      setJoinModalOpen(false)
    }, 250)
  }, [])

  const openAutomationModal = (event) => {
    rememberDialogTrigger(event)
    setAutomationAnimatingOut(false)
    setAutomationModalOpen(true)
  }

  const closeAutomationModal = useCallback(() => {
    setAutomationAnimatingOut(true)
    setTimeout(() => {
      setAutomationModalOpen(false)
    }, 250)
  }, [])

  const activeModal = modalOpen
    ? 'campaign'
    : sandboxModalOpen
      ? 'sandbox'
      : joinModalOpen
        ? 'join'
        : deleteModalOpen
          ? 'delete'
          : automationModalOpen
            ? 'automation'
            : null

  useEffect(() => {
    if (!activeModal) return undefined

    const previouslyFocused = returnFocusRef.current || document.activeElement
    const closeActiveModal = {
      campaign: closeModal,
      sandbox: closeSandboxModal,
      join: closeJoinModal,
      delete: closeDeleteModal,
      automation: closeAutomationModal,
    }[activeModal]
    const focusableSelector = [
      'a[href]',
      'button:not([disabled])',
      'input:not([disabled])',
      'select:not([disabled])',
      'textarea:not([disabled])',
      '[tabindex]:not([tabindex="-1"])',
    ].join(',')

    const focusFrame = window.requestAnimationFrame(() => {
      const dialog = dialogRef.current
      const preferredTarget = dialog?.querySelector('[autofocus]')
        || dialog?.querySelector('input:not([disabled]), select:not([disabled]), textarea:not([disabled])')
        || dialog?.querySelector(focusableSelector)
        || dialog
      preferredTarget?.focus()
    })

    const handleDialogKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeActiveModal()
        return
      }

      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = Array.from(dialogRef.current.querySelectorAll(focusableSelector))
        .filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true')

      if (focusable.length === 0) {
        event.preventDefault()
        dialogRef.current.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleDialogKeyDown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      document.removeEventListener('keydown', handleDialogKeyDown)
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
        previouslyFocused.focus()
      }
      returnFocusRef.current = null
    }
  }, [activeModal, closeAutomationModal, closeDeleteModal, closeJoinModal, closeModal, closeSandboxModal])

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
      <section className="campaign-hero">
        <div className="campaign-hero-copy">
          <span className="section-kicker">WELCOME TO FIRESIDE</span>
          <h1>Friends around the fire.<br />Adventure everywhere else.</h1>
          <p>{hasCampaigns ? `${campaigns.length} campaign${campaigns.length === 1 ? ' is' : 's are'} ready when your table is.` : 'Create a campaign or join a table with an invite code.'}</p>
          <div className="campaign-hero-actions">
            <button className="btn btn-primary" onClick={openModal}><i className="bi bi-plus-lg" aria-hidden="true" /> New campaign</button>
            <button className="btn btn-secondary" onClick={openJoinModal}><i className="bi bi-key" aria-hidden="true" /> Join with code</button>
          </div>
        </div>
        <div className="campaign-hero-art" aria-hidden="true">
          <span className="campaign-hero-grain" />
          <div className="campaign-hero-caption">
            <span className="campaign-hero-caption-mark">✦</span>
            <span>Gather here. Go anywhere.</span>
          </div>
        </div>
      </section>

      {hasCampaigns && (
        <>
          <header className="campaigns-header">
            <div>
              <span className="section-kicker">YOUR CAMPAIGNS</span>
              <h2 className="campaigns-title">Return to the table</h2>
            </div>
          </header>

          <div className="campaigns-grid">
            {campaigns.map((campaign) => (
              <CampaignCard
                key={campaign.id}
                campaign={campaign}
                onDelete={user?.id === campaign.user_id ? (event) => openDeleteModal(campaign, event) : null}
              />
            ))}
          </div>
        </>
      )}

      <ErrorMessage message={error} />

      <details className="home-utilities">
        <summary>Utilities</summary>
        <div className="home-utilities-menu">
          <button type="button" onClick={openAutomationModal}>
            <i className="bi bi-key" aria-hidden="true" />
            <span><strong>API keys</strong><small>Manage automation access</small></span>
          </button>
          <button type="button" onClick={openSandboxModal}>
            <i className="bi bi-crosshair" aria-hidden="true" />
            <span><strong>Combat sandbox</strong><small>Test an encounter setup</small></span>
          </button>
        </div>
      </details>


      {/* Modal */}
      {modalOpen && (
        <div className={`modal-overlay ${animatingOut ? 'fade-out' : 'fade-in'}`} onClick={closeModal}>
          <div ref={dialogRef} tabIndex="-1" className={`modal-panel ${animatingOut ? 'slide-down' : 'slide-up'}`} role="dialog" aria-modal="true" aria-labelledby="new-campaign-title" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 id="new-campaign-title">New campaign</h2>
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
          <div ref={dialogRef} tabIndex="-1" className={`modal-panel ${sandboxAnimatingOut ? 'slide-down' : 'slide-up'}`} role="dialog" aria-modal="true" aria-labelledby="combat-sandbox-title" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 id="combat-sandbox-title">Combat sandbox</h2>
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
          <div ref={dialogRef} tabIndex="-1" className={`modal-panel ${automationAnimatingOut ? 'slide-down' : 'slide-up'}`} role="dialog" aria-modal="true" aria-labelledby="api-keys-title" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 id="api-keys-title">API keys</h2>
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
          <div ref={dialogRef} tabIndex="-1" className={`modal-panel ${joinAnimatingOut ? 'slide-down' : 'slide-up'}`} role="dialog" aria-modal="true" aria-labelledby="join-campaign-title" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 id="join-campaign-title">Join a campaign</h2>
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
          <div ref={dialogRef} tabIndex="-1" className={`modal-panel ${deleteAnimatingOut ? 'slide-down' : 'slide-up'}`} role="alertdialog" aria-modal="true" aria-labelledby="delete-campaign-title" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2 id="delete-campaign-title" style={{ color: 'var(--color-danger)' }}>Delete campaign</h2>
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
                This action is permanent and will delete its sessions and campaign data. Characters stay in your character library.
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
                  {deleteLoading ? 'Deleting...' : 'Delete campaign'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

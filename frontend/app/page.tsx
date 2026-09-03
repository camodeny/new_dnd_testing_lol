'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { useAuthContext } from '@/contexts/AuthContext'
import { campaigns as campaignsApi, campaignMembers } from '@/lib/api'
import LandingPage from '@/components/landing/LandingPage'
import CampaignCard from '@/components/campaign/CampaignCard'
import CampaignForm from '@/components/campaign/CampaignForm'
import Modal from '@/components/common/Modal'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Campaign } from '@/types'

function HeroLock() {
  useEffect(() => {
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = '' }
  }, [])
  return null
}

type ActiveModal = 'create' | 'join' | 'delete' | null

export default function HomePage() {
  const { user } = useAuthContext()
  const router = useRouter()

  const [campaignList, setCampaignList] = useState<Campaign[]>([])
  const [loadingList, setLoadingList] = useState(true)
  const [activeModal, setActiveModal] = useState<ActiveModal>(null)

  // Join modal
  const [inviteCode, setInviteCode] = useState('')
  const [joinError, setJoinError] = useState('')
  const [joinLoading, setJoinLoading] = useState(false)

  // Delete modal
  const [campaignToDelete, setCampaignToDelete] = useState<Campaign | null>(null)
  const [deleteLoading, setDeleteLoading] = useState(false)
  const [deleteError, setDeleteError] = useState('')

  useEffect(() => {
    if (!user) {
      setLoadingList(false)
      return
    }
    campaignsApi
      .list()
      .then((data) => setCampaignList(data.campaigns ?? []))
      .catch(() => { /* show empty state when backend is unavailable */ })
      .finally(() => setLoadingList(false))
  }, [user])

  const handleCampaignCreated = (campaign: Campaign) => {
    setCampaignList((prev) => [campaign, ...prev])
    setActiveModal(null)
    router.push(`/campaigns/${campaign.id}`)
  }

  const openDelete = (campaign: Campaign) => {
    setCampaignToDelete(campaign)
    setDeleteError('')
    setActiveModal('delete')
  }

  const handleConfirmDelete = async () => {
    if (!campaignToDelete) return
    setDeleteLoading(true)
    setDeleteError('')
    try {
      await campaignsApi.delete(campaignToDelete.id)
      setCampaignList((prev) => prev.filter((c) => c.id !== campaignToDelete.id))
      setActiveModal(null)
    } catch (err) {
      setDeleteError((err as Error).message || 'Failed to delete campaign.')
    } finally {
      setDeleteLoading(false)
    }
  }

  const handleJoinSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const clean = inviteCode.trim()
    if (!clean) { setJoinError('Please enter an invite code.'); return }
    setJoinLoading(true)
    setJoinError('')
    try {
      const data = await campaignMembers.lookupInvite(clean)
      setActiveModal(null)
      router.push(`/join/${(data as { campaign_id?: string }).campaign_id ?? ''}?code=${encodeURIComponent(clean.toUpperCase())}`)
    } catch (err) {
      setJoinError((err as Error).message || 'Failed to locate campaign. Check your code and try again.')
    } finally {
      setJoinLoading(false)
    }
  }

  if (!user) {
    return <LandingPage />
  }

  if (loadingList) return <Loading />

  const hasCampaigns = campaignList.length > 0

  const modals = (
    <>
      <Modal open={activeModal === 'create'} onClose={() => setActiveModal(null)} title="New campaign" titleId="new-campaign-title" maxWidth={680}>
        <CampaignForm onCreated={handleCampaignCreated} onCancel={() => setActiveModal(null)} />
      </Modal>
      <Modal open={activeModal === 'join'} onClose={() => setActiveModal(null)} title="Join a campaign" titleId="join-campaign-title">
        <form onSubmit={handleJoinSubmit} style={{ display: 'grid', gap: 16 }}>
          <div className="form-field">
            <label htmlFor="invite-code-input">Invite Code</label>
            <input id="invite-code-input" type="text" className="input" placeholder="e.g., ABCDEFGH" value={inviteCode} onChange={(e) => setInviteCode(e.target.value.toUpperCase())} maxLength={20} required autoFocus disabled={joinLoading} />
            <p style={{ margin: '6px 0 0', fontSize: '0.8rem', color: 'var(--ink-muted)' }}>Ask the campaign owner for the invite code.</p>
          </div>
          {joinError && <ErrorMessage message={joinError} />}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="btn btn-secondary" onClick={() => setActiveModal(null)} disabled={joinLoading}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={joinLoading || !inviteCode.trim()}>{joinLoading ? 'Checking…' : 'Find Campaign'}</button>
          </div>
        </form>
      </Modal>
      <Modal open={activeModal === 'delete'} onClose={() => setActiveModal(null)} title="Delete campaign" titleId="delete-campaign-title" alertDialog>
        <div style={{ display: 'grid', gap: 16 }}>
          <p style={{ margin: 0, lineHeight: 1.6, color: 'var(--ink)' }}>Are you sure you want to delete <strong>{campaignToDelete?.name}</strong>?</p>
          <p style={{ margin: 0, fontSize: '0.9rem', color: 'var(--danger)' }}><i className="bi bi-exclamation-triangle-fill" style={{ marginRight: 8 }} aria-hidden="true" />This is permanent and will delete all sessions and campaign data.</p>
          {deleteError && <ErrorMessage message={deleteError} />}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" className="btn btn-secondary" onClick={() => setActiveModal(null)} disabled={deleteLoading}>Cancel</button>
            <button type="button" className="btn btn-danger" onClick={handleConfirmDelete} disabled={deleteLoading}>{deleteLoading ? 'Deleting…' : 'Delete campaign'}</button>
          </div>
        </div>
      </Modal>
    </>
  )

  if (!hasCampaigns) {
    return (
      <>
        <HeroLock />
        <section className="campaign-hero" style={{ minHeight: 'calc(100svh - 64px)', marginBottom: 0 }}>
          <div className="campaign-hero-copy">
            <span className="section-kicker">WELCOME TO FIRESIDE</span>
            <h1>
              Friends around the fire.
              <br />
              Adventure everywhere else.
            </h1>
            <p>Create a campaign or join a table with an invite code.</p>
            <div className="campaign-hero-actions">
              <button className="btn btn-primary" onClick={() => setActiveModal('create')}>
                <i className="bi bi-plus-lg" aria-hidden="true" /> New campaign
              </button>
              <button className="btn btn-secondary" onClick={() => { setInviteCode(''); setJoinError(''); setActiveModal('join') }}>
                <i className="bi bi-key" aria-hidden="true" /> Join with code
              </button>
            </div>
          </div>
          <div className="campaign-hero-art" aria-hidden="true">
            <span className="campaign-hero-grain" />
          </div>
        </section>
        {modals}
      </>
    )
  }

  return (
    <div className="home-page-v2">

      {/* Campaign list */}
      {hasCampaigns && (
        <>
          <header className="campaigns-header">
            <div>
              <span className="section-kicker">YOUR CAMPAIGNS</span>
              <h2 className="campaigns-title">Return to the table</h2>
            </div>
            <div className="campaigns-header-actions">
              <button className="btn btn-secondary small" onClick={() => { setInviteCode(''); setJoinError(''); setActiveModal('join') }}>
                <i className="bi bi-key" aria-hidden="true" /> Join with code
              </button>
              <button className="btn btn-primary small" onClick={() => setActiveModal('create')}>
                <i className="bi bi-plus-lg" aria-hidden="true" /> New campaign
              </button>
            </div>
          </header>
          <div className="campaigns-grid">
            {campaignList.map((campaign) => (
              <CampaignCard
                key={campaign.id}
                campaign={campaign}
                onDelete={
                  String(user?.id) === String(campaign.owner_id)
                    ? (e: React.MouseEvent) => { e.preventDefault(); openDelete(campaign) }
                    : null
                }
              />
            ))}
          </div>
        </>
      )}

      {modals}
    </div>
  )
}

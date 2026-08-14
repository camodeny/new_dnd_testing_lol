'use client'

import { useState, useEffect } from 'react'
import { useParams, useRouter, useSearchParams } from 'next/navigation'
import Link from 'next/link'
import { apiFetch, campaignMembers } from '@/lib/api'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { Campaign } from '@/types'

function getInitials(name: string): string {
  if (!name) return '?'
  return name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
}

export default function CampaignJoinPage() {
  const { id } = useParams<{ id: string }>()
  const searchParams = useSearchParams()
  const router = useRouter()

  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [loadingCampaign, setLoadingCampaign] = useState(true)
  const [joining, setJoining] = useState(false)
  const [joined, setJoined] = useState(false)
  const [error, setError] = useState('')
  const [code, setCode] = useState(searchParams.get('code') ?? '')

  useEffect(() => {
    const codeParam = searchParams.get('code') ?? ''
    const path = `/campaigns/${id}${codeParam ? `?code=${encodeURIComponent(codeParam)}` : ''}`
    apiFetch<{ campaign: Campaign }>(path)
      .then((data) => setCampaign(data.campaign))
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoadingCampaign(false))
  }, [id, searchParams])

  const handleJoin = async () => {
    if (!code.trim()) { setError('No invite code provided.'); return }
    setJoining(true)
    setError('')
    try {
      await campaignMembers.joinCampaign(Number(id), code)
      setJoined(true)
      setTimeout(() => router.push(`/campaigns/${id}`), 1500)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setJoining(false)
    }
  }

  if (loadingCampaign) {
    return (
      <div className="page campaign-join-page">
        <Loading message="Loading campaign…" />
      </div>
    )
  }

  if (error && !campaign) {
    return (
      <div className="page campaign-join-page">
        <ErrorMessage message={error} />
        <Link href="/" className="btn btn-secondary" style={{ marginTop: 16 }}>Back to campaigns</Link>
      </div>
    )
  }

  if (!campaign) {
    return (
      <div className="page campaign-join-page">
        <ErrorMessage message="Campaign not found." />
      </div>
    )
  }

  const initials = getInitials(campaign.name)

  return (
    <div className="page campaign-join-page">
      {error && (
        <div style={{
          position: 'fixed', top: 18, left: '50%', transform: 'translateX(-50%)',
          zIndex: 1500, width: 'min(520px, calc(100% - 32px))',
          padding: '12px 42px 12px 14px', border: '1px solid rgba(169, 73, 62, 0.3)',
          borderRadius: 8, background: 'var(--danger-soft)', color: '#7e342c',
          boxShadow: 'var(--shadow-md)', fontSize: '0.8rem',
        }} role="alert">
          {error}
          <button
            type="button"
            onClick={() => setError('')}
            aria-label="Dismiss"
            style={{ position: 'absolute', top: 10, right: 10, background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', fontSize: '1rem' }}
          >
            ×
          </button>
        </div>
      )}

      <div style={{ maxWidth: 500, margin: '0 auto' }}>
        <div className="card join-card" style={{ overflow: 'hidden' }}>
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
            gap: 12, padding: 'clamp(28px, 5vw, 42px)',
            borderBottom: '1px solid var(--line)',
          }}>
            <div style={{
              width: 56, height: 56, borderRadius: 10,
              background: 'var(--surface-muted)', color: 'var(--ember-hover)',
              display: 'grid', placeItems: 'center', fontSize: '1.1rem', fontWeight: 700, fontFamily: 'var(--mono)',
              boxShadow: 'var(--shadow-md)',
            }} className="join-avatar" aria-hidden="true">
              {initials}
            </div>
            <h1 className="join-title" style={{ margin: 0, fontSize: 'clamp(2rem, 4vw, 2.8rem)', lineHeight: 1 }}>
              {campaign.name}
            </h1>
            {campaign.description && (
              <p className="join-desc" style={{ margin: 0, fontSize: '0.88rem' }}>{campaign.description}</p>
            )}
          </div>

          <div style={{ padding: 'clamp(22px, 4vw, 36px)' }}>
            {joined ? (
              <div style={{ textAlign: 'center', padding: '24px 0' }}>
                <div style={{ fontSize: '2.5rem', color: 'var(--moss)', marginBottom: 12 }}>
                  <i className="bi bi-check-circle-fill" aria-hidden="true" />
                </div>
                <h2 style={{ margin: '0 0 8px', color: 'var(--ink-strong)', fontSize: '1.6rem' }}>Joined!</h2>
                <p style={{ margin: 0, color: 'var(--ink-muted)' }}>You&apos;ve joined this campaign. Redirecting…</p>
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 16 }}>
                <div>
                  <label className="join-input-label" htmlFor="campaign-invite-code" style={{ display: 'block', marginBottom: 8, fontSize: '0.69rem', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                    Invite Code
                  </label>
                  <input
                    id="campaign-invite-code"
                    type="text"
                    className="join-code-input input"
                    value={code}
                    onChange={(e) => setCode(e.target.value.toUpperCase())}
                    placeholder="Enter invite code…"
                    disabled={joining}
                    autoFocus={!code}
                  />
                </div>
                <button
                  className="join-btn btn btn-primary"
                  onClick={handleJoin}
                  disabled={joining || !code.trim()}
                  style={{ width: '100%', minHeight: 50, justifyContent: 'center' }}
                >
                  {joining ? 'Joining…' : (
                    <><i className="bi bi-box-arrow-in-right" aria-hidden="true" /> Join campaign</>
                  )}
                </button>
              </div>
            )}
          </div>

          <div style={{ padding: '16px clamp(22px, 4vw, 36px)', borderTop: '1px solid var(--line)' }}>
            <Link href="/" className="join-back-btn" style={{ fontSize: '0.8rem', color: 'var(--ink-muted)', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <i className="bi bi-arrow-left" aria-hidden="true" /> Back to campaigns
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}

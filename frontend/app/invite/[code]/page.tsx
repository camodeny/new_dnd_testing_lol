'use client'

import { useState, useEffect, useCallback } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import { campaignMembers } from '@/lib/api'
import Loading from '@/components/common/Loading'
import ErrorMessage from '@/components/common/ErrorMessage'
import type { InviteLookup } from '@/types'

export const PENDING_INVITE_KEY = 'pendingInviteCode'

// Canonical shareable invite URL — issue #242. Preserves invite context
// across authentication/account creation: unauthenticated recipients are
// parked on /login?next=/invite/CODE (code also in localStorage), and after
// sign-up/sign-in they land back here to accept and route into the lobby.
export default function InviteAcceptPage() {
  const { code: rawCode } = useParams<{ code: string }>()
  const router = useRouter()
  const code = (rawCode ?? '').toUpperCase()

  const [lookup, setLookup] = useState<InviteLookup | null>(null)
  const [loading, setLoading] = useState(true)
  const [accepting, setAccepting] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const [error, setError] = useState('')

  const parkAndLogin = useCallback(() => {
    try {
      localStorage.setItem(PENDING_INVITE_KEY, code)
    } catch { /* no-op */ }
    router.replace(`/login?next=${encodeURIComponent(`/invite/${code}`)}`)
  }, [code, router])

  useEffect(() => {
    if (!code) {
      setError('No invite code provided.')
      setLoading(false)
      return
    }
    let token: string | null = null
    try {
      token = localStorage.getItem('token')
    } catch { /* no-op */ }
    if (!token) {
      parkAndLogin()
      return
    }
    campaignMembers
      .lookupInvite(code)
      .then((data) => setLookup(data))
      .catch((err: Error & { status?: number }) => {
        if (err.status === 401) {
          parkAndLogin()
          return
        }
        setError(err.message)
      })
      .finally(() => setLoading(false))
  }, [code, parkAndLogin])

  const handleAccept = async () => {
    setAccepting(true)
    setError('')
    try {
      const result = await campaignMembers.acceptInvite(code)
      try {
        if (localStorage.getItem(PENDING_INVITE_KEY) === code) {
          localStorage.removeItem(PENDING_INVITE_KEY)
        }
      } catch { /* no-op */ }
      setAccepted(true)
      setTimeout(() => router.push(`/campaigns/${result.campaign_id}`), 1200)
    } catch (err) {
      const status = (err as Error & { status?: number }).status
      if (status === 401) {
        parkAndLogin()
        return
      }
      setError((err as Error).message)
    } finally {
      setAccepting(false)
    }
  }

  if (loading) {
    return (
      <div className="page campaign-join-page">
        <Loading message="Checking your invite…" />
      </div>
    )
  }

  if (!lookup) {
    return (
      <div className="page campaign-join-page">
        <div style={{ maxWidth: 500, margin: '0 auto' }}>
          <ErrorMessage message={error || 'Invite not found.'} />
          <p style={{ marginTop: 12, color: 'var(--ink-muted)', fontSize: '0.85rem' }}>
            This invite may have been revoked, expired, or already used up. Ask the
            campaign host for a fresh link.
          </p>
          <Link href="/" className="btn btn-secondary" style={{ marginTop: 16 }}>Back to campaigns</Link>
        </div>
      </div>
    )
  }

  const full = lookup.member_count >= lookup.required_players

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
            <h1 className="join-title" style={{ margin: 0, fontSize: 'clamp(2rem, 4vw, 2.8rem)', lineHeight: 1 }}>
              {lookup.campaign_name}
            </h1>
            <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--ink-muted)' }}>
              You&apos;re invited to join this campaign · {lookup.member_count} / {lookup.required_players} seats filled
              {lookup.expires_at && <> · invite expires {new Date(lookup.expires_at).toLocaleString()}</>}
            </p>
          </div>

          <div style={{ padding: 'clamp(22px, 4vw, 36px)' }}>
            {accepted ? (
              <div style={{ textAlign: 'center', padding: '24px 0' }}>
                <div style={{ fontSize: '2.5rem', color: 'var(--moss)', marginBottom: 12 }}>
                  <i className="bi bi-check-circle-fill" aria-hidden="true" />
                </div>
                <h2 style={{ margin: '0 0 8px', color: 'var(--ink-strong)', fontSize: '1.6rem' }}>Joined!</h2>
                <p style={{ margin: 0, color: 'var(--ink-muted)' }}>Taking you to the lobby…</p>
              </div>
            ) : lookup.campaign_status !== 'lobby' ? (
              <ErrorMessage message="This campaign has already started — invites are locked." />
            ) : full ? (
              <ErrorMessage message="This campaign is full — no seats remain." />
            ) : (
              <div style={{ display: 'grid', gap: 16 }}>
                <div className="lobby-invite-code" style={{ textAlign: 'center' }}>{lookup.code}</div>
                <button
                  className="join-btn btn btn-primary"
                  onClick={handleAccept}
                  disabled={accepting}
                  style={{ width: '100%', minHeight: 50, justifyContent: 'center' }}
                >
                  {accepting ? 'Joining…' : (
                    <><i className="bi bi-box-arrow-in-right" aria-hidden="true" /> Accept invite & join lobby</>
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

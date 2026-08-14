'use client'

import { useEffect } from 'react'
import { usePathname } from 'next/navigation'
import { useAuthContext } from '@/contexts/AuthContext'
import Header from './Header'
import LoginPage from '@/app/login/LoginPage'

const PAGE_TITLES: [RegExp, string][] = [
  [/^\/$/, 'Campaigns'],
  [/^\/characters(?:\/|$)/, 'Characters'],
  [/^\/campaigns\/[^/]+$/, 'Campaign'],
  [/^\/join\/[^/]+$/, 'Join campaign'],
]

export default function AppShell({ children }: { children: React.ReactNode }) {
  const { user, setUser, loading, logout } = useAuthContext()
  const pathname = usePathname()
  const isCampaignView = /^\/campaigns\/[^/]+$/.test(pathname)

  useEffect(() => {
    if (!loading && !user) {
      document.title = 'Sign in · Fireside'
      return
    }
    const match = PAGE_TITLES.find(([pattern]) => pattern.test(pathname))
    document.title = match ? `${match[1]} · Fireside` : 'Fireside'
  }, [loading, pathname, user])

  if (loading) {
    return (
      <div className="app-loading-shell" role="status" aria-live="polite">
        <span className="app-loading-mark" aria-hidden="true">✦</span>
        <span>Opening Fireside…</span>
      </div>
    )
  }

  if (!user) {
    return <LoginPage onLogin={setUser} />
  }

  return (
    <>
      {!isCampaignView && <Header user={user} onLogout={logout} />}
      <main className={`app-main${isCampaignView ? ' full-bleed' : ''}`}>
        {children}
      </main>
    </>
  )
}

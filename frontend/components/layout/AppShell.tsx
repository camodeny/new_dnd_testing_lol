'use client'

import { useEffect } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { useAuthContext } from '@/contexts/AuthContext'
import Header from './Header'
import PublicHeader from './PublicHeader'
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
  const router = useRouter()
  const isCampaignView = /^\/campaigns\/[^/]+$/.test(pathname)
  const isLanding = pathname === '/'
  const isLogin = pathname === '/login'

  useEffect(() => {
    if (!loading && !user) {
      if (isLanding) {
        document.title = 'Fireside — Friends around the fire. Adventure everywhere else.'
        return
      }
      if (isLogin) {
        document.title = 'Sign in · Fireside'
        return
      }
      document.title = 'Sign in · Fireside'
      return
    }
    if (!loading && user && isLogin) {
      document.title = 'Fireside'
      return
    }
    const match = PAGE_TITLES.find(([pattern]) => pattern.test(pathname))
    document.title = match ? `${match[1]} · Fireside` : 'Fireside'
  }, [loading, pathname, user, isLanding, isLogin])

  useEffect(() => {
    if (!loading && user && isLogin) {
      router.replace('/')
    }
  }, [loading, user, isLogin, router])

  if (loading) {
    return (
      <div className="app-loading-shell" role="status" aria-live="polite">
        <span className="app-loading-mark" aria-hidden="true">✦</span>
        <span>Opening Fireside…</span>
      </div>
    )
  }

  if (!user) {
    if (isLogin) {
      return <>{children}</>
    }
    if (isLanding) {
      return <>{children}</>
    }
    return <LoginPage onLogin={setUser} />
  }

  if (isLogin) {
    return null
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

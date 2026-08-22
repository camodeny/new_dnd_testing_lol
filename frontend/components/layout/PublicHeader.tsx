'use client'

import Link from 'next/link'

export default function PublicHeader() {
  return (
    <header className="app-header public-header">
      <div className="app-brand">
        <Link href="/" className="header-link" aria-label="Fireside home">
          <span className="header-mark" aria-hidden="true">✺</span>
          <span className="header-wordmark">Fireside</span>
        </Link>
      </div>

      <nav className="public-header-nav" aria-label="Public navigation">
        <a href="#features" className="public-nav-link">Features</a>
        <a href="#how-it-works" className="public-nav-link">How it works</a>
      </nav>

      <div className="header-actions public-header-actions">
        <Link href="/login" className="public-signin-link">
          Sign in
        </Link>
        <Link href="/login" className="btn btn-primary small">
          Get started
        </Link>
      </div>
    </header>
  )
}

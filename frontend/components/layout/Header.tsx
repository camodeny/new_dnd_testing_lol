'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import type { User } from '@/types'

interface HeaderProps {
  user: User
  onLogout: () => void
}

function getMonogram(name: string): string {
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .map((p) => p[0])
      .join('')
      .slice(0, 2)
      .toUpperCase() || '?'
  )
}

export default function Header({ user, onLogout }: HeaderProps) {
  const pathname = usePathname()
  const displayName = user.username || 'Account'

  function navClass(href: string, exact = false) {
    const active = exact ? pathname === href : pathname.startsWith(href)
    return `nav-link${active ? ' active' : ''}`
  }

  return (
    <header className="app-header">
      <div className="app-brand">
        <Link href="/" className="header-link" aria-label="Fireside campaigns">
          <span className="header-mark" aria-hidden="true">✺</span>
          <span className="header-wordmark">Fireside</span>
        </Link>
      </div>

      <nav className="app-nav" aria-label="Primary navigation">
        <Link href="/" className={navClass('/', true)}>Campaigns</Link>
        <Link href="/characters" className={navClass('/characters')}>Characters</Link>
      </nav>

      <div className="header-actions">
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button
              type="button"
              className="account-menu-trigger user-info"
              aria-label={`Account menu for ${displayName}`}
            >
              <span className="user-monogram" aria-hidden="true">{getMonogram(displayName)}</span>
              <span className="user-identity">
                <span className="user-identity-label">Signed in as</span>
                <span className="user-name">{displayName}</span>
              </span>
              <i className="bi bi-chevron-down account-menu-chevron" aria-hidden="true" />
            </button>
          </DropdownMenu.Trigger>

          <DropdownMenu.Portal>
            <DropdownMenu.Content
              className="account-menu-popover"
              sideOffset={8}
              align="end"
            >
              <DropdownMenu.Item asChild>
                <button type="button" className="logout-button" onClick={onLogout}>
                  <i className="bi bi-box-arrow-right" aria-hidden="true" />
                  <span>Log out</span>
                </button>
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
    </header>
  )
}

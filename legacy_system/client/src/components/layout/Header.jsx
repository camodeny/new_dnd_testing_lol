import { useRef } from 'react'
import { Link, NavLink } from 'react-router-dom'

function navigationClass({ isActive }) {
  return `nav-link ${isActive ? 'active' : ''}`
}

function toolsNavigationClass({ isActive }) {
  return `tools-menu-link ${isActive ? 'active' : ''}`
}

export default function Header({ user, onLogout }) {
  const toolsMenuRef = useRef(null)
  const displayName = user?.username || user?.email || 'Account'
  const monogram = displayName
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || '?'

  const closeToolsMenu = () => {
    if (toolsMenuRef.current) toolsMenuRef.current.open = false
  }

  const handleToolsKeyDown = (event) => {
    if (event.key !== 'Escape' || !toolsMenuRef.current?.open) return
    toolsMenuRef.current.open = false
    toolsMenuRef.current.querySelector('summary')?.focus()
  }

  return (
    <header className="app-header">
      <div className="app-brand">
        <Link to="/" className="header-link" aria-label="Fireside campaigns">
          <span className="header-mark" aria-hidden="true">✺</span>
          <span className="header-wordmark">Fireside</span>
        </Link>
      </div>

      <nav className="app-nav" aria-label="Primary navigation">
        <NavLink to="/" end className={navigationClass}>Campaigns</NavLink>
        <NavLink to="/characters" className={navigationClass}>Characters</NavLink>
        <NavLink to="/automation" className={navigationClass}>Automation</NavLink>
      </nav>

      <div className="header-actions">
        <details
          ref={toolsMenuRef}
          className="tools-menu"
          onKeyDown={handleToolsKeyDown}
        >
          <summary className="tools-menu-trigger" aria-label="Tools">
            <i className="bi bi-tools tools-menu-icon" aria-hidden="true" />
            <span>Tools</span>
            <i className="bi bi-chevron-down tools-menu-chevron" aria-hidden="true" />
          </summary>
          <nav className="tools-menu-popover" aria-label="Developer tools">
            <NavLink to="/design-lab" className={toolsNavigationClass} onClick={closeToolsMenu}>
              <i className="bi bi-palette" aria-hidden="true" />
              <span>Design Lab</span>
            </NavLink>
            <NavLink to="/dev/model" className={toolsNavigationClass} onClick={closeToolsMenu}>
              <i className="bi bi-cpu" aria-hidden="true" />
              <span>Model settings</span>
            </NavLink>
          </nav>
        </details>

        <div className="user-info" aria-label={`Signed in as ${displayName}`}>
          <span className="user-monogram" aria-hidden="true">{monogram}</span>
          <span className="user-identity">
            <span className="user-identity-label">Signed in as</span>
            <span className="user-name">{displayName}</span>
          </span>
          <button onClick={onLogout} className="logout-button" type="button" aria-label="Log out" title="Log out">
            <i className="bi bi-box-arrow-right" aria-hidden="true" />
            <span className="logout-label">Log out</span>
          </button>
        </div>
      </div>
    </header>
  )
}

import { Link, NavLink } from 'react-router-dom'

function navigationClass({ isActive }) {
  return `nav-link ${isActive ? 'active' : ''}`
}

export default function Header({ user, onLogout }) {
  return (
    <header className="app-header">
      <h1><Link to="/" className="header-link"><span className="header-mark">✺</span> Campfire</Link></h1>
      <nav className="app-nav">
        <NavLink to="/" end className={navigationClass}>Campaigns</NavLink>
        <NavLink to="/characters" className={navigationClass}>Characters</NavLink>
        <NavLink to="/automation" className={navigationClass}>Automation</NavLink>
        <NavLink to="/design-lab" className={navigationClass}>Design Lab</NavLink>
        <NavLink to="/dev/model" className={navigationClass}>Model</NavLink>
      </nav>
      <div className="user-info">
        <span className="user-monogram">{user?.username?.slice(0, 2).toUpperCase() || '?'}</span>
        <span className="user-name">{user?.username}</span>
        <button onClick={onLogout} className="logout-button" title="Log out" aria-label="Log out"><i className="bi bi-box-arrow-right" /></button>
      </div>
    </header>
  )
}

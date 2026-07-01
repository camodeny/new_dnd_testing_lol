import { Link } from 'react-router-dom'

export default function Header({ user, onLogout }) {
  return (
    <header className="app-header">
      <h1><Link to="/" className="header-link">D&D Adventure</Link></h1>
      <nav className="app-nav">
        <Link to="/" className="nav-link">Campaigns</Link>
        <Link to="/automation" className="nav-link">Automation</Link>
        <Link to="/characters" className="nav-link">Characters</Link>
        <Link to="/dev/model" className="nav-link">Model</Link>
      </nav>
      <div className="user-info">
        <span>Welcome, {user?.username}!</span>
        <button onClick={onLogout} className="logout-button">Logout</button>
      </div>
    </header>
  )
}

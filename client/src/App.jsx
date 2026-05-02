import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import Login from './Login'
import Header from './components/layout/Header'
import HomePage from './pages/HomePage'
import CampaignViewPage from './pages/CampaignViewPage'
import CharactersListPage from './pages/CharactersListPage'
import CharacterCreatePage from './pages/CharacterCreatePage'
import CharacterEditPage from './pages/CharacterEditPage'
import CharacterViewPage from './pages/CharacterViewPage'
import DevCharacterPage from './pages/DevCharacterPage'
import NotFoundPage from './pages/NotFoundPage'
import './App.css'

function AppRoutes() {
  const { user, setUser, loading, logout } = useAuth()
  const location = useLocation()
  const isCampaignView = location.pathname.startsWith('/campaigns/')

  if (loading) {
    return <div className="loading">Loading...</div>
  }

  if (!user) {
    return <Login onLogin={setUser} />
  }

  return (
    <>
      {!isCampaignView && <Header user={user} onLogout={logout} />}
      <main className={`app-main ${isCampaignView ? 'full-bleed' : ''}`}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/campaigns/:id" element={<CampaignViewPage />} />
          <Route path="/characters" element={<CharactersListPage />} />
          <Route path="/characters/new" element={<CharacterCreatePage />} />
          <Route path="/characters/:id" element={<CharacterViewPage />} />
          <Route path="/characters/:id/edit" element={<CharacterEditPage />} />
          <Route path="/dev/character" element={<DevCharacterPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </>
  )
}

function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}

export default App

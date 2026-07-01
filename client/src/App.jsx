import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import Login from './Login'
import Header from './components/layout/Header'
import HomePage from './pages/HomePage'
import CampaignViewPage from './pages/CampaignViewPage'
import CampaignDevPage from './pages/CampaignDevPage'
import CampaignJoinPage from './pages/CampaignJoinPage'
import CharactersListPage from './pages/CharactersListPage'
import CharacterCreatePage from './pages/CharacterCreatePage'
import CharacterEditPage from './pages/CharacterEditPage'
import CharacterViewPage from './pages/CharacterViewPage'
import DevCharacterPage from './pages/DevCharacterPage'
import DevModelPage from './pages/DevModelPage'
import AutomationHomePage from './pages/AutomationHomePage'
import AutomationScenarioPage from './pages/AutomationScenarioPage'
import AutomationRunPage from './pages/AutomationRunPage'
import AutomationComparePage from './pages/AutomationComparePage'
import NotFoundPage from './pages/NotFoundPage'
import './App.css'

function AppRoutes() {
  const { user, setUser, loading, logout } = useAuth()
  const location = useLocation()
  const isCampaignView = /^\/campaigns\/[^/]+$/.test(location.pathname)

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
          <Route path="/" element={<HomePage user={user} />} />
          <Route path="/campaigns/:id/dev" element={<CampaignDevPage user={user} />} />
          <Route path="/campaigns/:id" element={<CampaignViewPage user={user} />} />
          <Route path="/join/:id" element={<CampaignJoinPage />} />
          <Route path="/characters" element={<CharactersListPage />} />
          <Route path="/characters/new" element={<CharacterCreatePage />} />
          <Route path="/characters/:id" element={<CharacterViewPage />} />
          <Route path="/characters/:id/edit" element={<CharacterEditPage />} />
          <Route path="/dev/character" element={<DevCharacterPage />} />
          <Route path="/dev/model" element={<DevModelPage />} />
          <Route path="/automation" element={<AutomationHomePage />} />
          <Route path="/automation/scenarios/:scenarioId" element={<AutomationScenarioPage />} />
          <Route path="/automation/runs/:runId" element={<AutomationRunPage />} />
          <Route path="/automation/compare" element={<AutomationComparePage />} />
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

import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  createAutomationScenario,
  getAutomationWorkspace,
  getAutomationWorkspaceStreamUrl,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './AutomationWorkspace.css'

function upsertById(items, nextItem) {
  if (!nextItem?.id) return items
  const existingIndex = items.findIndex((item) => item.id === nextItem.id)
  if (existingIndex === -1) return [nextItem, ...items]
  return items.map((item, index) => (index === existingIndex ? nextItem : item))
}

function removeById(items, id) {
  return items.filter((item) => item.id !== id)
}

function applyWorkspaceDelta(previous, payload) {
  if (!previous || !payload?.type) return previous
  if (payload.type === 'scenario_created') {
    return { ...previous, scenarios: upsertById(previous.scenarios || [], payload.scenario) }
  }
  if (payload.type === 'scenario_updated') {
    return { ...previous, scenarios: upsertById(previous.scenarios || [], payload.scenario) }
  }
  if (payload.type === 'scenario_deleted') {
    return { ...previous, scenarios: removeById(previous.scenarios || [], payload.scenario_id) }
  }
  if (payload.type === 'snapshot_created') {
    return previous
  }
  if (payload.type === 'run_created' || payload.type === 'run_updated') {
    const run = payload.run
    const activeRuns = ['queued', 'claimed', 'running', 'stop_requested'].includes(run?.status)
      ? upsertById(previous.active_runs || [], run)
      : removeById(previous.active_runs || [], run?.id)
    const recentFailures = ['failed', 'stopped'].includes(run?.status)
      ? upsertById(previous.recent_failures || [], run).slice(0, 8)
      : removeById(previous.recent_failures || [], run?.id)
    return {
      ...previous,
      active_runs: activeRuns,
      recent_failures: recentFailures,
    }
  }
  return previous
}

export default function AutomationHomePage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const preselectedCampaignId = searchParams.get('sourceCampaignId') || ''
  const autoCreateScenario = searchParams.get('autoCreateScenario') === '1'
  const autoCaptureSnapshot = searchParams.get('captureSnapshot') === '1'
  const [workspace, setWorkspace] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [scenarioName, setScenarioName] = useState('')
  const [sourceCampaignId, setSourceCampaignId] = useState(preselectedCampaignId)
  const [autoCreateAttempted, setAutoCreateAttempted] = useState(false)

  useEffect(() => {
    let alive = true
    let eventSource = null

    const load = async () => {
      try {
        const data = await getAutomationWorkspace()
        if (alive) {
          setWorkspace(data)
          setError('')
        }
      } catch (err) {
        if (alive) setError(err.message)
      } finally {
        if (alive) setLoading(false)
      }
    }

    load()
    eventSource = new EventSource(getAutomationWorkspaceStreamUrl())
    eventSource.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data)
        if (payload.type === 'bootstrap' && alive) {
          setWorkspace(payload.workspace)
          setLoading(false)
          return
        }
        if (alive) {
          setWorkspace((previous) => applyWorkspaceDelta(previous, payload))
          setLoading(false)
        }
      } catch {
        // Ignore malformed deltas; bootstrap fetch already loaded the page.
      }
    }
    eventSource.onerror = () => {}

    return () => {
      alive = false
      if (eventSource) eventSource.close()
    }
  }, [])

  useEffect(() => {
    if (preselectedCampaignId) {
      setSourceCampaignId(preselectedCampaignId)
    }
  }, [preselectedCampaignId])

  useEffect(() => {
    if (!autoCreateScenario || !sourceCampaignId || creating || autoCreateAttempted) return
    const run = async () => {
      setAutoCreateAttempted(true)
      setCreating(true)
      try {
        const result = await createAutomationScenario({
          source_campaign_id: Number(sourceCampaignId),
          name: scenarioName.trim() || undefined,
        })
        navigate(`/automation/scenarios/${result.scenario.id}${autoCaptureSnapshot ? '?captureSnapshot=1' : ''}`)
      } catch (err) {
        setError(err.message)
      } finally {
        setCreating(false)
      }
    }
    run()
  }, [autoCreateAttempted, autoCreateScenario, autoCaptureSnapshot, creating, navigate, scenarioName, sourceCampaignId])

  const handleCreateScenario = async (event) => {
    event.preventDefault()
    if (!sourceCampaignId) {
      setError('Choose a source campaign first.')
      return
    }
    setCreating(true)
    try {
      const result = await createAutomationScenario({
        source_campaign_id: Number(sourceCampaignId),
        name: scenarioName.trim() || undefined,
      })
      navigate(`/automation/scenarios/${result.scenario.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreating(false)
    }
  }

  if (loading) return <Loading message="Loading automation workspace..." />

  const scenarios = workspace?.scenarios || []
  const activeRuns = workspace?.active_runs || []
  const recentFailures = workspace?.recent_failures || []
  const sourceCampaigns = workspace?.source_campaigns || []
  const scenarioTrends = workspace?.scenario_trends || []

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <h1 className="automation-title">Automation Workspace</h1>
          <p className="automation-subtitle">Benchmark scenarios, reproducible snapshots, live runs, and historical comparisons.</p>
        </div>
        <div className="automation-stat-grid">
          <div className="automation-stat-card">
            <strong>{scenarios.length}</strong>
            <span>Scenarios</span>
          </div>
          <div className="automation-stat-card">
            <strong>{activeRuns.length}</strong>
            <span>Active Runs</span>
          </div>
          <div className="automation-stat-card">
            <strong>{recentFailures.length}</strong>
            <span>Recent Failures</span>
          </div>
        </div>
      </div>

      <div className="automation-grid">
        <section className="automation-panel">
          <h2>Create Scenario</h2>
          <form className="automation-form" onSubmit={handleCreateScenario}>
            <label>
              Source campaign
              <select value={sourceCampaignId} onChange={(event) => setSourceCampaignId(event.target.value)}>
                <option value="">Choose a campaign…</option>
                {sourceCampaigns.map((campaign) => (
                  <option key={campaign.id} value={campaign.id}>{campaign.name}</option>
                ))}
              </select>
            </label>
            <label>
              Scenario name
              <input value={scenarioName} onChange={(event) => setScenarioName(event.target.value)} placeholder="Optional override" />
            </label>
            <button className="btn btn-primary" type="submit" disabled={creating}>
              {creating ? 'Creating…' : 'Create Scenario'}
            </button>
          </form>
        </section>

        <section className="automation-panel">
          <h2>Active Runs</h2>
          {activeRuns.length === 0 ? (
            <div className="automation-empty">No queued or running automation jobs.</div>
          ) : (
            <div className="automation-list">
              {activeRuns.map((run) => (
                <Link key={run.id} className="automation-list-item" to={`/automation/runs/${run.id}`}>
                  <div>
                    <strong>Run #{run.id}</strong>
                    <span>{run.status}</span>
                  </div>
                  <span>{run.scorecard_summary?.completed_turns || 0} turns</span>
                </Link>
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Scenarios</h2>
          <span>{scenarios.length} total</span>
        </div>
        {scenarios.length === 0 ? (
          <div className="automation-empty">Create your first automation scenario from a source campaign.</div>
        ) : (
          <div className="automation-list">
            {scenarios.map((scenario) => (
              <Link key={scenario.id} className="automation-list-item" to={`/automation/scenarios/${scenario.id}`}>
                <div>
                  <strong>{scenario.name}</strong>
                  <span>Campaign #{scenario.source_campaign_id}</span>
                </div>
                <span>{(scenario.roster || []).length} seats</span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Recent Failures</h2>
          <span>Newest first</span>
        </div>
        {recentFailures.length === 0 ? (
          <div className="automation-empty">No failed or manually stopped runs yet.</div>
        ) : (
          <div className="automation-list">
            {recentFailures.map((run) => (
              <Link key={run.id} className="automation-list-item" to={`/automation/runs/${run.id}`}>
                <div>
                  <strong>Run #{run.id}</strong>
                  <span>{run.error_text || run.status}</span>
                </div>
                <span>{run.status}</span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Scenario Trends</h2>
          <span>{scenarioTrends.length} tracked</span>
        </div>
        {scenarioTrends.length === 0 ? (
          <div className="automation-empty">No scenario trend data yet.</div>
        ) : (
          <div className="automation-list">
            {scenarioTrends.map((trend) => (
              <div key={trend.scenario_id} className="automation-list-item automation-list-static">
                <div>
                  <strong>{trend.scenario_name}</strong>
                  <span>{Math.round((trend.failure_rate || 0) * 100)}% fail · {trend.median_turns || 0} median turns</span>
                </div>
                <span>{trend.score_movement > 0 ? '+' : ''}{trend.score_movement || 0} score</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <ErrorMessage message={error} />
    </div>
  )
}

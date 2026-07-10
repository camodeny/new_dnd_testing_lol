import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  cleanupAutomationScenario,
  createAutomationRun,
  createAutomationSnapshot,
  getAutomationScenario,
  updateAutomationScenario,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './AutomationWorkspace.css'

export default function AutomationScenarioPage() {
  const { scenarioId } = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [snapshotLabel, setSnapshotLabel] = useState('')
  const [creatingSnapshot, setCreatingSnapshot] = useState(false)
  const [creatingRun, setCreatingRun] = useState(false)
  const [cleaningUp, setCleaningUp] = useState(false)
  const [selectedSnapshotId, setSelectedSnapshotId] = useState('')
  const [matrixModels, setMatrixModels] = useState('')
  const [auditorMode, setAuditorMode] = useState('manual')
  const [auditorModel, setAuditorModel] = useState('')
  const [auditorCount, setAuditorCount] = useState(1)
  const [auditorAutoContinue, setAuditorAutoContinue] = useState(false)
  const [auditorTargetCycles, setAuditorTargetCycles] = useState('')
  const [autoCaptureAttempted, setAutoCaptureAttempted] = useState(false)
  const [selectedRunIds, setSelectedRunIds] = useState([])

  const scenario = data?.scenario
  const snapshots = useMemo(() => data?.snapshots || [], [data?.snapshots])
  const runs = useMemo(() => data?.runs || [], [data?.runs])
  const latestTwoRuns = useMemo(() => runs.slice(0, 2), [runs])
  const baselineRunId = data?.baseline_run?.id || scenario?.baseline_run_id

  const handleToggleRunSelection = (runId) => {
    setSelectedRunIds((prev) =>
      prev.includes(runId)
        ? prev.filter((id) => id !== runId)
        : [...prev, runId]
    )
  }

  const autoCaptureSnapshot = searchParams.get('captureSnapshot') === '1'

  const loadData = useCallback(async () => {
    try {
      const payload = await getAutomationScenario(scenarioId)
      setData(payload)
      setError('')
      const latestSnapshot = payload.snapshots?.[0]
      setSelectedSnapshotId((current) => current || (latestSnapshot ? String(latestSnapshot.id) : ''))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [scenarioId])

  useEffect(() => {
    Promise.resolve().then(loadData)
  }, [loadData])

  useEffect(() => {
    if (!autoCaptureSnapshot || !scenario || creatingSnapshot || snapshots.length > 0 || autoCaptureAttempted) return
    const run = async () => {
      setAutoCaptureAttempted(true)
      setCreatingSnapshot(true)
      try {
        await createAutomationSnapshot(scenarioId, {})
        await loadData()
      } catch (err) {
        setError(err.message)
      } finally {
        setCreatingSnapshot(false)
      }
    }
    run()
  }, [autoCaptureAttempted, autoCaptureSnapshot, creatingSnapshot, loadData, scenario, scenarioId, snapshots.length])

  const handleCreateSnapshot = async (event) => {
    event?.preventDefault?.()
    setCreatingSnapshot(true)
    try {
      await createAutomationSnapshot(scenarioId, { label: snapshotLabel.trim() || undefined })
      setSnapshotLabel('')
      await loadData()
    } catch (err) {
      setError(err.message)
    } finally {
      setCreatingSnapshot(false)
    }
  }

  const buildRunnerConfig = (overrides = {}) => {
    const config = { ...overrides }
    config.auditor_config = {
      mode: auditorMode,
      model: auditorModel.trim() || undefined,
      count: Number(auditorCount) || 1,
      auto_continue: auditorAutoContinue,
      target_cycles: auditorTargetCycles ? Number(auditorTargetCycles) : undefined,
      required_tools: 'runtime_truth_full',
    }
    if (auditorMode === 'built_in' || auditorMode === 'external') {
      config.audit_pause_phases = ['after_dm']
    }
    return config
  }

  const handleQueueRun = async () => {
    if (!selectedSnapshotId) {
      setError('Choose a snapshot first.')
      return
    }
    setCreatingRun(true)
    try {
      const result = await createAutomationRun(scenarioId, {
        snapshot_id: Number(selectedSnapshotId),
        runner_config: buildRunnerConfig(),
      })
      navigate(`/automation/runs/${result.run.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreatingRun(false)
    }
  }

  const handleQueueMatrix = async () => {
    if (!selectedSnapshotId) {
      setError('Choose a snapshot first.')
      return
    }
    const models = matrixModels.split(',').map((item) => item.trim()).filter(Boolean)
    if (models.length < 2) {
      setError('Enter at least two comma-separated models for a matrix run.')
      return
    }
    setCreatingRun(true)
    try {
      const result = await createAutomationRun(scenarioId, {
        snapshot_id: Number(selectedSnapshotId),
        matrix: models.map((model) => ({
          label: model,
          runner_config: buildRunnerConfig({ model }),
        })),
      })
      const newestRun = result.runs?.[0]
      if (newestRun) navigate(`/automation/runs/${newestRun.id}`)
    } catch (err) {
      setError(err.message)
    } finally {
      setCreatingRun(false)
    }
  }

  const handleSetBaseline = async (runId) => {
    try {
      await updateAutomationScenario(scenarioId, { baseline_run_id: runId })
      await loadData()
    } catch (err) {
      setError(err.message)
    }
  }

  const handleCleanup = async () => {
    const confirmed = window.confirm('Remove clone campaigns created by this scenario? Runs and snapshots will remain available.')
    if (!confirmed) return
    setCleaningUp(true)
    try {
      await cleanupAutomationScenario(scenarioId, {})
      await loadData()
    } catch (err) {
      setError(err.message)
    } finally {
      setCleaningUp(false)
    }
  }

  if (loading) return <Loading message="Loading scenario..." />
  if (!scenario) return <ErrorMessage message={error || 'Scenario not found.'} />

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <Link className="automation-back-link" to="/automation">← Back to automation</Link>
          <h1 className="automation-title">{scenario.name}</h1>
          <p className="automation-subtitle">{scenario.description || `Source campaign #${scenario.source_campaign_id}`}</p>
        </div>
        <div className="automation-stat-grid">
          <div className="automation-stat-card">
            <strong>{snapshots.length}</strong>
            <span>Snapshots</span>
          </div>
          <div className="automation-stat-card">
            <strong>{runs.length}</strong>
            <span>Runs</span>
          </div>
          <div className="automation-stat-card">
            <strong>{(scenario.roster || []).length}</strong>
            <span>Seats</span>
          </div>
        </div>
      </div>

      <div className="automation-grid">
        <section className="automation-panel">
          <h2>Snapshot</h2>
          <form className="automation-form" onSubmit={handleCreateSnapshot}>
            <label>
              Snapshot label
              <input value={snapshotLabel} onChange={(event) => setSnapshotLabel(event.target.value)} placeholder="Optional label" />
            </label>
            <button className="btn btn-secondary" type="submit" disabled={creatingSnapshot}>
              {creatingSnapshot ? 'Capturing…' : 'Capture Snapshot'}
            </button>
          </form>
        </section>

        <section className="automation-panel">
          <h2>Queue Run</h2>
          <div className="automation-form">
            <label>
              Snapshot
              <select value={selectedSnapshotId} onChange={(event) => setSelectedSnapshotId(event.target.value)}>
                <option value="">Choose a snapshot…</option>
                {snapshots.map((snapshot) => (
                  <option key={snapshot.id} value={snapshot.id}>
                    {snapshot.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Auditor mode
              <select value={auditorMode} onChange={(event) => setAuditorMode(event.target.value)}>
                <option value="manual">Manual audit gate</option>
                <option value="built_in">Built-in tool-calling auditors</option>
                <option value="external">External CLI/API auditors</option>
              </select>
            </label>
            {auditorMode !== 'manual' && (
              <label>
                Auditor model
                <input
                  value={auditorModel}
                  onChange={(event) => setAuditorModel(event.target.value)}
                  placeholder="optional, e.g. opencode-go/deepseek-v4-flash"
                />
              </label>
            )}
            {auditorMode === 'built_in' && (
              <label>
                Built-in auditor count
                <input
                  type="number"
                  min="1"
                  max="8"
                  value={auditorCount}
                  onChange={(event) => setAuditorCount(event.target.value)}
                />
              </label>
            )}
            <label>
              Target audited cycles
              <input
                type="number"
                min="1"
                max="100"
                value={auditorTargetCycles}
                onChange={(event) => setAuditorTargetCycles(event.target.value)}
                placeholder="optional"
              />
            </label>
            {auditorMode === 'built_in' && (
              <label className="automation-checkbox-label">
                <input
                  type="checkbox"
                  checked={auditorAutoContinue}
                  onChange={(event) => setAuditorAutoContinue(event.target.checked)}
                />
                Auto-continue after built-in audits
              </label>
            )}
            <button className="btn btn-primary" type="button" onClick={handleQueueRun} disabled={creatingRun}>
              {creatingRun ? 'Queueing…' : 'Queue Run'}
            </button>
            <div className="automation-form-divider"><span>Or queue a model matrix</span></div>
            <label>
              Model matrix
              <input
                value={matrixModels}
                onChange={(event) => setMatrixModels(event.target.value)}
                placeholder="model-a, model-b, model-c"
              />
            </label>
            <button className="btn btn-secondary" type="button" onClick={handleQueueMatrix} disabled={creatingRun}>
              {creatingRun ? 'Queueing…' : 'Queue Matrix'}
            </button>
          </div>
        </section>
      </div>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Roster</h2>
          <span>Prompt seats</span>
        </div>
        <div className="automation-list">
          {(scenario.roster || []).map((entry) => (
            <div key={`${entry.user_id}-${entry.character_id}`} className="automation-list-item automation-list-static">
              <div>
                <strong>{entry.label}</strong>
                <span>{entry.character_name}</span>
              </div>
              <span>{entry.member_role}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Snapshots</h2>
          <button className="btn btn-secondary btn-small" onClick={handleCleanup} disabled={cleaningUp}>
            {cleaningUp ? 'Removing…' : 'Remove clone campaigns'}
          </button>
        </div>
        {snapshots.length === 0 ? (
          <div className="automation-empty">No snapshots yet.</div>
        ) : (
          <div className="automation-list">
            {snapshots.map((snapshot) => (
              <div key={snapshot.id} className="automation-list-item automation-list-static">
                <div>
                  <strong>{snapshot.label}</strong>
                  <span>{snapshot.summary || snapshot.metadata?.campaign_name}</span>
                </div>
                <span>{snapshot.created_at?.slice(0, 19).replace('T', ' ')}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Runs</h2>
          <div className="automation-inline-actions">
            {selectedRunIds.length === 2 ? (
              <Link
                className="btn btn-primary btn-small"
                to={`/automation/compare?left=${selectedRunIds[0]}&right=${selectedRunIds[1]}`}
              >
                Compare Selected ({selectedRunIds.length})
              </Link>
            ) : (
              <button
                className="btn btn-secondary btn-small"
                disabled
                title="Select exactly 2 runs to compare"
              >
                Compare Selected (select 2)
              </button>
            )}
            {latestTwoRuns.length === 2 && (
              <Link
                className="btn btn-secondary btn-small"
                to={`/automation/compare?left=${latestTwoRuns[1].id}&right=${latestTwoRuns[0].id}`}
              >
                Compare Latest Two
              </Link>
            )}
          </div>
        </div>
        {runs.length === 0 ? (
          <div className="automation-empty">No runs yet.</div>
        ) : (
          <div className="automation-list">
            {runs.map((run) => (
              <div key={run.id} className="automation-run-list-row">
                <input
                  type="checkbox"
                  checked={selectedRunIds.includes(run.id)}
                  onChange={() => handleToggleRunSelection(run.id)}
                  className="automation-run-checkbox"
                  title="Select run for comparison"
                />
                <Link className="automation-list-item" style={{ flex: 1 }} to={`/automation/runs/${run.id}`}>
                  <div>
                    <strong>
                      Run #{run.id}
                      {run.matrix_label ? ` (${run.matrix_label})` : ''}
                    </strong>
                    <span>{run.status}</span>
                  </div>
                  <span>{run.id === baselineRunId ? 'Baseline' : `${run.scorecard_summary?.completed_turns || 0} turns`}</span>
                </Link>
              </div>
            ))}
          </div>
        )}
        {runs.length > 0 && (
          <div className="automation-list">
            {runs.map((run) => (
              <button
                key={`baseline-${run.id}`}
                className="btn btn-secondary btn-small"
                type="button"
                onClick={() => handleSetBaseline(run.id)}
                disabled={run.id === baselineRunId}
              >
                {run.id === baselineRunId ? `Baseline Run #${run.id}` : `Set Run #${run.id} Baseline`}
              </button>
            ))}
          </div>
        )}
      </section>

      <ErrorMessage message={error} />
    </div>
  )
}

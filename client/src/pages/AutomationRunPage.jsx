import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  continueAutomationRun,
  getAutomationRun,
  getAutomationRunStreamUrl,
  stopAutomationRun,
  submitAutomationRunAudit,
} from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './AutomationWorkspace.css'

function formatTime(iso) {
  if (!iso) return 'Unknown'
  return new Date(iso).toLocaleString()
}

function upsertById(items, nextItem) {
  if (!nextItem?.id) return items
  const existingIndex = items.findIndex((item) => item.id === nextItem.id)
  if (existingIndex === -1) return [...items, nextItem]
  return items.map((item, index) => (index === existingIndex ? nextItem : item))
}

function applyRunEvent(previous, payload) {
  if (!previous?.run || !payload?.event) return previous

  const next = {
    ...previous,
    run: payload.run || previous.run,
    incidents: payload.incidents || previous.incidents || [],
    baseline_comparison: payload.baseline_comparison || previous.baseline_comparison || {},
    events: upsertById(previous.events || [], payload.event),
  }

  if (payload.delta?.message) {
    const latestSession = previous.latest_session || {}
    next.latest_session = {
      ...latestSession,
      messages: upsertById(latestSession.messages || [], payload.delta.message),
    }
  }

  if (payload.delta?.pending_sheet_proposals) {
    const latestSession = next.latest_session || previous.latest_session || {}
    next.latest_session = {
      ...latestSession,
      pending_sheet_proposals: payload.delta.pending_sheet_proposals,
    }
  }

  if (payload.event.event_type === 'run_scorecard_updated') {
    next.scorecard = payload.delta?.scorecard || payload.event.payload?.scorecard || previous.scorecard || []
  }

  if (payload.delta?.audit_cycles) {
    next.audit_cycles = payload.delta.audit_cycles
    next.current_audit_cycle = payload.delta.current_audit_cycle || null
  }

  if (payload.scorecard_template) next.scorecard_template = payload.scorecard_template

  return next
}

export default function AutomationRunPage() {
  const { runId } = useParams()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [continuing, setContinuing] = useState(false)
  const [savingAudit, setSavingAudit] = useState(false)
  const [auditSummary, setAuditSummary] = useState('')
  const [auditNotes, setAuditNotes] = useState('')
  const [overallStatus, setOverallStatus] = useState('pass')
  const [overallSummary, setOverallSummary] = useState('')
  const [criteriaDrafts, setCriteriaDrafts] = useState([])

  const loadData = async () => {
    try {
      const payload = await getAutomationRun(runId)
      setData(payload)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let alive = true
    let eventSource = null
    loadData()
    eventSource = new EventSource(getAutomationRunStreamUrl(runId))
    eventSource.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data)
        if (payload.type === 'bootstrap' && alive) {
          setData(payload.run)
          setLoading(false)
          return
        }
        if (payload.type === 'run_event' && alive) {
          setData((previous) => applyRunEvent(previous, payload))
          setLoading(false)
        }
      } catch {
        // Ignore malformed events; bootstrap fetch already loaded the page.
      }
    }
    eventSource.onerror = () => {}

    return () => {
      alive = false
      if (eventSource) eventSource.close()
    }
  }, [runId])

  useEffect(() => {
    const cycle = data?.current_audit_cycle
    const template = data?.scorecard_template || {}
    if (!cycle) {
      setAuditSummary('')
      setAuditNotes('')
      setOverallStatus('pass')
      setOverallSummary('')
      setCriteriaDrafts([])
      return
    }
    setAuditSummary(cycle.summary || '')
    setAuditNotes(cycle.notes || '')
    setOverallStatus(cycle.scorecard_summary?.overall_status || 'pass')
    setOverallSummary(cycle.scorecard_summary?.overall_summary || '')
    const existing = new Map((cycle.scorecard?.criteria || []).map((item) => [item.criterion_id, item]))
    setCriteriaDrafts((template.criteria || []).map((criterion) => {
      const previous = existing.get(criterion.id) || {}
      return {
        criterion_id: criterion.id,
        label: criterion.label || criterion.id,
        description: criterion.description || '',
        status: previous.status || 'pass',
        summary: previous.summary || '',
        evidence: previous.evidence || '',
      }
    }))
  }, [data?.current_audit_cycle, data?.scorecard_template])

  const handleStop = async () => {
    setStopping(true)
    try {
      await stopAutomationRun(runId)
      await loadData()
    } catch (err) {
      setError(err.message)
    } finally {
      setStopping(false)
    }
  }

  const handleCriterionChange = (criterionId, field, value) => {
    setCriteriaDrafts((previous) => previous.map((item) => (
      item.criterion_id === criterionId ? { ...item, [field]: value } : item
    )))
  }

  const handleSaveAudit = async () => {
    const cycle = data?.current_audit_cycle
    if (!cycle) return
    setSavingAudit(true)
    try {
      const payload = await submitAutomationRunAudit(runId, cycle.id, {
        summary: auditSummary.trim() || undefined,
        notes: auditNotes.trim() || undefined,
        scorecard: {
          overall_status: overallStatus,
          overall_summary: overallSummary.trim() || undefined,
          criteria: criteriaDrafts,
        },
      })
      setData((previous) => previous ? {
        ...previous,
        run: payload.run || previous.run,
        current_audit_cycle: payload.audit_cycle || previous.current_audit_cycle,
        audit_cycles: (previous.audit_cycles || []).map((item) => (item.id === payload.audit_cycle?.id ? payload.audit_cycle : item)),
        scorecard: payload.scorecard || previous.scorecard,
      } : previous)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingAudit(false)
    }
  }

  const handleContinue = async () => {
    setContinuing(true)
    try {
      const payload = await continueAutomationRun(runId)
      setData((previous) => previous ? {
        ...previous,
        run: payload.run || previous.run,
        current_audit_cycle: null,
      } : previous)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setContinuing(false)
    }
  }

  if (loading) return <Loading message="Loading run..." />
  if (!data?.run) return <ErrorMessage message={error || 'Run not found.'} />

  const run = data.run
  const scenario = data.scenario
  const latestSession = data.latest_session
  const events = data.events || []
  const scorecard = data.scorecard || []
  const messages = latestSession?.messages || []
  const incidents = data.incidents || run.scorecard_summary?.incidents || []
  const encounterMap = data.encounter_map
  const auditCycles = data.audit_cycles || []
  const currentAuditCycle = data.current_audit_cycle
  const scorecardTemplate = data.scorecard_template || {}
  const compareLink = scenario ? `/automation/scenarios/${scenario.id}` : '/automation'
  const canStop = ['queued', 'claimed', 'running', 'awaiting_audit'].includes(run.status)

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <Link className="automation-back-link" to={compareLink}>← Back to scenario</Link>
          <h1 className="automation-title">Run #{run.id}</h1>
          <p className="automation-subtitle">{scenario?.name || 'Automation run'} · {run.status}</p>
        </div>
        <div className="automation-stat-grid">
          <div className="automation-stat-card">
            <strong>{run.scorecard_summary?.completed_turns || 0}</strong>
            <span>Turns</span>
          </div>
          <div className="automation-stat-card">
            <strong>{run.scorecard_summary?.error_count || 0}</strong>
            <span>Errors</span>
          </div>
          <div className="automation-stat-card">
            <strong>{scorecard.length}</strong>
            <span>Checks</span>
          </div>
          <div className="automation-stat-card">
            <strong>{incidents.length}</strong>
            <span>Incidents</span>
          </div>
        </div>
      </div>

      <div className="automation-grid">
        <section className="automation-panel">
          <div className="automation-section-header">
            <h2>Status</h2>
            {canStop && (
              <button className="btn btn-secondary btn-small" onClick={handleStop} disabled={stopping}>
                {stopping ? 'Stopping…' : 'Stop Run'}
              </button>
            )}
          </div>
          <div className="automation-meta-grid">
            <div><strong>Status</strong><span>{run.status}</span></div>
            <div><strong>Created</strong><span>{formatTime(run.created_at)}</span></div>
            <div><strong>Started</strong><span>{formatTime(run.started_at)}</span></div>
            <div><strong>Finished</strong><span>{formatTime(run.finished_at)}</span></div>
            <div><strong>Derived campaign</strong><span>{run.derived_campaign_id || 'Not created'}</span></div>
            <div><strong>Snapshot</strong><span>{data.snapshot?.label || run.snapshot_id}</span></div>
            <div><strong>Encounter map</strong><span>{encounterMap?.title || 'None'}</span></div>
          </div>
          {run.error_text && <ErrorMessage message={run.error_text} />}
        </section>

        <section className="automation-panel">
          <h2>Scorecard</h2>
          {scorecard.length === 0 ? (
            <div className="automation-empty">No scorecard results yet.</div>
          ) : (
            <div className="automation-scorecard">
              {scorecard.map((result) => (
                <div key={result.check_id} className={`automation-scorecard-row status-${result.status}`}>
                  <div>
                    <strong>{result.check_id}</strong>
                    <span>{result.summary}</span>
                  </div>
                  <span>{result.status}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="automation-panel">
        <div className="automation-section-header">
          <h2>Audit Gate</h2>
          <span>{run.status === 'awaiting_audit' ? 'Paused for audit' : 'Live or finished'}</span>
        </div>
        {currentAuditCycle ? (
          <div className="automation-form">
            <div className="automation-meta-grid">
              <div><strong>Cycle</strong><span>#{currentAuditCycle.cycle_number}</span></div>
              <div><strong>Phase</strong><span>{currentAuditCycle.phase}</span></div>
              <div><strong>Scorecard</strong><span>{scorecardTemplate.name || 'Freeform audit'}</span></div>
            </div>
            <label>
              Audit summary
              <input value={auditSummary} onChange={(event) => setAuditSummary(event.target.value)} placeholder="Short verdict for this pause point" />
            </label>
            <label>
              Audit notes
              <textarea className="automation-textarea" value={auditNotes} onChange={(event) => setAuditNotes(event.target.value)} placeholder="Runtime-truth notes, evidence, and repair concerns" />
            </label>
            <label>
              Overall status
              <select value={overallStatus} onChange={(event) => setOverallStatus(event.target.value)}>
                <option value="pass">pass</option>
                <option value="warn">warn</option>
                <option value="fail">fail</option>
              </select>
            </label>
            <label>
              Overall summary
              <input value={overallSummary} onChange={(event) => setOverallSummary(event.target.value)} placeholder="Optional overall summary" />
            </label>
            {criteriaDrafts.length > 0 && (
              <div className="automation-events">
                {criteriaDrafts.map((criterion) => (
                  <div key={criterion.criterion_id} className="automation-event">
                    <div className="automation-message-meta">
                      <strong>{criterion.label}</strong>
                      <span>{criterion.description}</span>
                    </div>
                    <div className="automation-form">
                      <label>
                        Status
                        <select value={criterion.status} onChange={(event) => handleCriterionChange(criterion.criterion_id, 'status', event.target.value)}>
                          <option value="pass">pass</option>
                          <option value="warn">warn</option>
                          <option value="fail">fail</option>
                        </select>
                      </label>
                      <label>
                        Summary
                        <input value={criterion.summary} onChange={(event) => handleCriterionChange(criterion.criterion_id, 'summary', event.target.value)} />
                      </label>
                      <label>
                        Evidence
                        <input value={criterion.evidence} onChange={(event) => handleCriterionChange(criterion.criterion_id, 'evidence', event.target.value)} />
                      </label>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="automation-inline-actions">
              <button className="btn btn-secondary" type="button" onClick={handleSaveAudit} disabled={savingAudit}>
                {savingAudit ? 'Saving…' : 'Save Audit'}
              </button>
              <button className="btn btn-primary" type="button" onClick={handleContinue} disabled={continuing || currentAuditCycle.status !== 'audited'}>
                {continuing ? 'Continuing…' : 'Continue Run'}
              </button>
            </div>
          </div>
        ) : (
          <div className="automation-empty">This run is not currently paused for manual audit.</div>
        )}
      </section>

      <section className="automation-panel">
        <h2>Incidents</h2>
        {incidents.length === 0 ? (
          <div className="automation-empty">No halt or retry incidents detected.</div>
        ) : (
          <div className="automation-scorecard">
            {incidents.map((incident) => (
              <div
                key={`${incident.incident_type}-${incident.count}`}
                className={`automation-scorecard-row status-${incident.severity === 'fail' ? 'fail' : 'warn'}`}
              >
                <div>
                  <strong>{incident.incident_type}</strong>
                  <span>{incident.summary}</span>
                </div>
                <span>{incident.severity}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <h2>Audit Cycles</h2>
        {auditCycles.length === 0 ? (
          <div className="automation-empty">No audit pauses recorded yet.</div>
        ) : (
          <div className="automation-events">
            {auditCycles.map((cycle) => (
              <details key={cycle.id} className="automation-event">
                <summary>
                  <strong>Cycle #{cycle.cycle_number}</strong>
                  <span>{cycle.phase} · {cycle.status}</span>
                </summary>
                <pre>{JSON.stringify(cycle, null, 2)}</pre>
              </details>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <h2>Live Transcript</h2>
        {messages.length === 0 ? (
          <div className="automation-empty">No session messages yet.</div>
        ) : (
          <div className="automation-transcript">
            {messages.map((message) => (
              <div key={message.id} className={`automation-message automation-message-${message.role}`}>
                <div className="automation-message-meta">
                  <strong>{message.username || message.role}</strong>
                  <span>{formatTime(message.created_at)}</span>
                </div>
                <div>{message.content}</div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="automation-panel">
        <h2>Run Events</h2>
        {events.length === 0 ? (
          <div className="automation-empty">No structured run events yet.</div>
        ) : (
          <div className="automation-events">
            {events.map((event) => (
              <details key={event.id} className="automation-event">
                <summary>
                  <strong>{event.event_type}</strong>
                  <span>{formatTime(event.created_at)}</span>
                </summary>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </details>
            ))}
          </div>
        )}
      </section>

      <ErrorMessage message={error} />
    </div>
  )
}

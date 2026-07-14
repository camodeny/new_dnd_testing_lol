import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  continueAutomationRun,
  getAutomationRun,
  getAutomationRunProviderCalls,
  getAutomationRunStreamUrl,
  startAutomationRunAuditors,
  stopAutomationRun,
  stopAutomationRunAuditors,
  submitAutomationRunAudit,
  deleteAutomationRun,
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

  if (payload.delta?.auditor_jobs) {
    next.auditor_jobs = payload.delta.auditor_jobs
  }

  if (payload.scorecard_template) next.scorecard_template = payload.scorecard_template

  return next
}

export default function AutomationRunPage() {
  const { runId } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [providerCalls, setProviderCalls] = useState([])
  const [loadingCalls, setLoadingCalls] = useState(false)
  const [activeTab, setActiveTab] = useState('scorecard')
  const [expandedCallId, setExpandedCallId] = useState(null)

  const loadProviderCalls = async () => {
    if (!runId) return
    try {
      setLoadingCalls(true)
      const res = await getAutomationRunProviderCalls(runId, true)
      setProviderCalls(res.provider_calls || [])
    } catch (err) {
      console.error('Failed to load provider calls:', err)
    } finally {
      setLoadingCalls(false)
    }
  }

  // Load provider calls when runId changes or tab switches to llm-calls
  useEffect(() => {
    if (runId) {
      loadProviderCalls()
    }
  }, [runId])

  useEffect(() => {
    if (activeTab === 'llm-calls' && runId) {
      loadProviderCalls()
    }
  }, [activeTab, runId])
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [continuing, setContinuing] = useState(false)
  const [savingAudit, setSavingAudit] = useState(false)
  const [startingAuditors, setStartingAuditors] = useState(false)
  const [stoppingAuditors, setStoppingAuditors] = useState(false)
  const [auditSummary, setAuditSummary] = useState('')
  const [auditNotes, setAuditNotes] = useState('')
  const [overallStatus, setOverallStatus] = useState('not_assessed')
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
      setOverallStatus('not_assessed')
      setOverallSummary('')
      setCriteriaDrafts([])
      return
    }
    setAuditSummary(cycle.summary || '')
    setAuditNotes(cycle.notes || '')
    setOverallStatus(cycle.scorecard_summary?.overall_status || 'not_assessed')
    setOverallSummary(cycle.scorecard_summary?.overall_summary || '')
    const existing = new Map((cycle.scorecard?.criteria || []).map((item) => [item.criterion_id, item]))
    setCriteriaDrafts((template.criteria || []).map((criterion) => {
      const previous = existing.get(criterion.id) || {}
      return {
        criterion_id: criterion.id,
        label: criterion.label || criterion.id,
        description: criterion.description || '',
        status: previous.status || 'not_assessed',
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

  const handleDeleteRun = async () => {
    const confirmed = window.confirm('Are you sure you want to permanently delete this run? This action cannot be undone.')
    if (!confirmed) return
    setDeleting(true)
    try {
      await deleteAutomationRun(runId)
      const target = data?.run?.scenario_id ? `/automation/scenarios/${data.run.scenario_id}` : '/automation'
      navigate(target)
    } catch (err) {
      setError(err.message)
      setDeleting(false)
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

  const handleStartAuditors = async () => {
    setStartingAuditors(true)
    try {
      const payload = await startAutomationRunAuditors(runId)
      setData((previous) => previous ? {
        ...previous,
        run: payload.run || previous.run,
        auditor_jobs: payload.auditor_jobs || previous.auditor_jobs || [],
        current_audit_cycle: payload.audit_cycle || previous.current_audit_cycle,
        scorecard: payload.scorecard || previous.scorecard,
      } : previous)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setStartingAuditors(false)
    }
  }

  const handleStopAuditors = async () => {
    setStoppingAuditors(true)
    try {
      const payload = await stopAutomationRunAuditors(runId)
      setData((previous) => previous ? {
        ...previous,
        run: payload.run || previous.run,
        auditor_jobs: payload.auditor_jobs || previous.auditor_jobs || [],
      } : previous)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setStoppingAuditors(false)
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
  const auditorJobs = data.auditor_jobs || []
  const currentAuditCycle = data.current_audit_cycle
  const auditorConfig = run.runner_config?.auditor_config || {}
  const currentAuditorJobs = currentAuditCycle
    ? auditorJobs.filter((job) => job.cycle_id === currentAuditCycle.id)
    : []
  const canStartBuiltInAuditors = run.status === 'awaiting_audit' && currentAuditCycle && auditorConfig.mode === 'built_in'
  const scorecardTemplate = data.scorecard_template || {}
  const compareLink = scenario ? `/automation/scenarios/${scenario.id}` : '/automation'
  const canStop = ['queued', 'claimed', 'running', 'awaiting_audit'].includes(run.status)

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px', marginBottom: '8px' }}>
            <Link className="automation-back-link" style={{ marginBottom: 0 }} to={compareLink}>← Back to scenario</Link>
            <button
              className="btn btn-danger btn-small"
              onClick={handleDeleteRun}
              disabled={deleting}
            >
              {deleting ? 'Deleting…' : 'Delete Run'}
            </button>
          </div>
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

      <div className="automation-run-workspace-grid">
        {/* Left Column: Live RPG Chat Transcript */}
        <div className="automation-chat-column">
          <h2>Live Session Transcript</h2>
          {messages.length === 0 ? (
            <div className="automation-empty">No session messages yet.</div>
          ) : (
            <div className="automation-chat-viewport">
              {messages.map((message) => (
                <div key={message.id} className={`chat-msg chat-msg-${message.role}`}>
                  <div className="chat-msg-meta">
                    <span className="chat-msg-sender">
                      {message.role === 'dm' ? '🤖 AI DM' : `🧙 ${message.username || message.role}`}
                    </span>
                    <span className="chat-msg-time">{formatTime(message.created_at)}</span>
                  </div>
                  <div className="chat-msg-body">{message.content}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right Column: Diagnostics Workspace Tab panel */}
        <div className="diagnostics-tabs">
          <div className="tabs-header">
            <button
              className={`tab-btn ${activeTab === 'scorecard' ? 'active' : ''}`}
              onClick={() => setActiveTab('scorecard')}
            >
              Scorecard & Audits
            </button>
            <button
              className={`tab-btn ${activeTab === 'llm-calls' ? 'active' : ''}`}
              onClick={() => setActiveTab('llm-calls')}
            >
              LLM Calls ({providerCalls.length})
            </button>
            <button
              className={`tab-btn ${activeTab === 'events' ? 'active' : ''}`}
              onClick={() => setActiveTab('events')}
            >
              System Events ({events.length})
            </button>
          </div>

          <div className="tab-body">
            {activeTab === 'scorecard' && (
              <>
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

                <section className="automation-panel">
                  <div className="automation-section-header">
                    <h2>Audit Gate</h2>
                    <span>{run.status === 'awaiting_audit' ? 'Paused for audit' : 'Live or finished'}</span>
                  </div>
                  <div className="automation-meta-grid">
                    <div><strong>Auditor mode</strong><span>{auditorConfig.mode || 'manual'}</span></div>
                    <div><strong>Auditor model</strong><span>{auditorConfig.model || 'Default app model'}</span></div>
                    <div><strong>Auditor count</strong><span>{auditorConfig.count || 1}</span></div>
                    <div><strong>Auto-continue</strong><span>{auditorConfig.auto_continue ? 'Yes' : 'No'}</span></div>
                  </div>
                  {canStartBuiltInAuditors && (
                    <div className="automation-inline-actions" style={{ marginTop: '14px' }}>
                      <button className="btn btn-primary" type="button" onClick={handleStartAuditors} disabled={startingAuditors}>
                        {startingAuditors ? 'Starting Auditors…' : 'Run Built-In Auditors'}
                      </button>
                      {currentAuditorJobs.some((job) => ['queued', 'running'].includes(job.status)) && (
                        <button className="btn btn-secondary" type="button" onClick={handleStopAuditors} disabled={stoppingAuditors}>
                          {stoppingAuditors ? 'Stopping…' : 'Stop Auditors'}
                        </button>
                      )}
                    </div>
                  )}
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
                          <option value="not_assessed">not_assessed</option>
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
                              <div className="automation-form" style={{ padding: '14px' }}>
                                <label>
                                  Status
                                  <select value={criterion.status} onChange={(event) => handleCriterionChange(criterion.criterion_id, 'status', event.target.value)}>
                                    <option value="pass">pass</option>
                                    <option value="warn">warn</option>
                                    <option value="fail">fail</option>
                                    <option value="not_assessed">not_assessed</option>
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
                  <h2>Built-In Auditor Jobs</h2>
                  {auditorJobs.length === 0 ? (
                    <div className="automation-empty">No built-in auditor jobs recorded for this run.</div>
                  ) : (
                    <div className="automation-events">
                      {auditorJobs.map((job) => (
                        <details key={job.id} className="automation-event">
                          <summary>
                            <strong>Cycle #{auditCycles.find((cycle) => cycle.id === job.cycle_id)?.cycle_number || job.cycle_id} · Auditor {job.auditor_slot}</strong>
                            <span>{job.status} · {job.model || 'default model'} · {job.tool_call_count || 0} tools</span>
                          </summary>
                          {job.error_text && <ErrorMessage message={job.error_text} />}
                          <div className="automation-meta-grid">
                            <div><strong>Provider</strong><span>{job.provider || 'Unknown'}</span></div>
                            <div><strong>Provider call</strong><span>{job.provider_call_id || 'None'}</span></div>
                            <div><strong>Started</strong><span>{formatTime(job.started_at)}</span></div>
                            <div><strong>Finished</strong><span>{formatTime(job.finished_at)}</span></div>
                          </div>
                          {job.submitted_scorecard?.overall_summary && (
                            <p className="automation-subtitle">{job.submitted_scorecard.overall_summary}</p>
                          )}
                          {job.tool_trace?.length > 0 && (
                            <div className="automation-tool-trace">
                              <strong>Tool calls</strong>
                              <ol>
                                {job.tool_trace.map((toolCall, index) => (
                                  <li key={`${job.id}-tool-${index}`}>
                                    <code>{toolCall.tool_name || 'unknown_tool'}</code>
                                    <pre className="llm-json-box">{JSON.stringify({
                                      arguments: toolCall.arguments || {},
                                      result: toolCall.result || {},
                                    }, null, 2)}</pre>
                                  </li>
                                ))}
                              </ol>
                            </div>
                          )}
                          {job.submitted_scorecard?.unresolved_evidence_gaps?.length > 0 && (
                            <div className="automation-scorecard-row status-warn">
                              <div>
                                <strong>Evidence gaps</strong>
                                <span>{job.submitted_scorecard.unresolved_evidence_gaps.join(' | ')}</span>
                              </div>
                              <span>warn</span>
                            </div>
                          )}
                          <pre className="llm-json-box">{JSON.stringify(job.submitted_scorecard || {}, null, 2)}</pre>
                        </details>
                      ))}
                    </div>
                  )}
                </section>

                <section className="automation-panel">
                  <h2>Incidents</h2>
                  {incidents.length === 0 ? (
                    <div className="automation-empty">No halt or retry incidents detected.</div>
                  ) : (
                    <div className="automation-scorecard">
                      {incidents.map((incident, idx) => (
                        <div
                          key={idx}
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
                  <h2>Audit Cycles History</h2>
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
                          <pre className="llm-json-box">{JSON.stringify(cycle, null, 2)}</pre>
                        </details>
                      ))}
                    </div>
                  )}
                </section>
              </>
            )}

            {activeTab === 'llm-calls' && (
              <section className="automation-panel">
                <div className="automation-section-header">
                  <h2>LLM Provider Calls</h2>
                  {loadingCalls ? (
                    <span>Refreshing…</span>
                  ) : (
                    <button className="btn btn-secondary btn-small" onClick={loadProviderCalls}>
                      Refresh Calls
                    </button>
                  )}
                </div>

                {providerCalls.length === 0 ? (
                  <div className="automation-empty">No LLM provider calls logged for this run yet.</div>
                ) : (
                  <div className="llm-call-list">
                    {providerCalls.map((call) => {
                      const isExpanded = expandedCallId === call.id
                      return (
                        <div key={call.id} className="llm-call-card">
                          <div
                            className="llm-call-header"
                            onClick={() => setExpandedCallId(isExpanded ? null : call.id)}
                          >
                            <div className="llm-call-summary">
                              <span className="llm-call-phase">
                                {call.phase?.replace(/_/g, ' ')}
                              </span>
                              <span className="llm-call-model">{call.model}</span>
                            </div>
                            <div className="llm-call-metrics">
                              <div className="llm-metric">
                                <span>Latency</span>
                                <strong>{call.latency_ms ? `${(call.latency_ms / 1000).toFixed(2)}s` : '—'}</strong>
                              </div>
                              <div className="llm-metric">
                                <span>Tokens</span>
                                <strong>{call.usage_total_tokens || '—'}</strong>
                              </div>
                              <button className="btn btn-secondary btn-small" style={{ pointerEvents: 'none' }}>
                                {isExpanded ? 'Collapse' : 'Inspect'}
                              </button>
                            </div>
                          </div>

                          {isExpanded && (
                            <div className="llm-call-details">
                              {call.request?.system && (
                                <div className="llm-details-section">
                                  <h4>System Prompt (DM Behavior Instructions)</h4>
                                  <div className="llm-prompt-box">
                                    {call.request.system}
                                  </div>
                                </div>
                              )}

                              {call.request?.messages && (
                                <div className="llm-details-section">
                                  <h4>Prompt Message History ({call.request.messages.length} turns)</h4>
                                  <div className="llm-prompt-box">
                                    {call.request.messages.map((m, idx) => (
                                      <div key={idx} style={{ marginBottom: '14px', borderBottom: '1px dashed rgba(255,255,255,0.05)', paddingBottom: '8px' }}>
                                        <strong>[{m.role}]:</strong>
                                        <div style={{ marginTop: '4px', whiteSpace: 'pre-wrap' }}>
                                          {typeof m.content === 'string' ? m.content : JSON.stringify(m.content, null, 2)}
                                        </div>
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}

                              {call.response_text && (
                                <div className="llm-details-section">
                                  <h4>Model Raw Output Text</h4>
                                  <div className="llm-response-box">
                                    {call.response_text}
                                  </div>
                                </div>
                              )}

                              {call.parsed_output && Object.keys(call.parsed_output).length > 0 && (
                                <div className="llm-details-section">
                                  <h4>Parsed Structured Proposals</h4>
                                  <div className="llm-json-box">
                                    {JSON.stringify(call.parsed_output, null, 2)}
                                  </div>
                                </div>
                              )}

                              <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', borderTop: '1px solid var(--border-color)', paddingTop: '10px', display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
                                <span>Provider: {call.provider}</span>
                                <span>ID: {call.provider_response_id || 'N/A'}</span>
                                <span>Repair Attempts: {call.parse_repair_attempts || 0}</span>
                              </div>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            )}

            {activeTab === 'events' && (
              <section className="automation-panel">
                <h2>Structured Run Events</h2>
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
                        <pre className="llm-json-box">{JSON.stringify(event.payload, null, 2)}</pre>
                      </details>
                    ))}
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      </div>

      <ErrorMessage message={error} />
    </div>
  )
}

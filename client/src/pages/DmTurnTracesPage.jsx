import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { apiFetch } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './DmTurnTracesPage.css'

function formatMs(ms) {
  const value = Number(ms) || 0
  return value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${value}ms`
}

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function trimText(value, max = 180) {
  const text = String(value || '').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function PhaseBars({ phases, totalMs }) {
  const total = Number(totalMs) || phases.reduce((sum, phase) => sum + Number(phase.duration_ms || 0), 0) || 1
  return (
    <div className="dm-turn-phases">
      {(phases || []).slice(0, 8).map((phase) => {
        const width = Math.max(3, Math.round((Number(phase.duration_ms || 0) / total) * 100))
        return (
          <div className="dm-turn-phase" key={phase.phase}>
            <div className="dm-turn-phase-label">
              <span>{phase.label}</span>
              <span>{formatMs(phase.duration_ms)} · {phase.event_count} events</span>
            </div>
            <div className="dm-turn-phase-track">
              <div className="dm-turn-phase-fill" style={{ width: `${width}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TurnTraceCard({ trace }) {
  const [open, setOpen] = useState(false)
  const result = trace.visible_result || {}
  const input = trace.player_input || {}
  return (
    <article className="dm-turn-card">
      <button className="dm-turn-card-head" onClick={() => setOpen((value) => !value)}>
        <div>
          <div className="dm-turn-title-row">
            <strong>{trace.turn_kind === 'opening' ? 'Opening scene' : `Player message #${trace.player_message_id || '?'}`}</strong>
            <span className={`dm-turn-mode dm-turn-mode-${result.mode || 'unknown'}`}>{result.mode || 'unknown'}</span>
          </div>
          <p>{trimText(input.content || result.content || result.reason || trace.trace_label)}</p>
        </div>
        <div className="dm-turn-meta">
          <span>{formatMs(trace.total_ms)}</span>
          <span>{formatTime(trace.started_at)}</span>
          <i className={`bi bi-chevron-${open ? 'up' : 'down'}`} />
        </div>
      </button>

      <PhaseBars phases={trace.phases || []} totalMs={trace.total_ms} />

      <div className="dm-turn-facts">
        <span>{trace.model_request_count || 0} model calls</span>
        <span>{trace.tool_names?.length || 0} tools</span>
        <span>{trace.guard_event_count || 0} guard events</span>
        <span>{trace.memory_event_count || 0} memory events</span>
      </div>

      {trace.tool_names?.length > 0 && (
        <div className="dm-turn-tools">
          {trace.tool_names.map((tool) => <span key={tool}>{tool}</span>)}
        </div>
      )}

      {open && (
        <div className="dm-turn-details">
          <section>
            <h4>Visible result</h4>
            <pre>{result.content || result.reason || '(no visible result)'}</pre>
          </section>
          <section>
            <h4>Timeline</h4>
            <div className="dm-turn-timeline">
              {(trace.timeline || []).map((event) => (
                <div className="dm-turn-timeline-row" key={event.event_id}>
                  <span>+{formatMs(event.delta_ms)}</span>
                  <strong>{event.phase_label}</strong>
                  <span>{event.event_type}</span>
                  <span>{event.summary || event.operation || event.actor || ''}</span>
                </div>
              ))}
            </div>
          </section>
        </div>
      )}
    </article>
  )
}

export default function DmTurnTracesPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [payload, setPayload] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  async function load(background = false) {
    if (background) setRefreshing(true)
    else setLoading(true)
    setError('')
    try {
      setPayload(await apiFetch(`/campaigns/${id}/dev/dm-turn-traces?limit=100`))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => { load(false) }, [id])

  const traces = payload?.traces || []
  const stats = useMemo(() => {
    if (!traces.length) return { average: 0, slowest: 0, modelCalls: 0 }
    return {
      average: Math.round(traces.reduce((sum, trace) => sum + Number(trace.total_ms || 0), 0) / traces.length),
      slowest: Math.max(...traces.map((trace) => Number(trace.total_ms || 0))),
      modelCalls: traces.reduce((sum, trace) => sum + Number(trace.model_request_count || 0), 0),
    }
  }, [traces])

  if (loading) return <Loading message="Loading DM turn traces..." />

  return (
    <div className="dm-turn-page">
      <header className="dm-turn-header">
        <div>
          <button className="dm-turn-back" onClick={() => navigate(`/campaigns/${id}/dev`)}>
            <i className="bi bi-arrow-left" /> Developer audit
          </button>
          <h1>DM Turn Traces</h1>
          <p>Per-turn phase timings, tool usage, guard activity, memory activity, and visible outputs.</p>
        </div>
        <button className="btn btn-secondary" onClick={() => load(true)} disabled={refreshing}>
          <i className={`bi bi-arrow-clockwise ${refreshing ? 'dm-turn-spin' : ''}`} /> Refresh
        </button>
      </header>

      {error && <ErrorMessage message={error} />}

      <section className="dm-turn-stat-grid">
        <div><strong>{traces.length}</strong><span>turns</span></div>
        <div><strong>{formatMs(stats.average)}</strong><span>average</span></div>
        <div><strong>{formatMs(stats.slowest)}</strong><span>slowest</span></div>
        <div><strong>{stats.modelCalls}</strong><span>model calls</span></div>
      </section>

      <main className="dm-turn-list">
        {traces.length ? traces.map((trace) => <TurnTraceCard trace={trace} key={trace.trace_id} />) : (
          <div className="dm-turn-empty">No DM turn traces found yet. Send a session message, then refresh.</div>
        )}
      </main>
    </div>
  )
}

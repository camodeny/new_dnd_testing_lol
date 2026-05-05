import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { getCampaignDevAudit } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

function formatDateTime(iso) {
  if (!iso) return 'Unknown'
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function stringify(value) {
  if (value == null) return 'null'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function joinNames(items, key = 'name') {
  return (items || []).map((item) => item?.[key]).filter(Boolean).join(', ')
}

function AuditMessage({ entry }) {
  return (
    <div className="campaign-dev-message">
      <div className="campaign-dev-message-meta">
        <span className={`campaign-dev-pill campaign-dev-pill-${entry.kind}`}>{entry.label}</span>
        <span>{formatDateTime(entry.created_at)}</span>
        {entry.context && <span>{entry.context}</span>}
      </div>
      <div className="campaign-dev-message-content">{entry.content || '(empty)'}</div>
    </div>
  )
}

function formatEventType(type) {
  return String(type || 'event').replace(/_/g, ' ')
}

function JsonPanel({ title, value, summary, defaultOpen = false }) {
  return (
    <details className="campaign-dev-details" open={defaultOpen}>
      <summary>
        <span>{title}</span>
        {summary && <span className="campaign-dev-summary">{summary}</span>}
      </summary>
      <pre className="campaign-dev-code">{stringify(value)}</pre>
    </details>
  )
}

function AuditEventCard({ event }) {
  const payload = event.payload || {}
  const messageCount = payload.messages?.length || payload.raw_response?.choices?.length || null

  return (
    <article className={`campaign-dev-event campaign-dev-event-${event.event_type}`}>
      <div className="campaign-dev-event-rail">
        <span>{event.id}</span>
      </div>
      <div className="campaign-dev-event-body">
        <div className="campaign-dev-message-meta">
          <span className={`campaign-dev-pill campaign-dev-pill-${event.event_type}`}>{formatEventType(event.event_type)}</span>
          <span>{formatDateTime(event.created_at)}</span>
          {event.actor && <span>actor {event.actor}</span>}
          {event.source && <span>source {event.source}</span>}
          {messageCount ? <span>{messageCount} item{messageCount === 1 ? '' : 's'}</span> : null}
        </div>
        <div className="campaign-dev-message-content">{event.summary}</div>
        <JsonPanel title="Payload" value={payload} summary="Full captured input/output" />
      </div>
    </article>
  )
}

export default function CampaignDevPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [lastLoadedAt, setLastLoadedAt] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true

    async function load(background = false) {
      if (background) {
        setRefreshing(true)
      } else {
        setLoading(true)
      }
      setError('')
      try {
        const payload = await getCampaignDevAudit(id)
        if (alive) {
          setData(payload)
          setLastLoadedAt(new Date())
        }
      } catch (err) {
        if (alive) setError(err.message)
      } finally {
        if (alive) {
          setLoading(false)
          setRefreshing(false)
        }
      }
    }

    load(false)

    let interval = null
    if (autoRefresh) {
      interval = window.setInterval(() => load(true), 2500)
    }

    return () => {
      alive = false
      if (interval) window.clearInterval(interval)
    }
  }, [autoRefresh, id])

  const timeline = useMemo(() => {
    if (!data) return []

    const entries = []
    const planningMessages = data.planning?.messages || []
    const sessions = data.sessions || []
    const worldEvents = data.world?.events || []

    planningMessages.forEach((message) => {
      entries.push({
        kind: `planning-${message.role}`,
        label: `Planning ${message.role}`,
        created_at: message.created_at,
        content: message.content,
        context: `Campaign ${id}`,
      })
    })

    sessions.forEach((session) => {
      session.messages?.forEach((message) => {
        entries.push({
          kind: `session-${message.role}`,
          label: `Session ${session.id} ${message.role}`,
          created_at: message.created_at,
          content: message.content,
          context: session.is_active ? 'active session' : `session ${session.id}`,
        })
      })
    })

    worldEvents.forEach((event) => {
      entries.push({
        kind: `world-${event.visibility || 'unknown'}`,
        label: `World event ${event.event_type}`,
        created_at: event.created_at,
        content: event.summary,
        context: event.visibility || 'unknown',
      })
    })

    return entries.sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
  }, [data, id])

  if (loading) return <Loading message="Loading campaign audit..." />
  if (error) return <ErrorMessage message={error} />
  if (!data) return <ErrorMessage message="Campaign audit not found." />

  const campaign = data.campaign
  const members = data.members || []
  const sessions = data.sessions || []
  const activeSession = data.active_session
  const worldPublic = data.world?.public || null
  const worldContext = data.world?.context || null
  const planning = data.planning || {}
  const planningVisible = planning.visible || {}
  const planningSummary = planning.summary || {}
  const latestSession = data.latest_session
  const auditEvents = data.audit_events || []
  const legacyTimelineCount = timeline.length
  const streamIsLive = autoRefresh && !error

  return (
    <div className="campaign-dev-page">
      <div className="campaign-dev-hero">
        <div className="campaign-dev-hero-copy">
          <div className="campaign-dev-kicker">Campaign audit</div>
          <h2>{campaign.name}</h2>
          <p>{campaign.description || 'No campaign description is set.'}</p>
          <div className="campaign-dev-chips">
            <span className="campaign-dev-chip">Members {members.length}</span>
            <span className="campaign-dev-chip">Sessions {sessions.length}</span>
            <span className="campaign-dev-chip">Planning messages {planning.messages?.length || 0}</span>
            <span className="campaign-dev-chip">World {worldPublic?.is_ready ? 'ready' : 'pending'}</span>
            <span className="campaign-dev-chip">Audit events {auditEvents.length}</span>
          </div>
        </div>
        <div className="campaign-dev-hero-actions">
          <button className="btn btn-secondary" onClick={() => navigate(`/campaigns/${id}`)}>
            Back to campaign
          </button>
          <div className="campaign-dev-live-controls">
            <button className="btn btn-secondary" onClick={() => setAutoRefresh((value) => !value)}>
              {autoRefresh ? 'Pause' : 'Resume'}
            </button>
            <button
              className="btn btn-secondary"
              onClick={async () => {
                setRefreshing(true)
                try {
                  const payload = await getCampaignDevAudit(id)
                  setData(payload)
                  setLastLoadedAt(new Date())
                } catch (err) {
                  setError(err.message)
                } finally {
                  setRefreshing(false)
                }
              }}
            >
              Refresh
            </button>
          </div>
          <div className="campaign-dev-route">/campaigns/{id}/dev</div>
        </div>
      </div>

      <div className="campaign-dev-layout">
        <aside className="campaign-dev-aside">
          <section className="campaign-dev-panel">
            <h3>Setup Snapshot</h3>
            <dl className="campaign-dev-stats">
              <div>
                <dt>Owner</dt>
                <dd>{data.campaign.owner_username || campaign.user_id}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{campaign.status || 'unknown'}</dd>
              </div>
              <div>
                <dt>Active session</dt>
                <dd>{activeSession ? `#${activeSession.id}` : 'none'}</dd>
              </div>
              <div>
                <dt>World approved</dt>
                <dd>{worldPublic?.world?.approved_at ? 'yes' : 'no'}</dd>
              </div>
            </dl>
          </section>

          <section className="campaign-dev-panel">
            <h3>Members</h3>
            <div className="campaign-dev-list">
              {members.map((member) => (
                <div key={member.id} className="campaign-dev-list-row">
                  <div>
                    <strong>{member.username || `User ${member.user_id}`}</strong>
                    <div className="campaign-dev-muted">
                      {member.role} {member.is_character_ready ? 'ready' : 'not ready'}
                    </div>
                  </div>
                  <div className="campaign-dev-muted">
                    {member.selected_character_id ? `Character ${member.selected_character_id}` : 'No character'}
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="campaign-dev-panel">
            <h3>Trace Limits</h3>
            <p className="campaign-dev-note">
              Hidden provider chain-of-thought is not returned to this app. This page shows the exact app
              inputs, system prompts, model payloads, raw model responses, graph reads and writes, and client
              payloads that the app can persist.
            </p>
          </section>
        </aside>

        <main className="campaign-dev-main">
          <section className="campaign-dev-panel campaign-dev-stream-panel">
            <div className="campaign-dev-section-header">
              <div>
                <h3>Live Audit Stream</h3>
                <span>
                  {streamIsLive ? 'watching' : 'paused'}{refreshing ? ', refreshing' : ''}
                  {lastLoadedAt ? `, last loaded ${formatDateTime(lastLoadedAt.toISOString())}` : ''}
                </span>
              </div>
              <span>{auditEvents.length || legacyTimelineCount} ordered entries</span>
            </div>
            <div className="campaign-dev-stream">
              {auditEvents.length === 0 ? (
                <div className="campaign-dev-empty">
                  No persisted audit events yet. New planning, world, and session actions will appear here as
                  they run. Older campaigns still show the reconstructed legacy timeline below.
                </div>
              ) : (
                auditEvents.map((event) => <AuditEventCard key={event.id} event={event} />)
              )}
            </div>
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>Planning Messages</h3>
              <span>{planning.messages?.length || 0} stored</span>
            </div>
            <div className="campaign-dev-message-list">
              {(planning.messages || []).map((message) => (
                <AuditMessage
                  key={message.id}
                  entry={{
                    kind: `planning-${message.role}`,
                    label: message.role === 'dm' ? 'DM' : 'Player',
                    created_at: message.created_at,
                    content: message.content,
                    context: `user ${message.user_id}`,
                  }}
                />
              ))}
            </div>
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>Planning Setup</h3>
              <span>State and prompts</span>
            </div>
            <JsonPanel title="Visible planning state" value={planningVisible} summary="What the current user can see" />
            <JsonPanel title="Planning summary" value={planningSummary} summary="Includes private planning memory" />
            <JsonPanel
              title="Planning DM prompt"
              value={planning.prompt_traces?.dm_response}
              summary="Reconstructed prompt sent to the planning DM"
            />
            <JsonPanel
              title="Planning summary update prompt"
              value={planning.prompt_traces?.summary_update}
              summary="Prompt used to update planning memory"
            />
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>World Package</h3>
              <span>{worldPublic?.is_ready ? 'ready' : 'pending'}</span>
            </div>
            <JsonPanel title="World public payload" value={worldPublic} summary="Campaign-facing world state" />
            <JsonPanel title="World context" value={worldContext} summary="DM-visible world memory" />
            <JsonPanel
              title="World genesis prompt"
              value={data.world?.prompt_traces?.world_genesis}
              summary="Prompt used to generate the world package"
            />
            <JsonPanel
              title="Opening scene prompt"
              value={data.world?.prompt_traces?.opening_scene}
              summary="Prompt used for the first visible DM message"
            />
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>World Details</h3>
              <span>{joinNames(data.world?.npc_actors || [], 'name')} </span>
            </div>
            <JsonPanel title="NPC actors" value={data.world?.npc_actors || []} summary="Full actor dossiers" />
            <JsonPanel title="Clocks" value={data.world?.clocks || []} summary="Visible and private pressure clocks" />
            <JsonPanel title="World events" value={data.world?.events || []} summary="Persisted world events" />
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>Session Transcripts</h3>
              <span>{sessions.length} sessions</span>
            </div>
            <div className="campaign-dev-session-list">
              {sessions.map((session) => (
                <details key={session.id} className="campaign-dev-details" open={session.is_active}>
                  <summary>
                    <span>
                      Session #{session.id}{session.is_active ? ' active' : ''}
                    </span>
                    <span className="campaign-dev-summary">
                      {session.messages?.length || 0} messages, started {formatDateTime(session.started_at)}
                    </span>
                  </summary>
                  <div className="campaign-dev-message-list">
                    {(session.messages || []).map((message) => (
                      <AuditMessage
                        key={message.id}
                        entry={{
                          kind: `session-${message.role}`,
                          label: message.role === 'dm' ? 'DM' : message.role === 'system' ? 'System' : 'Player',
                          created_at: message.created_at,
                          content: message.content,
                          context: `session ${session.id}`,
                        }}
                      />
                    ))}
                  </div>
                </details>
              ))}
            </div>
            <JsonPanel
              title="Session DM prompt"
              value={data.world?.prompt_traces?.session_dm_response}
              summary={`Reconstructed from ${latestSession ? `session ${latestSession.id}` : 'no session'}`}
            />
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>Legacy Timeline</h3>
              <span>{timeline.length} reconstructed entries</span>
            </div>
            <div className="campaign-dev-timeline">
              {timeline.length === 0 ? (
                <div className="campaign-dev-empty">No planning, session, or world events were found.</div>
              ) : (
                timeline.map((entry, index) => <AuditMessage key={`${entry.kind}-${index}-${entry.created_at}`} entry={entry} />)
              )}
            </div>
          </section>

          <section className="campaign-dev-panel">
            <div className="campaign-dev-section-header">
              <h3>Raw Payloads</h3>
              <span>Useful for debugging</span>
            </div>
            <JsonPanel title="Campaign" value={campaign} summary="Campaign record" />
            <JsonPanel title="Members" value={members} summary="Campaign membership records" />
            <JsonPanel title="Characters" value={data.characters || []} summary="Campaign-linked characters" />
          </section>
        </main>
      </div>
    </div>
  )
}

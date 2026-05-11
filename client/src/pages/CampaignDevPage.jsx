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

function roleLabel(role) {
  if (role === 'player') return 'Player'
  if (role === 'agent') return 'Agent'
  return 'Tools'
}

function summarizeContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  return stringify(content)
}

function MessageTranscript({ messages, title = 'Messages sent' }) {
  if (!messages?.length) return null

  return (
    <details className="campaign-dev-details campaign-dev-transcript">
      <summary>
        <span>{title}</span>
        <span className="campaign-dev-summary">{messages.length} message{messages.length === 1 ? '' : 's'}</span>
      </summary>
      <div className="campaign-dev-transcript-list">
        {messages.map((message, index) => (
          <div key={`${message.role || 'message'}-${index}`} className={`campaign-dev-transcript-message campaign-dev-transcript-${message.role || 'unknown'}`}>
            <div className="campaign-dev-message-meta">
              <span className={`campaign-dev-pill campaign-dev-pill-message-${message.role || 'unknown'}`}>{message.role || 'unknown'}</span>
              <span>message {index + 1}</span>
              {message.name && <span>name {message.name}</span>}
              {message.tool_call_id && <span>tool call {message.tool_call_id}</span>}
            </div>
            <pre className="campaign-dev-transcript-content">{summarizeContent(message.content) || '(empty)'}</pre>
            {Object.keys(message).some((key) => !['role', 'content', 'name', 'tool_call_id'].includes(key)) && (
              <JsonPanel title="Full message object" value={message} summary="All fields on this message" />
            )}
          </div>
        ))}
      </div>
    </details>
  )
}

function ReasoningPanel({ reasoning }) {
  if (!reasoning) return null
  const usage = reasoning.usage || {}

  return (
    <details className="campaign-dev-details campaign-dev-reasoning">
      <summary>
        <span>Reasoning</span>
        <span className="campaign-dev-summary">
          {reasoning.returned ? 'returned by provider' : 'No reasoning returned'}
          {usage.reasoning_tokens != null ? `, ${usage.reasoning_tokens} tokens` : ''}
        </span>
      </summary>
      {reasoning.returned ? (
        <div className="campaign-dev-reasoning-body">
          {reasoning.reasoning && (
            <pre className="campaign-dev-code">{reasoning.reasoning}</pre>
          )}
          {reasoning.reasoning_details && (
            <JsonPanel title="reasoning_details" value={reasoning.reasoning_details} summary="Provider-returned reasoning blocks" defaultOpen />
          )}
          <JsonPanel title="Reasoning usage" value={usage} summary="Usage fields related to reasoning tokens" />
        </div>
      ) : (
        <div className="campaign-dev-empty campaign-dev-empty-compact">
          This model response did not include OpenRouter reasoning fields.
        </div>
      )}
    </details>
  )
}

function ContextStrategyPanel({ payload }) {
  const manifest = payload?.context_manifest || {}
  const tokenEstimate = payload?.token_estimate || {}
  const toolNames = payload?.tool_names || manifest.available_tools || []
  if (!Object.keys(manifest).length && !Object.keys(tokenEstimate).length && !toolNames.length) return null

  return (
    <details className="campaign-dev-details campaign-dev-context-strategy" open>
      <summary>
        <span>Context Strategy</span>
        <span className="campaign-dev-summary">
          {manifest.strategy || 'model request'}
          {manifest.full_world_graph_included === false || tokenEstimate.full_world_graph_included === false ? ', full graph not fed' : ''}
        </span>
      </summary>
      <div className="campaign-dev-mini-grid">
        <div>
          <strong>Fed directly</strong>
          <div className="campaign-dev-muted">{(manifest.fed_sections || []).join(', ') || 'message history'}</div>
        </div>
        <div>
          <strong>Tools offered</strong>
          <div className="campaign-dev-muted">{toolNames.join(', ') || 'none'}</div>
        </div>
        <div>
          <strong>Estimated tokens</strong>
          <div className="campaign-dev-muted">
            {manifest.estimated_total_tokens || tokenEstimate.estimated_message_tokens || 'unknown'}
          </div>
        </div>
      </div>
      <JsonPanel title="Context manifest" value={manifest} summary="Fed context and available retrieval tools" />
      <JsonPanel title="Token estimate" value={tokenEstimate} summary="Approximate local estimate; provider usage is authoritative" />
    </details>
  )
}

function ToolCallsPanel({ entry, payload }) {
  const requested = entry.tool_calls || payload?.tool_calls || []
  const execution = payload?.tool_name ? {
    tool_name: payload.tool_name,
    arguments: payload.arguments,
    result: payload.result,
    mutated: payload.mutated,
    affected_ids: payload.affected_ids,
  } : null
  if (!requested.length && !execution) return null

  return (
    <details className="campaign-dev-details campaign-dev-tool-calls" open>
      <summary>
        <span>Tool Calls</span>
        <span className="campaign-dev-summary">
          {requested.length ? `${requested.length} requested` : execution?.tool_name}
          {execution?.mutated ? ', mutated state' : ''}
        </span>
      </summary>
      {requested.length ? <JsonPanel title="Requested tool calls" value={requested} summary="Model-requested tool invocations" defaultOpen /> : null}
      {execution ? <JsonPanel title="Tool execution" value={execution} summary="Server-side arguments, result, and affected records" defaultOpen /> : null}
    </details>
  )
}

function MemoryUpdatePanel({ entry, payload }) {
  if (!['memory_writer_request', 'memory_writer_response', 'memory_patch_applied'].includes(entry.event_type)) return null
  const summary = entry.event_type === 'memory_patch_applied'
    ? 'Applied graph, clock, NPC, event, and summary changes'
    : 'Post-turn memory writer model step'
  return (
    <details className="campaign-dev-details campaign-dev-memory-update" open>
      <summary>
        <span>Memory Update</span>
        <span className="campaign-dev-summary">{summary}</span>
      </summary>
      {payload.patch ? <JsonPanel title="Patch" value={payload.patch} summary="Structured memory writer output" defaultOpen /> : null}
      {payload.result ? <JsonPanel title="Applied result" value={payload.result} summary="DB mutations performed from the patch" defaultOpen /> : null}
      {payload.context ? <JsonPanel title="Memory writer context" value={payload.context} summary="Compact input used for durable memory updates" /> : null}
    </details>
  )
}

function AuditStreamCard({ entry }) {
  const payload = entry.payload || {}
  const messageCount = entry.message_count || payload.raw_response?.choices?.length || null
  const content = entry.content || payload.message?.content || payload.content || entry.summary

  return (
    <article className={`campaign-dev-event campaign-dev-stream-entry campaign-dev-stream-${entry.role}`}>
      <div className="campaign-dev-event-rail">
        <span>{entry.id}</span>
      </div>
      <div className="campaign-dev-event-body">
        <div className="campaign-dev-message-meta">
          <span className={`campaign-dev-pill campaign-dev-pill-role-${entry.role}`}>{roleLabel(entry.role)}</span>
          <span className={`campaign-dev-pill campaign-dev-pill-${entry.event_type}`}>{formatEventType(entry.event_type)}</span>
          <span>{formatDateTime(entry.created_at)}</span>
          {entry.actor && <span>actor {entry.actor}</span>}
          {entry.source && <span>source {entry.source}</span>}
          {entry.trace_id && <span>trace {entry.trace_id}</span>}
          {messageCount ? <span>{messageCount} item{messageCount === 1 ? '' : 's'}</span> : null}
        </div>
        <div className="campaign-dev-message-content">{content || '(empty)'}</div>
        <ContextStrategyPanel payload={payload} />
        <ToolCallsPanel entry={entry} payload={payload} />
        <MemoryUpdatePanel entry={entry} payload={payload} />
        <MessageTranscript messages={entry.messages} />
        <ReasoningPanel reasoning={entry.reasoning} />
        <JsonPanel title="Payload" value={payload} summary="Full captured input/output" />
      </div>
    </article>
  )
}

function AgentRunNode({ run, depth = 0 }) {
  const events = run.events || []
  const modelRequests = events.filter((event) => event.event_type === 'model_request')
  const modelResponses = events.filter((event) => event.event_type === 'model_response')
  const toolEvents = events.filter((event) => event.event_type === 'dm_tool_execution')
  const memoryEvents = events.filter((event) => String(event.event_type || '').startsWith('memory_'))

  return (
    <details className="campaign-dev-agent-run" open={depth === 0}>
      <summary>
        <span>{run.trace_label || run.trace_id}</span>
        <span className="campaign-dev-summary">
          {events.length} event{events.length === 1 ? '' : 's'}
          {modelRequests.length ? `, ${modelRequests.length} request${modelRequests.length === 1 ? '' : 's'}` : ''}
          {toolEvents.length ? `, ${toolEvents.length} tool call${toolEvents.length === 1 ? '' : 's'}` : ''}
          {memoryEvents.length ? `, ${memoryEvents.length} memory update${memoryEvents.length === 1 ? '' : 's'}` : ''}
          {modelResponses.some((event) => event.reasoning?.returned) ? ', reasoning returned' : ''}
        </span>
      </summary>
      <div className="campaign-dev-agent-run-body">
        <div className="campaign-dev-message-meta">
          <span>trace {run.trace_id}</span>
          {run.parent_trace_id && <span>parent {run.parent_trace_id}</span>}
          {run.actor && <span>actor {run.actor}</span>}
        </div>
        <div className="campaign-dev-agent-events">
          {events.map((event) => (
            <div key={event.id} className="campaign-dev-agent-event">
              <div className="campaign-dev-message-meta">
                <span className={`campaign-dev-pill campaign-dev-pill-role-${event.role}`}>{roleLabel(event.role)}</span>
                <span>{formatEventType(event.event_type)}</span>
                <span>{formatDateTime(event.created_at)}</span>
              </div>
              <div className="campaign-dev-message-content">{event.summary}</div>
              <ContextStrategyPanel payload={event.payload || {}} />
              <ToolCallsPanel entry={event} payload={event.payload || {}} />
              <MemoryUpdatePanel entry={event} payload={event.payload || {}} />
              <MessageTranscript messages={event.messages} title="Fed message history" />
              <ReasoningPanel reasoning={event.reasoning} />
            </div>
          ))}
        </div>
        {run.children?.length ? (
          <div className="campaign-dev-agent-children">
            {run.children.map((child) => <AgentRunNode key={child.trace_id} run={child} depth={depth + 1} />)}
          </div>
        ) : null}
      </div>
    </details>
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
  const auditStream = data.audit_stream || []
  const agentRuns = data.agent_runs || []
  const contextStrategy = data.world?.context_strategy || {}
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
            <span className="campaign-dev-chip">Audit entries {auditStream.length || auditEvents.length}</span>
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
              {data.audit_notes?.message || 'This page shows the exact app inputs, system prompts, model payloads, raw model responses, graph reads and writes, and client payloads that the app can persist.'}
            </p>
            {data.audit_notes?.thinking && (
              <p className="campaign-dev-note">
                {data.audit_notes.thinking}
              </p>
            )}
          </section>

          <section className="campaign-dev-panel">
            <h3>Audit Roles</h3>
            <div className="campaign-dev-role-key">
              <span className="campaign-dev-pill campaign-dev-pill-role-player">Player</span>
              <span>Player-submitted planning and session messages.</span>
              <span className="campaign-dev-pill campaign-dev-pill-role-agent">Agent</span>
              <span>Assistant/model output and agent-authored results.</span>
              <span className="campaign-dev-pill campaign-dev-pill-role-tools">Tools</span>
              <span>Context reads, writes, API calls, and client payloads.</span>
            </div>
          </section>

          <section className="campaign-dev-panel">
            <h3>Agent Runs</h3>
            <div className="campaign-dev-agent-tree">
              {agentRuns.length === 0 ? (
                <div className="campaign-dev-empty campaign-dev-empty-compact">
                  No agent trace metadata has been captured yet.
                </div>
              ) : (
                agentRuns.map((run) => <AgentRunNode key={run.trace_id} run={run} />)
              )}
            </div>
            <p className="campaign-dev-note">
              Trace groups include exact message history for model requests and any reasoning fields returned by the provider.
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
              <span>{auditStream.length || auditEvents.length || legacyTimelineCount} ordered entries</span>
            </div>
            <div className="campaign-dev-stream">
              {auditStream.length === 0 ? (
                <div className="campaign-dev-empty">
                  No persisted audit events yet. New planning, world, and session actions will appear here as
                  they run. Older campaigns still show the reconstructed legacy timeline below.
                </div>
              ) : (
                auditStream.map((entry) => <AuditStreamCard key={entry.id} entry={entry} />)
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
              title="Session context strategy"
              value={contextStrategy}
              summary="What live session DM turns feed directly versus retrieve through tools"
              defaultOpen
            />
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
              title="Session DM prompt + tools"
              value={data.world?.prompt_traces?.session_dm_response}
              summary={`Compact hot prompt reconstructed from ${latestSession ? `session ${latestSession.id}` : 'no session'}`}
            />
            <JsonPanel
              title="Legacy full session DM prompt"
              value={data.world?.prompt_traces?.legacy_full_session_dm_response}
              summary="Old full-context prompt kept here for comparison"
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

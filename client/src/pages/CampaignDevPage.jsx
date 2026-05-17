import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { getCampaignDevAudit } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'

const FLOW_FILTERS = [
  { key: 'planning', label: 'Planning', icon: 'bi-journal-text' },
  { key: 'sessions', label: 'Sessions', icon: 'bi-chat-dots' },
  { key: 'agents', label: 'Agents', icon: 'bi-robot' },
  { key: 'tools', label: 'Tools', icon: 'bi-wrench' },
  { key: 'memory', label: 'Memory', icon: 'bi-database' },
  { key: 'raw', label: 'Raw', icon: 'bi-code-slash' },
]

const DEFAULT_FILTERS = FLOW_FILTERS.reduce((acc, filter) => {
  acc[filter.key] = filter.key !== 'raw'
  return acc
}, {})

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

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
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

function formatEventType(type) {
  return String(type || 'event').replace(/_/g, ' ')
}

function summarizeContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  return stringify(content)
}

function roleLabel(role) {
  if (role === 'dm') return 'DM'
  if (role === 'player') return 'Player'
  if (role === 'assistant') return 'Assistant'
  if (role === 'system') return 'System'
  if (role === 'tool') return 'Tool'
  return role || 'Message'
}

function stepCategoryLabel(step, category) {
  if (step.kind === 'prompt_message') return 'model input'
  return category
}

function stepCategoryPillClass(step, category) {
  if (step.kind === 'prompt_message') return 'dev-pill-cat-prompt'
  return `dev-pill-cat-${category}`
}

function branchCategory(branch) {
  const actor = String(branch.actor || '')
  const hasMemory = actor.includes('memory') || branch.memory_events?.length
  const hasTools = branch.tool_events?.length || branch.role === 'tools'
  const hasAgentShape = actor.includes('dm') || actor.includes('architect') || actor.includes('draft') || branch.response || branch.messages?.length
  if (hasMemory) return 'memory'
  if (hasAgentShape) return 'agents'
  if (hasTools) return 'tools'
  return 'agents'
}

function branchHasStepCategory(branch, category) {
  if ((branch.steps || []).some((step) => step.category === category)) return true
  return (branch.children || []).some((child) => branchHasStepCategory(child, category))
}

function branchVisible(branch, filters) {
  if (branchHasStepCategory(branch, 'tools') && filters.tools) return true
  if (branchHasStepCategory(branch, 'memory') && filters.memory) return true
  return filters[branchCategory(branch)]
}

function stepVisible(step, filters) {
  const category = step.category || 'agents'
  if (category === 'tools') return filters.tools
  if (category === 'memory') return filters.memory
  return filters.agents
}

function isSessionDmBranch(branch) {
  if (branch.actor !== 'session_dm') return false
  if (['opening_scene', 'session_dm_response'].includes(branch.operation)) return true
  return (branch.steps || []).some((step) => ['dm_output_stored', 'dm_silence_chosen', 'dm_output_empty'].includes(step.kind))
}

function isMemoryBranch(branch) {
  return branchCategory(branch) === 'memory' || branchHasStepCategory(branch, 'memory')
}

function mainThreadSteps(branches, filters, { includeSetup = false } = {}) {
  return (branches || [])
    .filter(isSessionDmBranch)
    .flatMap((branch) => branch.steps || [])
    .filter((step) => stepVisible(step, filters))
    .filter((step) => includeSetup || step.kind !== 'prompt_message')
    .filter((step) => step.kind !== 'dm_output_stored')
}

function sideBranchesForMessage(message, previousMessage) {
  const ownBranches = (message.branches || []).filter((branch) => !isSessionDmBranch(branch))
  const ownSessionChildren = (message.branches || [])
    .filter(isSessionDmBranch)
    .flatMap((branch) => branch.children || [])
    .filter((branch) => !isMemoryBranch(branch))
  if (message.role !== 'dm' || !previousMessage) return [...ownBranches, ...ownSessionChildren]

  const priorDmChildren = (previousMessage.branches || [])
    .filter(isSessionDmBranch)
    .flatMap((branch) => branch.children || [])
    .filter(isMemoryBranch)

  return [...ownBranches, ...ownSessionChildren, ...priorDmChildren]
}

function laneVisible(lane, filters) {
  if (lane.type === 'planning') return filters.planning
  if (lane.type === 'session') return filters.sessions
  return filters.agents || filters.tools || filters.memory
}

function matchesSearch(branch, query) {
  if (!query) return true
  const q = query.toLowerCase()
  const fields = [
    branch.trace_label,
    branch.actor,
    branch.summary,
    branch.response,
    branch.operation,
  ]
  if (fields.some((f) => f && String(f).toLowerCase().includes(q))) return true
  if (branch.messages?.some((m) => m.content && String(m.content).toLowerCase().includes(q))) return true
  if (branch.tool_events?.some((e) => stringify(e).toLowerCase().includes(q))) return true
  if (branch.memory_events?.some((e) => stringify(e).toLowerCase().includes(q))) return true
  if ((branch.steps || []).some((step) => stringify(step).toLowerCase().includes(q))) return true
  return false
}

function messageMatchesSearch(message, query) {
  if (!query) return true
  const q = query.toLowerCase()
  if (message.content && String(message.content).toLowerCase().includes(q)) return true
  if (message.username && String(message.username).toLowerCase().includes(q)) return true
  if (message.role && String(message.role).toLowerCase().includes(q)) return true
  return false
}

function extractActors(chatFlow) {
  const actors = new Set()
  for (const lane of chatFlow.lanes || []) {
    for (const msg of lane.messages || []) {
      for (const branch of msg.branches || []) {
        if (branch.actor) actors.add(branch.actor)
      }
    }
    for (const branch of lane.branches || []) {
      if (branch.actor) actors.add(branch.actor)
    }
  }
  return [...actors].sort()
}

function isBranchError(branch) {
  return branch.event_ids?.some((id) => String(id).includes('error')) ||
    String(branch.actor || '').includes('error') ||
    String(branch.summary || '').toLowerCase().includes('error')
}

function formatTokens(n) {
  if (n == null) return null
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function requestTokenEstimate(entry) {
  const estimate = entry?.token_estimate || {}
  const messageTokens = Number(estimate.estimated_message_tokens) || 0
  const toolSchemaTokens = Number(estimate.estimated_tool_schema_tokens) || 0
  return messageTokens + toolSchemaTokens
}

function tokenStats(auditStream = []) {
  const stats = {
    estimatedSentTotal: 0,
    latestRequestSent: null,
    latestSessionDmSent: null,
    providerPromptTotal: 0,
    providerTotal: 0,
    hasProviderUsage: false,
  }

  for (const entry of auditStream) {
    if (entry.event_type === 'model_request') {
      const estimate = requestTokenEstimate(entry)
      stats.estimatedSentTotal += estimate
      stats.latestRequestSent = estimate
      if (entry.actor === 'session_dm') stats.latestSessionDmSent = estimate
    }

    if (entry.event_type === 'model_response') {
      const usage = entry.usage || {}
      const promptTokens = Number(usage.prompt_tokens)
      const totalTokens = Number(usage.total_tokens)
      if (Number.isFinite(promptTokens)) {
        stats.providerPromptTotal += promptTokens
        stats.hasProviderUsage = true
      }
      if (Number.isFinite(totalTokens)) {
        stats.providerTotal += totalTokens
        stats.hasProviderUsage = true
      }
    }
  }

  return stats
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function CopyButton({ value, label = 'Copy' }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async (e) => {
    e.stopPropagation()
    const text = typeof value === 'string' ? value : stringify(value)
    const ok = await copyToClipboard(text)
    if (ok) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  return (
    <button className={`dev-copy-btn ${copied ? 'dev-copy-btn-copied' : ''}`} onClick={handleCopy} title={label}>
      <i className={copied ? 'bi bi-check' : 'bi bi-clipboard'} />
    </button>
  )
}

function JsonPanel({ title, value, summary, defaultOpen = false }) {
  return (
    <details className="dev-details" open={defaultOpen}>
      <summary>
        <span className="dev-details-title">{title}</span>
        {summary && <span className="dev-details-summary">{summary}</span>}
      </summary>
      <div className="dev-code-wrapper">
        <CopyButton value={value} label={`Copy ${title}`} />
        <pre className="dev-code">{stringify(value)}</pre>
      </div>
    </details>
  )
}

function TruncatedContent({ content, maxLength = 500 }) {
  const text = summarizeContent(content)
  const [expanded, setExpanded] = useState(false)

  if (!text || text.length <= maxLength) {
    return <pre className="dev-transcript-content">{text || '(empty)'}</pre>
  }

  return (
    <div className="dev-truncated">
      <pre className="dev-transcript-content">{expanded ? text : text.slice(0, maxLength) + '...'}</pre>
      <button className="dev-expand-btn" onClick={() => setExpanded(!expanded)}>
        {expanded ? 'Show less' : `Show all ${text.length.toLocaleString()} chars`}
      </button>
    </div>
  )
}

function MessageTranscript({ messages, title = 'Message history', defaultOpen = false }) {
  if (!messages?.length) return null

  return (
    <details className="dev-details dev-transcript" open={defaultOpen}>
      <summary>
        <span className="dev-details-title">{title}</span>
        <span className="dev-details-summary">{messages.length} msg{messages.length === 1 ? '' : 's'}</span>
      </summary>
      <div className="dev-transcript-list">
        {messages.map((message, index) => (
          <div key={`${message.role || 'message'}-${index}`} className={`dev-transcript-message dev-transcript-${message.role || 'unknown'}`}>
            <div className="dev-message-meta">
              <span className={`dev-pill dev-pill-msg-${message.role || 'unknown'}`}>{message.role || '?'}</span>
              <span>#{index + 1}</span>
              {message.name && <span className="dev-meta-tag">{message.name}</span>}
              {message.tool_call_id && <span className="dev-meta-tag">tool: {message.tool_call_id}</span>}
            </div>
            <TruncatedContent content={message.content} />
          </div>
        ))}
      </div>
    </details>
  )
}

function ReasoningPanel({ reasoning }) {
  if (!reasoning) return null
  const usage = reasoning.usage || {}
  const hasContent = reasoning.reasoning || reasoning.reasoning_details

  return (
    <details className="dev-details dev-reasoning">
      <summary>
        <span className="dev-details-title">
          <i className="bi bi-lightbulb" /> Reasoning
        </span>
        <span className="dev-details-summary">
          {reasoning.returned ? 'returned' : 'not returned'}
          {usage.reasoning_tokens != null ? ` · ${formatTokens(usage.reasoning_tokens)} tok` : ''}
        </span>
      </summary>
      {hasContent ? (
        <div className="dev-reasoning-body">
          {reasoning.reasoning && (
            <div className="dev-code-wrapper">
              <CopyButton value={reasoning.reasoning} label="Copy reasoning" />
              <pre className="dev-code dev-code-reasoning">{reasoning.reasoning}</pre>
            </div>
          )}
          {reasoning.reasoning_details && (
            <JsonPanel title="reasoning_details" value={reasoning.reasoning_details} summary="Provider blocks" defaultOpen />
          )}
          {Object.keys(usage).length > 0 && (
            <JsonPanel title="Usage" value={usage} summary="Token usage fields" />
          )}
        </div>
      ) : (
        <div className="dev-empty-compact">No reasoning content was returned by the provider.</div>
      )}
    </details>
  )
}

function stepIcon(kind, category) {
  if (category === 'tools') return 'bi-wrench'
  if (category === 'memory') return 'bi-database'
  if (kind === 'prompt_message') return 'bi-chat-left-text'
  if (kind === 'model_request') return 'bi-send'
  if (kind === 'model_response') return 'bi-chat-square-text'
  if (kind === 'dm_output_stored') return 'bi-arrow-return-right'
  return 'bi-activity'
}

function BranchStepCard({ step, inline = false }) {
  const category = step.category || 'agents'
  const categoryLabel = stepCategoryLabel(step, category)
  const categoryPillClass = stepCategoryPillClass(step, category)
  const hasUsage = step.usage && Object.keys(step.usage).length > 0
  const hasPayload = step.payload && Object.keys(step.payload).length > 0
  const showRawPayload = hasPayload && !['model_request', 'model_response', 'dm_tool_execution'].includes(step.kind)
  const contentMax = step.kind === 'prompt_message' && step.prompt_role === 'system' ? 900 : step.kind === 'dm_output_stored' ? 360 : 520
  return (
    <article className={`dev-step-card dev-step-${category} ${inline ? 'dev-step-inline' : ''}`}>
      <div className="dev-step-header">
        <span className={`dev-step-icon dev-step-icon-${category}`}>
          <i className={`bi ${stepIcon(step.kind, category)}`} />
        </span>
        <div className="dev-step-heading">
          <div className="dev-step-title-row">
            <strong>{step.title || formatEventType(step.kind)}</strong>
            {step.prompt_role && <span className={`dev-pill dev-pill-msg-${step.prompt_role}`}>{roleLabel(step.prompt_role)}</span>}
            <span className={`dev-pill ${categoryPillClass}`}>{categoryLabel}</span>
            {step.provider && <span className="dev-pill dev-pill-cat-tools">{step.provider}</span>}
            {step.model && <span className="dev-pill dev-pill-cat-prompt">{step.model}</span>}
          </div>
          <div className="dev-step-meta">
            {step.actor && <span>{step.actor}</span>}
            {step.summary && <span>{step.summary}</span>}
            {step.event_id && <span>event {step.event_id}</span>}
            {step.name && <span>{step.name}</span>}
            {step.tool_call_id && <span>tool {step.tool_call_id}</span>}
            {step.created_at && <span>{formatDateTime(step.created_at)}</span>}
          </div>
        </div>
      </div>

      {step.content != null && (
        <div className="dev-step-content">
          <TruncatedContent content={step.content} maxLength={contentMax} />
        </div>
      )}

      <MessageTranscript
        messages={step.messages}
        title={step.kind === 'model_request' ? 'Prompt payload sent to provider' : 'Messages sent'}
      />

      {step.tool_calls?.length > 0 && (
        <JsonPanel title="Requested tool calls" value={step.tool_calls} summary={`${step.tool_calls.length} call${step.tool_calls.length === 1 ? '' : 's'}`} />
      )}
      {step.tool_call && (
        <JsonPanel title="Tool call payload" value={step.tool_call} summary="Provider request" />
      )}
      {step.arguments != null && (
        <JsonPanel title="Arguments" value={step.arguments} summary={step.tool_name || step.title} defaultOpen={category === 'tools'} />
      )}
      {step.result != null && (
        <JsonPanel title="Result" value={step.result} summary={step.mutated ? 'mutated state' : 'read result'} />
      )}
      {step.affected_ids && (
        <JsonPanel title="Affected records" value={step.affected_ids} summary="Database ids" />
      )}
      <ReasoningPanel reasoning={step.reasoning} />
      {hasUsage && <JsonPanel title="Usage" value={step.usage} summary="Provider usage" />}
      {showRawPayload && <JsonPanel title="Payload" value={step.payload} summary="Raw event payload" />}
    </article>
  )
}

function MainThreadSteps({ steps }) {
  if (!steps.length) return null
  return (
    <div className="dev-main-step-list">
      {steps.map((step) => (
        <BranchStepCard key={step.id || `${step.event_id}-${step.kind}`} step={step} inline />
      ))}
    </div>
  )
}

function BranchTimeline({ branch, filters }) {
  const steps = (branch.steps || []).filter((step) => stepVisible(step, filters))
  if (!steps.length) {
    return <div className="dev-empty-compact">No branch steps match the active filters.</div>
  }

  return (
    <div className="dev-branch-timeline">
      {steps.map((step) => (
        <BranchStepCard key={step.id || `${step.event_id}-${step.kind}`} step={step} />
      ))}
    </div>
  )
}

function BranchRun({ branch, filters, compact = false }) {
  if (!branchVisible(branch, filters)) return null

  const category = branchCategory(branch)
  const isError = isBranchError(branch)
  const eventCount = branch.event_ids?.length || branch.events?.length || 0
  const toolNames = [...new Set((branch.tool_events || []).map((event) => event.tool_name || formatEventType(event.event_type)).filter(Boolean))]
  const stepCount = (branch.steps || []).length
  const branchClassName = [
    'dev-branch-run',
    `dev-branch-run-${category}`,
    compact ? 'dev-branch-run-compact' : '',
    isError ? 'dev-branch-run-error' : '',
  ].filter(Boolean).join(' ')

  return (
    <div className={branchClassName}>
      <div className="dev-branch-run-header">
        <span className={`dev-pill dev-pill-cat-${category}`}>{category}</span>
        <span className="dev-branch-label">{branch.trace_label || branch.actor || branch.summary || 'Branch'}</span>
        {branch.operation && <span className="dev-branch-op">{formatEventType(branch.operation)}</span>}
        {branch.provider && <span className="dev-branch-op">{branch.provider}</span>}
        {branch.model && <span className="dev-branch-op">{branch.model}</span>}
        <span className="dev-branch-op">{stepCount} step{stepCount === 1 ? '' : 's'}</span>
        {toolNames.length > 0 && (
          <span className="dev-branch-op">
            <i className="bi bi-wrench" /> {toolNames.join(', ')}
          </span>
        )}
      </div>
      {branch.summary && <p className="dev-branch-summary">{branch.summary}</p>}
      <BranchTimeline branch={branch} filters={filters} />
      <JsonPanel
        title="Raw events"
        value={branch.events || []}
        summary={`${eventCount} event${eventCount === 1 ? '' : 's'}`}
      />
    </div>
  )
}

function splitBranches(branches) {
  return branches.reduce((groups, branch, index) => {
    const category = branchCategory(branch)
    if (category === 'memory' || index % 2 === 1) {
      groups.right.push(branch)
    } else {
      groups.left.push(branch)
    }
    return groups
  }, { left: [], right: [] })
}

function BranchStack({ branch, filters, compact = false }) {
  return (
    <div className="dev-graph-branch-stack">
      <BranchRun branch={branch} filters={filters} compact={compact} />
      {(branch.children || []).filter((child) => branchVisible(child, filters)).map((child) => (
        <div key={child.id} className="dev-graph-child">
          <BranchStack branch={child} filters={filters} compact />
        </div>
      ))}
    </div>
  )
}

function BranchColumn({ branches, filters, side }) {
  const branchClassName = [
    'dev-graph-branches',
    `dev-graph-branches-${side}`,
    branches.length ? 'dev-graph-branches-has-branches' : '',
  ].filter(Boolean).join(' ')

  if (!branches.length) return <div className={branchClassName} />

  return (
    <div className={branchClassName}>
      {branches.map((branch) => (
        <BranchStack key={branch.id} branch={branch} filters={filters} />
      ))}
    </div>
  )
}

function FlowGraphNode({ message, previousMessage, filters, isLast, searchQuery }) {
  const sideBranches = sideBranchesForMessage(message, previousMessage)
  const branches = sideBranches.filter((branch) => branchVisible(branch, filters) && matchesSearch(branch, searchQuery))
  const includeSetup = message.role === 'dm' && (message.branches || []).some((branch) => branch.actor === 'session_dm' && branch.operation === 'opening_scene')
  const steps = mainThreadSteps(message.branches || [], filters, { includeSetup })
  const stepsBeforeMessage = message.role === 'dm' ? steps : []
  const stepsAfterMessage = message.role === 'dm' ? [] : steps
  const { left, right } = splitBranches(branches)
  const role = message.role === 'dm' ? 'dm' : message.role === 'system' ? 'system' : 'player'
  const rowClassName = [
    'dev-graph-row',
    branches.length ? 'has-branches' : '',
    left.length ? 'has-left-branches' : '',
    right.length ? 'has-right-branches' : '',
    isLast ? 'is-last' : '',
  ].filter(Boolean).join(' ')

  if (searchQuery && !messageMatchesSearch(message, searchQuery) && branches.length === 0 && !steps.some((step) => stringify(step).toLowerCase().includes(searchQuery.toLowerCase()))) return null

  return (
    <div className={rowClassName}>
      <BranchColumn branches={left} filters={filters} side="left" />
      <div className="dev-main-thread-stack">
        <MainThreadSteps steps={stepsBeforeMessage} />
        <article className={`dev-graph-node dev-graph-message dev-graph-message-${role}`}>
          <div className="dev-message-meta">
            <span className={`dev-pill dev-pill-${message.source}-${message.role}`}>{roleLabel(message.role)}</span>
            {message.username && <span className="dev-meta-tag">{message.username}</span>}
            <span className="dev-meta-time">{formatTime(message.created_at)}</span>
            {branches.length > 0 && <span className="dev-branch-count">{branches.length} branch{branches.length === 1 ? '' : 'es'}</span>}
          </div>
          <div className="dev-message-content">{message.content || '(empty)'}</div>
        </article>
        <MainThreadSteps steps={stepsAfterMessage} />
      </div>
      <BranchColumn branches={right} filters={filters} side="right" />
    </div>
  )
}

function FlowLane({ lane, filters, searchQuery }) {
  const branches = (lane.branches || []).filter((branch) => branchVisible(branch, filters) && matchesSearch(branch, searchQuery))
  const messages = lane.messages || []

  const filteredMessages = searchQuery
    ? messages.filter((m) => messageMatchesSearch(m, searchQuery) || (m.branches || []).some((b) => matchesSearch(b, searchQuery)))
    : messages
  const unlinkedLeft = branches.filter((_, index) => index % 2 === 0)
  const unlinkedRight = branches.filter((_, index) => index % 2 === 1)
  const unlinkedRowClassName = [
    'dev-graph-row',
    'dev-graph-row-unlinked',
    'is-last',
    branches.length ? 'has-branches' : '',
    unlinkedLeft.length ? 'has-left-branches' : '',
    unlinkedRight.length ? 'has-right-branches' : '',
  ].filter(Boolean).join(' ')

  if (searchQuery && filteredMessages.length === 0 && branches.length === 0) return null

  return (
    <section className={`dev-panel dev-flow-lane dev-flow-lane-${lane.type}`}>
      <div className="dev-section-header">
        <div>
          <h3>{lane.title}</h3>
          <span className="dev-section-subtitle">{lane.subtitle}</span>
        </div>
        <span className="dev-section-count">
          {filteredMessages.length} msg{filteredMessages.length === 1 ? '' : 's'}
          {branches.length ? ` · ${branches.length} unlinked` : ''}
        </span>
      </div>

      <div className="dev-graph">
        {filteredMessages.map((message, index) => (
          <FlowGraphNode
            key={message.key}
            message={message}
            previousMessage={filteredMessages[index - 1]}
            filters={filters}
            searchQuery={searchQuery}
            isLast={index === filteredMessages.length - 1 && branches.length === 0}
          />
        ))}
        {branches.length > 0 && (
          <div className={unlinkedRowClassName}>
            <BranchColumn branches={unlinkedLeft} filters={filters} side="left" />
            <div className="dev-graph-node dev-graph-message dev-graph-message-system">
              <div className="dev-message-meta">
                <span className="dev-pill dev-pill-cat-tools">Unlinked</span>
                <span>{branches.length} branch{branches.length === 1 ? '' : 'es'}</span>
              </div>
              <div className="dev-message-content">Agent activity not tied to a visible chat message.</div>
            </div>
            <BranchColumn branches={unlinkedRight} filters={filters} side="right" />
          </div>
        )}
        {(!filteredMessages.length && !branches.length) && (
          <div className="dev-empty">No matching entries for the active filters.</div>
        )}
      </div>
    </section>
  )
}

function FilterPanel({ filters, setFilters, chatFlow, data, searchQuery, setSearchQuery, actors, actorFilter, setActorFilter, onExpandAll, onCollapseAll }) {
  const stats = chatFlow?.stats || {}
  const auditCount = data.audit_stream?.length || data.audit_events?.length || 0
  const tokens = tokenStats(data.audit_stream || [])

  return (
    <aside className="dev-aside">
      <div className="dev-search-wrapper">
        <i className="bi bi-search dev-search-icon" />
        <input
          type="text"
          className="dev-search-input"
          placeholder="Search content, actors, labels..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
        {searchQuery && (
          <button className="dev-search-clear" onClick={() => setSearchQuery('')}>
            <i className="bi bi-x" />
          </button>
        )}
      </div>

      <section className="dev-panel">
        <div className="dev-panel-header">
          <h3>Filters</h3>
          <div className="dev-panel-actions">
            <button className="dev-link-btn" onClick={onExpandAll}>Expand all</button>
            <span className="dev-panel-sep">·</span>
            <button className="dev-link-btn" onClick={onCollapseAll}>Collapse</button>
          </div>
        </div>
        <div className="dev-filter-pills">
          {FLOW_FILTERS.map((filter) => (
            <button
              key={filter.key}
              className={`dev-filter-pill ${filters[filter.key] ? 'dev-filter-pill-active' : ''}`}
              onClick={() => setFilters((current) => ({ ...current, [filter.key]: !current[filter.key] }))}
            >
              <i className={`bi ${filter.icon}`} />
              {filter.label}
            </button>
          ))}
        </div>
      </section>

      {actors.length > 0 && (
        <section className="dev-panel">
          <h3>Actors</h3>
          <div className="dev-actor-chips">
            <button
              className={`dev-actor-chip ${actorFilter.length === 0 ? 'dev-actor-chip-active' : ''}`}
              onClick={() => setActorFilter([])}
            >
              All
            </button>
            {actors.map((actor) => (
              <button
                key={actor}
                className={`dev-actor-chip ${actorFilter.includes(actor) ? 'dev-actor-chip-active' : ''}`}
                onClick={() => {
                  setActorFilter((current) =>
                    current.includes(actor) ? current.filter((a) => a !== actor) : [...current, actor]
                  )
                }}
              >
                {actor.replace(/_/g, ' ')}
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="dev-panel">
        <h3>Summary</h3>
        <div className="dev-stats-grid">
          <div className="dev-stat">
            <span className="dev-stat-value">{stats.visible_message_count || 0}</span>
            <span className="dev-stat-label">Messages</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">{stats.linked_branch_count || 0}</span>
            <span className="dev-stat-label">Branches</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">{stats.unlinked_branch_count || 0}</span>
            <span className="dev-stat-label">Unlinked</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">{auditCount}</span>
            <span className="dev-stat-label">Events</span>
          </div>
        </div>
      </section>

      <section className="dev-panel">
        <h3>Tokens</h3>
        <div className="dev-stats-grid">
          <div className="dev-stat">
            <span className="dev-stat-value">{formatTokens(tokens.estimatedSentTotal)}</span>
            <span className="dev-stat-label">Estimated Sent</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">{formatTokens(tokens.latestRequestSent) || '—'}</span>
            <span className="dev-stat-label">Latest Request</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">{formatTokens(tokens.latestSessionDmSent) || '—'}</span>
            <span className="dev-stat-label">Latest Main DM</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">
              {tokens.hasProviderUsage ? formatTokens(tokens.providerPromptTotal) : '—'}
            </span>
            <span className="dev-stat-label">Provider Prompt</span>
          </div>
          <div className="dev-stat">
            <span className="dev-stat-value">
              {tokens.hasProviderUsage ? formatTokens(tokens.providerTotal) : '—'}
            </span>
            <span className="dev-stat-label">Provider Total</span>
          </div>
        </div>
      </section>

      <section className="dev-panel dev-panel-note">
        <p>
          The center thread shows the causal DM chain: visible messages, agent setup prompts, model requests, returned reasoning, tool calls, tool results, and model responses. Full provider prompt payloads stay collapsed on each model request.
        </p>
        {data.audit_notes?.thinking && (
          <p className="dev-note-extra">{data.audit_notes.thinking}</p>
        )}
      </section>
    </aside>
  )
}

function useExpandCollapse() {
  const [expandKey, setExpandKey] = useState(0)

  const expandAll = useCallback(() => {
    document.querySelectorAll('.dev-graph-branch, .dev-details').forEach((el) => {
      if (el.tagName === 'DETAILS') el.open = true
    })
    setExpandKey((k) => k + 1)
  }, [])

  const collapseAll = useCallback(() => {
    document.querySelectorAll('.dev-graph-branch, .dev-details').forEach((el) => {
      if (el.tagName === 'DETAILS') el.open = false
    })
    setExpandKey((k) => k + 1)
  }, [])

  return { expandKey, expandAll, collapseAll }
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
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [searchQuery, setSearchQuery] = useState('')
  const [actorFilter, setActorFilter] = useState([])
  const { expandKey, expandAll, collapseAll } = useExpandCollapse()
  const pulseRef = useRef(false)

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
          if (background) {
            pulseRef.current = true
            setTimeout(() => { pulseRef.current = false }, 600)
          }
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

  const chatFlow = useMemo(() => data?.chat_flow || { lanes: [], stats: {} }, [data])
  const actors = useMemo(() => extractActors(chatFlow), [chatFlow])

  const visibleLanes = useMemo(() => {
    const lanes = chatFlow.lanes || []
    return lanes
      .filter((lane) => laneVisible(lane, filters))
      .map((lane) => {
        if (actorFilter.length === 0) return lane
        const filteredMessages = (lane.messages || []).map((msg) => ({
          ...msg,
          branches: (msg.branches || []).filter((b) => actorFilter.includes(b.actor)),
        }))
        const filteredBranches = (lane.branches || []).filter((b) => actorFilter.includes(b.actor))
        return { ...lane, messages: filteredMessages, branches: filteredBranches }
      })
  }, [chatFlow, filters, actorFilter])

  if (loading) return <Loading message="Loading audit data..." />
  if (error) return <ErrorMessage message={error} />
  if (!data) return <ErrorMessage message="Campaign audit data not found." />

  const campaign = data.campaign || {}
  const members = data.members || []
  const sessions = data.sessions || []
  const planningCount = data.planning?.messages?.length || 0
  const streamIsLive = autoRefresh && !error

  const handleManualRefresh = async () => {
    setRefreshing(true)
    try {
      const payload = await getCampaignDevAudit(id)
      setData(payload)
      setLastLoadedAt(new Date())
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="dev-page">
      <header className="dev-header">
        <div className="dev-breadcrumb">
          <button className="dev-breadcrumb-link" onClick={() => navigate(`/campaigns/${id}`)}>
            <i className="bi bi-arrow-left" />
            {campaign.name}
          </button>
          <span className="dev-breadcrumb-sep">/</span>
          <span className="dev-breadcrumb-current">Developer Audit</span>
        </div>

        <div className="dev-header-right">
          <div className="dev-chips-row">
            <span className="dev-chip">{members.length} members</span>
            <span className="dev-chip">{sessions.length} sessions</span>
            <span className="dev-chip">{planningCount} planning</span>
          </div>

          <div className="dev-controls">
            <button className={`dev-live-toggle ${streamIsLive ? 'dev-live-active' : ''}`} onClick={() => setAutoRefresh((v) => !v)}>
              <span className={`dev-live-dot ${streamIsLive ? 'dev-live-dot-on' : ''}`} />
              {streamIsLive ? 'Live' : 'Paused'}
            </button>
            <button className="btn btn-secondary btn-small" onClick={handleManualRefresh} disabled={refreshing}>
              <i className={`bi bi-arrow-clockwise ${refreshing ? 'dev-spin' : ''}`} />
              Refresh
            </button>
            {lastLoadedAt && (
              <span className="dev-last-updated">{formatTime(lastLoadedAt.toISOString())}</span>
            )}
          </div>
        </div>
      </header>

      <div className="dev-layout">
        <FilterPanel
          filters={filters}
          setFilters={setFilters}
          chatFlow={chatFlow}
          data={data}
          searchQuery={searchQuery}
          setSearchQuery={setSearchQuery}
          actors={actors}
          actorFilter={actorFilter}
          setActorFilter={setActorFilter}
          onExpandAll={expandAll}
          onCollapseAll={collapseAll}
        />

        <main className="dev-main" key={expandKey}>
          {visibleLanes.length === 0 ? (
            <section className="dev-panel">
              <div className="dev-empty">No lanes match the active filters{searchQuery ? ` or search "${searchQuery}"` : ''}.</div>
            </section>
          ) : (
            visibleLanes.map((lane) => (
              <FlowLane key={lane.id} lane={lane} filters={filters} searchQuery={searchQuery} />
            ))
          )}

          {filters.raw && (
            <section className="dev-panel">
              <div className="dev-section-header">
                <div>
                  <h3>Raw Payloads</h3>
                  <span className="dev-section-subtitle">Original audit data for debugging</span>
                </div>
              </div>
              <JsonPanel title="Audit stream" value={data.audit_stream || []} summary="Normalized events" />
              <JsonPanel title="Agent runs" value={data.agent_runs || []} summary="Trace tree" />
              <JsonPanel title="Raw audit events" value={data.audit_events || []} summary="DB records" />
            </section>
          )}
        </main>
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Link, useSearchParams, useNavigate } from 'react-router-dom'
import { compareAutomationRuns, getAutomationScenario } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './AutomationWorkspace.css'

function DiffTable({ diffs }) {
  if (!diffs || diffs.length === 0) {
    return <div className="automation-empty">No differences detected.</div>
  }

  const formatVal = (val) => {
    if (val === null || val === undefined) {
      return <span className="diff-val-nochange">—</span>
    }
    if (typeof val === 'object') {
      return (
        <pre className="llm-json-box" style={{ margin: 0, padding: '6px 10px', fontSize: '0.8rem', maxHeight: '180px' }}>
          {JSON.stringify(val, null, 2)}
        </pre>
      )
    }
    return String(val)
  }

  return (
    <table className="diff-table">
      <thead>
        <tr>
          <th style={{ width: '25%' }}>Path / Element</th>
          <th style={{ width: '37.5%' }}>Left Run Value</th>
          <th style={{ width: '37.5%' }}>Right Run Value</th>
        </tr>
      </thead>
      <tbody>
        {diffs.map((diff, idx) => (
          <tr key={idx}>
            <td>
              <code style={{ fontSize: '0.8rem', wordBreak: 'break-all' }}>{diff.path}</code>
            </td>
            <td>
              {diff.left !== undefined && diff.left !== null ? (
                <div className="diff-val-removed">{formatVal(diff.left)}</div>
              ) : (
                <span className="diff-val-nochange">(Added on right)</span>
              )}
            </td>
            <td>
              {diff.right !== undefined && diff.right !== null ? (
                <div className="diff-val-added">{formatVal(diff.right)}</div>
              ) : (
                <span className="diff-val-nochange">(Removed on right)</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function AutomationComparePage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const leftRunId = searchParams.get('left')
  const rightRunId = searchParams.get('right')
  const [data, setData] = useState(null)
  const [scenarioRuns, setScenarioRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const load = async () => {
      if (!leftRunId || !rightRunId) {
        setError('Both left and right run ids are required.')
        setLoading(false)
        return
      }
      try {
        const payload = await compareAutomationRuns({
          left_run_id: Number(leftRunId),
          right_run_id: Number(rightRunId),
        })
        setData(payload)
        setError('')

        // Fetch other scenario runs for switching dropdowns
        const scenarioId = payload.left_run?.scenario_id
        if (scenarioId) {
          const scenarioData = await getAutomationScenario(scenarioId)
          setScenarioRuns(scenarioData.runs || [])
        }
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [leftRunId, rightRunId])

  const handleLeftChange = (e) => {
    const val = e.target.value
    if (val) {
      navigate(`/automation/compare?left=${val}&right=${rightRunId}`)
    }
  }

  const handleRightChange = (e) => {
    const val = e.target.value
    if (val) {
      navigate(`/automation/compare?left=${leftRunId}&right=${val}`)
    }
  }

  if (loading) return <Loading message="Comparing runs..." />
  if (!data) return <ErrorMessage message={error || 'Comparison unavailable.'} />

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <Link className="automation-back-link" to={data.left_run?.scenario_id ? `/automation/scenarios/${data.left_run.scenario_id}` : '/automation'}>← Back to scenario</Link>
          <h1 className="automation-title">Run Comparison</h1>
          <p className="automation-subtitle">Left run #{data.left_run?.id} compared with right run #{data.right_run?.id}</p>
        </div>
      </div>

      {scenarioRuns.length > 0 && (
        <div className="compare-selects-row">
          <div className="compare-select-group">
            <label htmlFor="left-run-select">Left Run:</label>
            <select
              id="left-run-select"
              value={leftRunId || ''}
              onChange={handleLeftChange}
            >
              {scenarioRuns.map((r) => (
                <option key={r.id} value={r.id}>
                  Run #{r.id} ({r.status}) {r.matrix_label ? `· ${r.matrix_label}` : ''} · {r.created_at?.slice(0, 16).replace('T', ' ')}
                </option>
              ))}
            </select>
          </div>

          <div className="compare-select-vs">VS</div>

          <div className="compare-select-group">
            <label htmlFor="right-run-select">Right Run:</label>
            <select
              id="right-run-select"
              value={rightRunId || ''}
              onChange={handleRightChange}
            >
              {scenarioRuns.map((r) => (
                <option key={r.id} value={r.id}>
                  Run #{r.id} ({r.status}) {r.matrix_label ? `· ${r.matrix_label}` : ''} · {r.created_at?.slice(0, 16).replace('T', ' ')}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      <section className="automation-panel">
        <div className="automation-compare-header">
          <div>
            <strong>Left</strong>
            <span>Run #{data.left_run?.id} · {data.left_run?.status}</span>
          </div>
          <div>
            <strong>Right</strong>
            <span>Run #{data.right_run?.id} · {data.right_run?.status}</span>
          </div>
        </div>
        <div className="automation-compare-table">
          {data.comparisons.map((comparison) => (
            <div key={comparison.check_id} className="automation-compare-row">
              <div>
                <strong>{comparison.check_id}</strong>
              </div>
              <div>
                <span className={`status-${comparison.left?.status || 'missing'}`}>{comparison.left?.status || 'missing'}</span>
                <small>{comparison.left?.summary || 'No result'}</small>
              </div>
              <div>
                <span className={`status-${comparison.right?.status || 'missing'}`}>{comparison.right?.status || 'missing'}</span>
                <small>{comparison.right?.summary || 'No result'}</small>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="automation-panel">
        <h2>Transcript Diff</h2>
        <DiffTable diffs={data.transcript_diff} />
      </section>

      <section className="automation-panel">
        <h2>Audit Event Diff</h2>
        {data.audit_event_diff?.length ? (
          <div className="automation-scorecard">
            {data.audit_event_diff.map((diff) => (
              <div key={diff.event_type} className="automation-scorecard-row">
                <div>
                  <strong>{diff.event_type}</strong>
                  <span>Left {diff.left_count} · Right {diff.right_count}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="automation-empty">No audit event differences detected.</div>
        )}
      </section>

      <section className="automation-panel">
        <h2>World State Diff</h2>
        <DiffTable diffs={data.world_state_diff} />
      </section>

      <section className="automation-panel">
        <h2>Clock Diff</h2>
        <DiffTable diffs={data.clock_diff} />
      </section>

      <section className="automation-panel">
        <h2>Decision Trace Diff (AI Overseer Actions)</h2>
        <DiffTable diffs={data.decision_trace_diff} />
      </section>

      <ErrorMessage message={error} />
    </div>
  )
}

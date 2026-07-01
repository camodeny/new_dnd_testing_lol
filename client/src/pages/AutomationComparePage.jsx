import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { compareAutomationRuns } from '../api/client'
import Loading from '../components/common/Loading'
import ErrorMessage from '../components/common/ErrorMessage'
import './AutomationWorkspace.css'

export default function AutomationComparePage() {
  const [searchParams] = useSearchParams()
  const leftRunId = searchParams.get('left')
  const rightRunId = searchParams.get('right')
  const [data, setData] = useState(null)
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
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [leftRunId, rightRunId])

  if (loading) return <Loading message="Comparing runs..." />
  if (!data) return <ErrorMessage message={error || 'Comparison unavailable.'} />

  return (
    <div className="automation-page">
      <div className="automation-header">
        <div>
          <Link className="automation-back-link" to="/automation">← Back to automation</Link>
          <h1 className="automation-title">Run Comparison</h1>
          <p className="automation-subtitle">Left run #{data.left_run?.id} compared with right run #{data.right_run?.id}</p>
        </div>
      </div>

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
                <span>{comparison.left?.status || 'missing'}</span>
                <small>{comparison.left?.summary || 'No result'}</small>
              </div>
              <div>
                <span>{comparison.right?.status || 'missing'}</span>
                <small>{comparison.right?.summary || 'No result'}</small>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="automation-panel">
        <h2>Transcript Diff</h2>
        {data.transcript_diff?.length ? (
          <div className="automation-events">
            {data.transcript_diff.map((diff) => (
              <details key={diff.path} className="automation-event">
                <summary>
                  <strong>{diff.path}</strong>
                </summary>
                <pre>{JSON.stringify(diff, null, 2)}</pre>
              </details>
            ))}
          </div>
        ) : (
          <div className="automation-empty">No transcript differences detected.</div>
        )}
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
        <h2>World / Clock Diff</h2>
        <div className="automation-events">
          {(data.world_state_diff || []).map((diff) => (
            <details key={`world-${diff.path}`} className="automation-event">
              <summary>
                <strong>World</strong>
                <span>{diff.path}</span>
              </summary>
              <pre>{JSON.stringify(diff, null, 2)}</pre>
            </details>
          ))}
          {(data.clock_diff || []).map((diff) => (
            <details key={`clock-${diff.path}`} className="automation-event">
              <summary>
                <strong>Clock</strong>
                <span>{diff.path}</span>
              </summary>
              <pre>{JSON.stringify(diff, null, 2)}</pre>
            </details>
          ))}
          {!(data.world_state_diff?.length || data.clock_diff?.length) && (
            <div className="automation-empty">No world or clock differences detected.</div>
          )}
        </div>
      </section>

      <section className="automation-panel">
        <h2>Decision Trace Diff</h2>
        {data.decision_trace_diff?.length ? (
          <div className="automation-events">
            {data.decision_trace_diff.map((diff) => (
              <details key={`trace-${diff.path}`} className="automation-event">
                <summary>
                  <strong>{diff.path}</strong>
                </summary>
                <pre>{JSON.stringify(diff, null, 2)}</pre>
              </details>
            ))}
          </div>
        ) : (
          <div className="automation-empty">No decision trace differences detected.</div>
        )}
      </section>

      <ErrorMessage message={error} />
    </div>
  )
}

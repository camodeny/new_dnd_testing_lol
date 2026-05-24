import { useEffect, useMemo, useState } from 'react'
import { generateCampaignWorld, getCampaignWorld } from '../../api/client'

const BUILD_STEPS = [
  { icon: 'bi-people-fill', label: 'Reading party backstories' },
  { icon: 'bi-map-fill', label: 'Sketching the starting region' },
  { icon: 'bi-diagram-3-fill', label: 'Linking hooks and relationships' },
  { icon: 'bi-person-badge-fill', label: 'Preparing NPC actors' },
  { icon: 'bi-clock-history', label: 'Setting clocks and pressures' },
  { icon: 'bi-stars', label: 'Writing the opening scene' },
]

function getToneList(intro) {
  return Array.isArray(intro?.campaign_tone) ? intro.campaign_tone.filter(Boolean) : []
}

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

export default function WorldBuildingMode({ campaign, onBegin, onBack }) {
  const [world, setWorld] = useState(null)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState('')
  const [activeStep, setActiveStep] = useState(0)
  const [beginning, setBeginning] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function waitForGeneratedWorld() {
      while (!cancelled) {
        const latest = await getCampaignWorld(campaign.id)
        if (cancelled) return null
        if (latest.world?.public_intro) return latest.world
        await wait(3000)
      }
      return null
    }

    async function loadOrBuildWorld() {
      setStatus('loading')
      setError('')
      try {
        const existing = await getCampaignWorld(campaign.id)
        if (cancelled) return
        if (existing.world?.public_intro) {
          setWorld(existing.world)
          setStatus('ready')
          return
        }
        if (existing.generation_in_progress) {
          setStatus('building')
          const generatedWorld = await waitForGeneratedWorld()
          if (cancelled || !generatedWorld) return
          setWorld(generatedWorld)
          setStatus('ready')
          return
        }
        if (!existing.can_generate) {
          setError('The party must finish character planning before the world can be built.')
          setStatus('error')
          return
        }

        setStatus('building')
        const generated = await generateCampaignWorld(campaign.id)
        if (cancelled) return
        setWorld(generated.world)
        setStatus('ready')
      } catch (err) {
        if (cancelled) return
        if (err.status === 409 || err.data?.generation_in_progress) {
          setStatus('building')
          try {
            const generatedWorld = await waitForGeneratedWorld()
            if (cancelled || !generatedWorld) return
            setWorld(generatedWorld)
            setStatus('ready')
            return
          } catch (pollErr) {
            if (cancelled) return
            setError(pollErr.message || 'The DM could not build the world.')
            setStatus('error')
            return
          }
        }
        setError(err.message || 'The DM could not build the world.')
        setStatus('error')
      }
    }

    loadOrBuildWorld()

    return () => {
      cancelled = true
    }
  }, [campaign.id])

  useEffect(() => {
    if (status !== 'building' && status !== 'loading') return undefined
    const interval = window.setInterval(() => {
      setActiveStep((current) => (current + 1) % BUILD_STEPS.length)
    }, 1200)
    return () => window.clearInterval(interval)
  }, [status])

  const intro = useMemo(() => world?.public_intro || {}, [world])
  const tones = useMemo(() => getToneList(intro), [intro])

  const handleRetry = async () => {
    setWorld(null)
    setStatus('building')
    setError('')
    try {
      const generated = await generateCampaignWorld(campaign.id)
      setWorld(generated.world)
      setStatus('ready')
    } catch (err) {
      if (err.status === 409 || err.data?.generation_in_progress) {
        try {
          let latest = await getCampaignWorld(campaign.id)
          while (!latest.world?.public_intro) {
            await wait(3000)
            latest = await getCampaignWorld(campaign.id)
          }
          setWorld(latest.world)
          setStatus('ready')
          return
        } catch (pollErr) {
          setError(pollErr.message || 'The DM could not build the world.')
          setStatus('error')
          return
        }
      }
      setError(err.message || 'The DM could not build the world.')
      setStatus('error')
    }
  }

  const handleBegin = async () => {
    setBeginning(true)
    try {
      await onBegin()
    } finally {
      setBeginning(false)
    }
  }

  return (
    <div className="world-build-page">
      <header className="world-build-header">
        <button className="dashboard-back" onClick={onBack} title="Back to campaigns">
          <i className="bi bi-arrow-left"></i>
        </button>
        <div>
          <h1>{campaign.name}</h1>
          <p>World building</p>
        </div>
      </header>

      <main className="world-build-shell">
        <section className="world-build-stage">
          <div className="world-build-orbit" aria-hidden="true">
            {BUILD_STEPS.map((step, index) => (
              <span
                key={step.label}
                className={`world-build-node ${index === activeStep ? 'active' : ''} ${status === 'ready' ? 'complete' : ''}`}
                style={{ '--node-index': index }}
              >
                <i className={`bi ${step.icon}`}></i>
              </span>
            ))}
          </div>

          <div className="world-build-copy">
            {status === 'ready' ? (
              <>
                <span className="world-build-kicker">Campaign pitch</span>
                <h2>{intro.title || campaign.name}</h2>
                <p>{intro.elevator_pitch}</p>
              </>
            ) : status === 'error' ? (
              <>
                <span className="world-build-kicker error">Needs attention</span>
                <h2>World building paused</h2>
                <p>{error}</p>
              </>
            ) : (
              <>
                <span className="world-build-kicker">Assembling campaign memory</span>
                <h2>The DM is building the world.</h2>
                <p>The party-facing pitch will appear here when the private world package is ready.</p>
              </>
            )}
          </div>
        </section>

        <aside className="world-build-panel">
          {status === 'ready' ? (
            <>
              <div className="world-build-detail">
                <span>Starting location</span>
                <strong>{intro.starting_location}</strong>
              </div>

              {tones.length > 0 && (
                <div className="world-build-tones">
                  {tones.map((tone) => (
                    <span key={tone}>{tone}</span>
                  ))}
                </div>
              )}

              <div className="world-build-hook">
                <span>Party hook</span>
                <p>{intro.party_hook}</p>
              </div>

              <button className="btn btn-primary world-build-begin" onClick={handleBegin} disabled={beginning}>
                <i className="bi bi-play-fill"></i>
                {beginning ? 'Opening scene...' : 'Begin Adventure'}
              </button>
            </>
          ) : (
            <>
              <div className="world-build-progress-list">
                {BUILD_STEPS.map((step, index) => (
                  <div
                    key={step.label}
                    className={`world-build-progress-row ${index === activeStep ? 'active' : ''} ${index < activeStep ? 'complete' : ''}`}
                  >
                    <i className={`bi ${step.icon}`}></i>
                    <span>{step.label}</span>
                  </div>
                ))}
              </div>

              {status === 'error' && (
                <button className="btn btn-secondary world-build-retry" onClick={handleRetry}>
                  <i className="bi bi-arrow-clockwise"></i>
                  Retry
                </button>
              )}
            </>
          )}
        </aside>
      </main>
    </div>
  )
}

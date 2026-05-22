import { useEffect, useMemo, useRef, useState } from 'react'
import { getEncounterMapImage, getEncounterMapLabeledImage, moveEncounterMapToken } from '../../api/client'

// Convert row/col to chess-style algebraic coordinates, e.g. A-1, B-5, etc.
function getGridCoordinate(col, row) {
  if (typeof col !== 'number' || typeof row !== 'number') return ''
  const letter = String.fromCharCode(65 + (col % 26))
  const prefix = col >= 26 ? String.fromCharCode(65 + Math.floor(col / 26) - 1) : ''
  return `${prefix}${letter}-${row + 1}`
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max)
}

function getMovementSquares(character) {
  const speed = Number(character?.combat?.speed)
  if (!Number.isFinite(speed) || speed <= 0) return 0
  return Math.floor(speed / 5)
}

function getGridMoveDistance(fromCol, fromRow, toCol, toRow) {
  return Math.max(Math.abs(toCol - fromCol), Math.abs(toRow - fromRow))
}

function clampDestinationToMovement(fromCol, fromRow, toCol, toRow, maxSquares, columns, rows) {
  const limitedCol = fromCol + clamp(toCol - fromCol, -maxSquares, maxSquares)
  const limitedRow = fromRow + clamp(toRow - fromRow, -maxSquares, maxSquares)
  return {
    col: clamp(limitedCol, 0, columns - 1),
    row: clamp(limitedRow, 0, rows - 1),
  }
}

export default function EncounterMapPanel({
  encounterMap,
  loading,
  isOwner,
  currentUser,
  currentCharacter,
  onEncounterMapChange,
  isMapExpanded,
  setIsMapExpanded,
}) {
  const [imageState, setImageState] = useState({ mapId: null, url: '', error: '' })
  const [labeledImageState, setLabeledImageState] = useState({ mapId: null, url: '', error: '' })
  const tokenLayerRef = useRef(null)
  
  // UX UI states
  const isCollapsed = !isMapExpanded
  const [showGridLines, setShowGridLines] = useState(true)
  const [isRosterOpen, setIsRosterOpen] = useState(true)
  const [showLabeledImage, setShowLabeledImage] = useState(false)
  const [activeHoverId, setActiveHoverId] = useState(null)
  const [hasNotification, setHasNotification] = useState(false)
  const [prevMapId, setPrevMapId] = useState(encounterMap?.id || null)
  const [aspectRatio, setAspectRatio] = useState(null)
  const [imageDimensions, setImageDimensions] = useState({ width: 0, height: 0 })
  const [dragState, setDragState] = useState(null)
  const [pendingPlacementOverrides, setPendingPlacementOverrides] = useState({})
  const [moveError, setMoveError] = useState('')
  const [movementMessage, setMovementMessage] = useState('')
  const [isMovingToken, setIsMovingToken] = useState(false)

  // Adjust state when map ID changes (during render phase to avoid effect cascades)
  const currentMapId = encounterMap?.id || null
  if (currentMapId !== prevMapId) {
    setPrevMapId(currentMapId)
    setAspectRatio(null)
    setImageDimensions({ width: 0, height: 0 })
    setDragState(null)
    setPendingPlacementOverrides({})
    setMoveError('')
    setMovementMessage('')
    setIsMovingToken(false)
    if (isCollapsed) {
      setHasNotification(true)
    }
  }

  const hasDebugImage = Boolean(encounterMap?.labeled_image_url)
  const grid = useMemo(() => encounterMap?.grid || {}, [encounterMap?.grid])
  const columns = Number.isInteger(grid.columns) ? grid.columns : null
  const rows = Number.isInteger(grid.rows) ? grid.rows : null
  const canOverlayPlacements = Boolean(columns && rows)
  const placements = useMemo(() => encounterMap?.placements || [], [encounterMap?.placements])
  const currentUserActorId = currentUser?.id != null ? String(currentUser.id) : ''
  const movementSquares = getMovementSquares(currentCharacter)
  const playerPlacement = useMemo(
    () => placements.find((placement) => (
      placement.actor_type === 'player' && String(placement.actor_id) === currentUserActorId
    )),
    [placements, currentUserActorId],
  )
  const canMovePlayerToken = Boolean(
    encounterMap?.id &&
    canOverlayPlacements &&
    playerPlacement &&
    currentCharacter
  )
  const canDragPlayerToken = canMovePlayerToken && !isMovingToken
  const displayPlacements = useMemo(() => (
    placements.map((placement) => {
      if (dragState?.placementId === placement.id) {
        return { ...placement, col: dragState.col, row: dragState.row }
      }
      const pending = pendingPlacementOverrides[placement.id]
      if (pending) {
        return { ...placement, ...pending }
      }
      return placement
    })
  ), [placements, dragState, pendingPlacementOverrides])
  
  const gridLayout = useMemo(() => {
    if (!grid || !grid.origin_px || !grid.cell_size_px || !imageDimensions.width || !imageDimensions.height) {
      return {
        left: '0%',
        top: '0%',
        width: '100%',
        height: '100%',
        right: 'auto',
        bottom: 'auto'
      }
    }

    const { width: imgW, height: imgH } = imageDimensions
    const originX = typeof grid.origin_px.x === 'number' ? grid.origin_px.x : 0
    const originY = typeof grid.origin_px.y === 'number' ? grid.origin_px.y : 0
    
    // Support cell_size_px as object {x, y} or number, falling back to average
    let cellW = 0
    let cellH = 0
    if (typeof grid.cell_size_px === 'object' && grid.cell_size_px !== null) {
      cellW = typeof grid.cell_size_px.x === 'number' ? grid.cell_size_px.x : (grid.cell_size_px.average || 0)
      cellH = typeof grid.cell_size_px.y === 'number' ? grid.cell_size_px.y : (grid.cell_size_px.average || 0)
    } else if (typeof grid.cell_size_px === 'number') {
      cellW = grid.cell_size_px
      cellH = grid.cell_size_px
    }

    const cols = columns || 0
    const rws = rows || 0

    if (!cellW || !cellH || !cols || !rws) {
      return {
        left: '0%',
        top: '0%',
        width: '100%',
        height: '100%',
        right: 'auto',
        bottom: 'auto'
      }
    }

    const left = (originX / imgW) * 100
    const top = (originY / imgH) * 100
    const width = ((cols * cellW) / imgW) * 100
    const height = ((rws * cellH) / imgH) * 100

    return {
      left: `${left}%`,
      top: `${top}%`,
      width: `${width}%`,
      height: `${height}%`,
      right: 'auto',
      bottom: 'auto'
    }
  }, [grid, columns, rows, imageDimensions])
  
  const imageUrl = imageState.mapId === encounterMap?.id ? imageState.url : ''
  const labeledImageUrl = labeledImageState.mapId === encounterMap?.id ? labeledImageState.url : ''
  const imageError = imageState.mapId === encounterMap?.id ? imageState.error : ''
  const labeledImageError = labeledImageState.mapId === encounterMap?.id ? labeledImageState.error : ''

  // Group placements by actor type
  const groupedPlacements = useMemo(() => {
    const groups = { player: [], npc: [], monster: [] }
    displayPlacements.forEach((p) => {
      if (groups[p.actor_type]) {
        groups[p.actor_type].push(p)
      }
    })
    return groups
  }, [displayPlacements])

  // Sync state changes with localStorage
  const handleToggleCollapse = () => {
    setIsMapExpanded((prev) => {
      const next = !prev
      localStorage.setItem('encounter_map_collapsed', String(!next))
      if (next) {
        setHasNotification(false) // clear notification once expanded
      }
      return next
    })
  }

  const handleExpand = () => {
    setIsMapExpanded(true)
    localStorage.setItem('encounter_map_collapsed', 'false')
    setHasNotification(false)
  }

  const handleImageLoad = (e) => {
    const { naturalWidth, naturalHeight } = e.target
    if (naturalWidth && naturalHeight) {
      setAspectRatio(naturalWidth / naturalHeight)
      setImageDimensions({ width: naturalWidth, height: naturalHeight })
    }
  }

  // Load images
  useEffect(() => {
    let cancelled = false
    const objectUrls = []

    if (!encounterMap?.id) {
      return () => {}
    }

    getEncounterMapImage(encounterMap.id)
      .then((blob) => {
        if (cancelled) return
        const objectUrl = URL.createObjectURL(blob)
        objectUrls.push(objectUrl)
        setImageState({ mapId: encounterMap.id, url: objectUrl, error: '' })
      })
      .catch((err) => {
        if (!cancelled) setImageState({ mapId: encounterMap.id, url: '', error: err.message })
      })

    if (encounterMap.labeled_image_url) {
      getEncounterMapLabeledImage(encounterMap.id)
        .then((blob) => {
          if (cancelled) return
          const objectUrl = URL.createObjectURL(blob)
          objectUrls.push(objectUrl)
          setLabeledImageState({ mapId: encounterMap.id, url: objectUrl, error: '' })
        })
        .catch((err) => {
          if (!cancelled) setLabeledImageState({ mapId: encounterMap.id, url: '', error: err.message })
        })
    }

    return () => {
      cancelled = true
      objectUrls.forEach((objectUrl) => URL.revokeObjectURL(objectUrl))
    }
  }, [encounterMap?.id, encounterMap?.labeled_image_url])

  const getGridCellFromPointer = (event) => {
    const layer = tokenLayerRef.current
    if (!layer || !columns || !rows) return null
    const rect = layer.getBoundingClientRect()
    if (!rect.width || !rect.height) return null

    const x = clamp((event.clientX - rect.left) / rect.width, 0, 0.999999)
    const y = clamp((event.clientY - rect.top) / rect.height, 0, 0.999999)
    return {
      col: clamp(Math.floor(x * columns), 0, columns - 1),
      row: clamp(Math.floor(y * rows), 0, rows - 1),
    }
  }

  const getLimitedDestinationFromPointer = (event, state) => {
    const cell = getGridCellFromPointer(event)
    if (!cell || !state || !columns || !rows) return null
    return clampDestinationToMovement(
      state.fromCol,
      state.fromRow,
      cell.col,
      cell.row,
      state.maxSquares,
      columns,
      rows,
    )
  }

  const canDragPlacement = (placement) => (
    canDragPlayerToken &&
    placement.actor_type === 'player' &&
    String(placement.actor_id) === currentUserActorId
  )

  const handleTokenPointerDown = (event, placement) => {
    if (!canDragPlacement(placement)) return

    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture?.(event.pointerId)
    setActiveHoverId(placement.id)
    setMoveError('')
    setMovementMessage('')
    setDragState({
      placementId: placement.id,
      pointerId: event.pointerId,
      fromCol: placement.col,
      fromRow: placement.row,
      col: placement.col,
      row: placement.row,
      maxSquares: movementSquares,
      distance: 0,
    })
  }

  const handleTokenPointerMove = (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId) return

    event.preventDefault()
    const destination = getLimitedDestinationFromPointer(event, dragState)
    if (!destination) return

    setDragState((current) => {
      if (!current || current.pointerId !== event.pointerId) return current
      return {
        ...current,
        ...destination,
        distance: getGridMoveDistance(current.fromCol, current.fromRow, destination.col, destination.row),
      }
    })
  }

  const handleTokenPointerUp = async (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId || !encounterMap?.id) return

    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.releasePointerCapture?.(event.pointerId)

    const destination = getLimitedDestinationFromPointer(event, dragState) || {
      col: dragState.col,
      row: dragState.row,
    }
    const distance = getGridMoveDistance(dragState.fromCol, dragState.fromRow, destination.col, destination.row)
    const placementId = dragState.placementId
    setDragState(null)

    if (!distance) {
      setActiveHoverId(null)
      return
    }

    setPendingPlacementOverrides((current) => ({
      ...current,
      [placementId]: destination,
    }))
    setIsMovingToken(true)

    try {
      const data = await moveEncounterMapToken(encounterMap.id, destination.col, destination.row)
      onEncounterMapChange?.(data.encounter_map)
      setPendingPlacementOverrides((current) => {
        const next = { ...current }
        delete next[placementId]
        return next
      })
      setMovementMessage(`Moved ${data.movement?.moved_squares ?? distance}/${data.movement?.max_squares ?? movementSquares} sq to ${getGridCoordinate(destination.col, destination.row)}.`)
      setMoveError('')
    } catch (err) {
      setPendingPlacementOverrides((current) => {
        const next = { ...current }
        delete next[placementId]
        return next
      })
      setMoveError(err.message)
    } finally {
      setIsMovingToken(false)
      setActiveHoverId(null)
    }
  }

  const handleTokenPointerCancel = (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId) return
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    setDragState(null)
    setActiveHoverId(null)
  }

  // Helper renderer for lists
  const renderCombatantCard = (placement) => {
    const isHighlighted = activeHoverId === placement.id
    return (
      <div
        key={placement.id}
        className={`encounter-combatant-card ${placement.actor_type} ${isHighlighted ? 'highlighted' : ''}`}
        onMouseEnter={() => setActiveHoverId(placement.id)}
        onMouseLeave={() => setActiveHoverId(null)}
      >
        <span className="combatant-avatar">
          {placement.label?.slice(0, 2).toUpperCase() || '?'}
        </span>
        <div className="combatant-info">
          <strong className="combatant-name">{placement.label}</strong>
          <span className="combatant-meta">Actor ID: {placement.actor_id}</span>
        </div>
        <span className="combatant-coord-badge">
          {getGridCoordinate(placement.col, placement.row)}
        </span>
      </div>
    )
  }

  // --- COLLAPSED VIEW RENDER ---
  if (isCollapsed) {
    return (
      <section className={`encounter-map-panel collapsed ${hasNotification ? 'has-notification' : ''}`} aria-label="Encounter map header">
        <div className="encounter-map-collapsed-bar" onClick={handleExpand}>
          <div className="encounter-map-collapsed-info">
            <div className="encounter-map-kicker">
              <i className="bi bi-map-fill"></i>
              <span>Tactical Map</span>
            </div>
            <h2 className="encounter-map-collapsed-title">
              {encounterMap?.title || 'No active map'}
            </h2>
            {encounterMap && placements.length > 0 && (
              <span className="encounter-map-status-badge">
                ⚔️ {placements.length} Combatant{placements.length !== 1 ? 's' : ''} Active
              </span>
            )}
          </div>
          <div className="encounter-map-collapsed-actions" onClick={(e) => e.stopPropagation()}>
            {encounterMap && (
              <button
                className={`btn-icon-map ${showGridLines ? 'active' : ''}`}
                onClick={() => setShowGridLines(!showGridLines)}
                title="Toggle Grid Lines"
                aria-label="Toggle grid lines"
              >
                <i className="bi bi-grid-3x3"></i>
              </button>
            )}
            <button className="btn btn-primary btn-sm btn-map-expand" onClick={handleExpand}>
              <i className="bi bi-arrows-angle-expand"></i> View Map
            </button>
          </div>
        </div>
      </section>
    )
  }

  // --- EXPANDED VIEW RENDER ---
  return (
    <section className="encounter-map-panel expanded" aria-label="Encounter map VTT">
      <div className="encounter-map-toolbar">
        <div>
          <div className="encounter-map-kicker">
            <i className="bi bi-map-fill"></i>
            Tactical Map
          </div>
          <h2>{encounterMap?.title || 'No active map'}</h2>
        </div>
        <div className="encounter-map-actions">
          {encounterMap && (
            <>
              <button
                className={`btn-icon-map ${showGridLines ? 'active' : ''}`}
                onClick={() => setShowGridLines(!showGridLines)}
                title="Toggle Grid Overlay"
                aria-label="Toggle grid overlay"
              >
                <i className="bi bi-grid-3x3"></i>
              </button>
              {placements.length > 0 && (
                <button
                  className={`btn-icon-map ${isRosterOpen ? 'active' : ''}`}
                  onClick={() => setIsRosterOpen(!isRosterOpen)}
                  title="Toggle Combat Roster"
                  aria-label="Toggle combat roster"
                >
                  <i className="bi bi-people-fill"></i>
                </button>
              )}
            </>
          )}
          <button className="btn btn-secondary btn-sm btn-map-collapse" onClick={handleToggleCollapse}>
            <i className="bi bi-arrows-angle-contract"></i> Collapse Map
          </button>
        </div>
      </div>

      <div className="encounter-map-body-layout">
        <div className={`encounter-map-stage ${!encounterMap ? 'empty' : ''}`}>
        {loading ? (
          <div className="encounter-map-placeholder">
            <i className="bi bi-hourglass-split"></i>
            <span>Loading Map State...</span>
          </div>
        ) : encounterMap && imageUrl ? (
          <div className="encounter-map-images">
            <figure className="encounter-map-frame">
              <div
                className="encounter-map-board"
                style={{
                  aspectRatio: aspectRatio ? `${aspectRatio}` : 'auto'
                }}
              >
                <img
                  src={imageUrl}
                  alt={encounterMap.title || 'Encounter map'}
                  className="encounter-map-image"
                  onLoad={handleImageLoad}
                />
                
                {/* CSS Dynamic Grid Lines Overlay */}
                {canOverlayPlacements && (
                  <div
                    className="encounter-map-grid-overlay"
                    style={{
                      '--grid-cols': columns,
                      '--grid-rows': rows,
                      display: showGridLines ? 'block' : 'none',
                      ...gridLayout,
                    }}
                  />
                )}

                {/* Tokens Layer */}
                {canOverlayPlacements && (
                  <div
                    ref={tokenLayerRef}
                    className="encounter-map-token-layer"
                    aria-label="Placed combatants"
                    style={gridLayout}
                  >
                    {displayPlacements.map((placement) => {
                      const isHighlighted = activeHoverId === placement.id
                      const isDraggable = canDragPlacement(placement)
                      const isDragging = dragState?.placementId === placement.id
                      return (
                        <div
                          key={placement.id}
                          className={`encounter-map-token ${placement.actor_type} ${isHighlighted ? 'highlighted' : ''} ${isDraggable ? 'draggable' : ''} ${isDragging ? 'dragging' : ''}`}
                          style={{
                            left: `${((placement.col + 0.5) / columns) * 100}%`,
                            top: `${((placement.row + 0.5) / rows) * 100}%`,
                          }}
                          onPointerDown={(event) => handleTokenPointerDown(event, placement)}
                          onPointerMove={handleTokenPointerMove}
                          onPointerUp={handleTokenPointerUp}
                          onPointerCancel={handleTokenPointerCancel}
                          onMouseEnter={() => setActiveHoverId(placement.id)}
                          onMouseLeave={() => setActiveHoverId(null)}
                        >
                          <span className="token-initials">
                            {placement.label?.slice(0, 2).toUpperCase() || '?'}
                          </span>
                          
                          {/* Miniature Tooltip on Hover */}
                          <div className="encounter-token-tooltip">
                            <span className="tooltip-name">{placement.label}</span>
                            <span className="tooltip-coord">
                              Coordinate: {getGridCoordinate(placement.col, placement.row)}
                            </span>
                            {isDraggable && (
                              <span className="tooltip-coord">
                                Move: {dragState?.placementId === placement.id ? dragState.distance : 0}/{movementSquares} sq
                              </span>
                            )}
                            <span className="tooltip-alliance">{placement.actor_type}</span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
                {canMovePlayerToken && (
                  <div className={`encounter-map-movement-hud ${moveError ? 'has-error' : ''}`}>
                    <i className={isMovingToken ? 'bi bi-arrow-repeat' : 'bi bi-arrows-move'}></i>
                    <span>
                      {dragState
                        ? `${dragState.distance}/${dragState.maxSquares} sq`
                        : isMovingToken
                          ? 'Saving move...'
                          : `Move ${movementSquares} sq`}
                    </span>
                    {(moveError || movementMessage) && (
                      <small>{moveError || movementMessage}</small>
                    )}
                  </div>
                )}
              </div>
            </figure>
          </div>
        ) : encounterMap && imageError ? (
          <div className="encounter-map-placeholder">
            <i className="bi bi-exclamation-triangle-fill"></i>
            <span>{imageError}</span>
          </div>
        ) : encounterMap ? (
          <div className="encounter-map-placeholder">
            <i className="bi bi-image"></i>
            <span>Downloading map visual...</span>
          </div>
        ) : (
          <div className="encounter-map-placeholder">
            <i className="bi bi-map"></i>
            <span>The AI Dungeon Master will generate a tactical map when positioning matters.</span>
          </div>
        )}
        </div>

        {/* Styled Combatants Roster */}
        {encounterMap && placements.length > 0 && isRosterOpen && (
          <div className="encounter-combatants-section">
            <h3>Combat Roster</h3>
            <div className="encounter-combatants-groups">
              {groupedPlacements.player.length > 0 && (
                <div className="combatant-group player">
                  <h4>🛡️ Party</h4>
                  <div className="combatant-list">
                    {groupedPlacements.player.map(renderCombatantCard)}
                  </div>
                </div>
              )}

              {groupedPlacements.npc.length > 0 && (
                <div className="combatant-group npc">
                  <h4>🤝 Allies</h4>
                  <div className="combatant-list">
                    {groupedPlacements.npc.map(renderCombatantCard)}
                  </div>
                </div>
              )}

              {groupedPlacements.monster.length > 0 && (
                <div className="combatant-group monster">
                  <h4>⚔️ Threats</h4>
                  <div className="combatant-list">
                    {groupedPlacements.monster.map(renderCombatantCard)}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Narrative & Diagnostics HUD Overlays (floating bottom-left) */}
        {(encounterMap?.vtt_setup?.map_summary || (isOwner && encounterMap)) && (
          <div className="encounter-vtt-overlays-bottom-left">
            {encounterMap?.vtt_setup?.map_summary && (
              <div className="encounter-narrative">
                <i className="bi bi-blockquote-left narrative-icon"></i>
                <p className="encounter-map-summary">{encounterMap.vtt_setup.map_summary}</p>
              </div>
            )}

            {isOwner && encounterMap && (
              <details className="encounter-calibration-details">
                <summary>
                  <i className="bi bi-sliders"></i> System Calibration & Grid Diagnostics
                </summary>
                <div className="calibration-content">
                  <div className="encounter-map-feedback">
                    <div>
                      <span>Grid Size</span>
                      <strong>{columns && rows ? `${columns} x ${rows}` : 'Unknown'}</strong>
                    </div>
                    <div>
                      <span>Cell Width</span>
                      <strong>{grid.cell_size_px?.average ? `${Math.round(grid.cell_size_px.average)} px` : 'Unknown'}</strong>
                    </div>
                    <div>
                      <span>Calibration Confidence</span>
                      <strong>{typeof grid.confidence === 'number' ? `${Math.round(grid.confidence * 100)}%` : 'Unknown'}</strong>
                    </div>
                    <div>
                      <span>Processing Status</span>
                      <strong>{encounterMap.setup_status || 'pending'}</strong>
                    </div>
                  </div>

                  {hasDebugImage && (
                    <div className="calibration-image-toggle">
                      <button
                        type="button"
                        className={`btn btn-secondary btn-sm btn-calibration-toggle ${showLabeledImage ? 'active' : ''}`}
                        onClick={() => setShowLabeledImage(!showLabeledImage)}
                      >
                        <i className="bi bi-eye"></i> {showLabeledImage ? 'Hide' : 'Show'} AI Calibration Labeled Image
                      </button>

                      {showLabeledImage && (
                        <figure className="encounter-map-frame calibration-frame">
                          <figcaption>AI Calibration Overlay</figcaption>
                          {labeledImageUrl ? (
                            <img src={labeledImageUrl} alt={`${encounterMap.title || 'Encounter map'} debug grid`} className="calibration-img" />
                          ) : labeledImageError ? (
                            <div className="encounter-map-placeholder">
                              <i className="bi bi-exclamation-triangle-fill"></i>
                              <span>{labeledImageError}</span>
                            </div>
                          ) : (
                            <div className="encounter-map-placeholder">
                              <i className="bi bi-image"></i>
                              <span>Loading calibration overlay...</span>
                            </div>
                          )}
                        </figure>
                      )}
                    </div>
                  )}
                </div>
              </details>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

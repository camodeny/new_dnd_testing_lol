import { useEffect, useMemo, useRef, useState } from 'react'
import {
  getEncounterMapImage,
  getEncounterMapLabeledImage,
  moveEncounterMapToken,
  rollPlayerInitiative,
  advanceEncounterTurn,
} from '../../api/client'

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

function pointInPolygon(points, x, y) {
  if (!Array.isArray(points) || points.length < 3) return false
  let inside = false
  let previous = points[points.length - 1]
  points.forEach((current) => {
    const x1 = Number(previous?.col)
    const y1 = Number(previous?.row)
    const x2 = Number(current?.col)
    const y2 = Number(current?.row)
    if ([x1, y1, x2, y2].every(Number.isFinite) && ((y1 > y) !== (y2 > y))) {
      const xIntersection = ((x2 - x1) * (y - y1)) / ((y2 - y1) || 1e-9) + x1
      if (x < xIntersection) inside = !inside
    }
    previous = current
  })
  return inside
}

function rectContainsCell(rect, col, row) {
  const rectCol = Number(rect?.col)
  const rectRow = Number(rect?.row)
  const width = Number(rect?.width)
  const height = Number(rect?.height)
  if (![rectCol, rectRow, width, height].every(Number.isFinite)) return false
  return rectCol <= col && col < rectCol + width && rectRow <= row && row < rectRow + height
}

function areaContainsCell(area, col, row) {
  if (Array.isArray(area?.polygon) && area.polygon.length >= 3) {
    return pointInPolygon(area.polygon, col + 0.5, row + 0.5)
  }
  return rectContainsCell(area?.rect, col, row)
}

function getCellMovementProfile(vttSetup, col, row) {
  const profile = { blocked: false, cost: 1 }
  const movementAreaGroups = ['terrain_zones', 'obstacles']
  movementAreaGroups.forEach((group) => {
    const areas = Array.isArray(vttSetup?.[group]) ? vttSetup[group] : []
    areas.forEach((area) => {
      if (!area || !areaContainsCell(area, col, row)) return

      const kind = String(area.kind || '').toLowerCase()
      const movementEffect = String(area.movement_effect || '').toLowerCase()
      if (kind === 'blocked' || kind === 'wall' || movementEffect === 'blocks_movement') {
        profile.blocked = true
      }
      if (kind === 'difficult' || kind === 'water' || movementEffect === 'costs_extra_movement') {
        profile.cost = Math.max(profile.cost, 2)
      }
    })
  })
  return profile
}

function buildMovementGrid(vttSetup, columns, rows) {
  return Array.from({ length: rows }, (_, row) => (
    Array.from({ length: columns }, (_, col) => getCellMovementProfile(vttSetup, col, row))
  ))
}

function getReachableMovementCells(vttSetup, columns, rows, fromCol, fromRow, maxSquares) {
  if (!columns || !rows || maxSquares < 0) return []
  const movementGrid = buildMovementGrid(vttSetup, columns, rows)
  const bestCosts = new Map([[`${fromCol},${fromRow}`, 0]])
  const queue = [{ col: fromCol, row: fromRow, cost: 0 }]
  const directions = [
    [-1, -1], [0, -1], [1, -1],
    [-1, 0], [1, 0],
    [-1, 1], [0, 1], [1, 1],
  ]

  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index]
    directions.forEach(([dc, dr]) => {
      const col = current.col + dc
      const row = current.row + dr
      if (col < 0 || row < 0 || col >= columns || row >= rows) return
      if (movementGrid[row][col].blocked) return
      if (dc && dr && (movementGrid[current.row][col].blocked || movementGrid[row][current.col].blocked)) return

      const cost = current.cost + movementGrid[row][col].cost
      const key = `${col},${row}`
      if (cost > maxSquares || cost >= (bestCosts.get(key) ?? Infinity)) return
      bestCosts.set(key, cost)
      queue.push({ col, row, cost })
    })
  }

  return Array.from(bestCosts, ([key, cost]) => {
    const [col, row] = key.split(',').map(Number)
    return {
      col,
      row,
      cost,
      isDifficult: movementGrid[row]?.[col]?.cost > 1,
    }
  })
}

function getNearestReachableCell(target, reachableCells) {
  if (!target || !reachableCells.length) return null
  return reachableCells.reduce((best, cell) => {
    const distance = getGridMoveDistance(target.col, target.row, cell.col, cell.row)
    const tiebreaker = Math.abs(target.col - cell.col) + Math.abs(target.row - cell.row)
    const score = distance * 1000 + tiebreaker * 10 + cell.cost
    if (!best || score < best.score) return { ...cell, score }
    return best
  }, null)
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
  onSendMessage,
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
  const [manualInitValues, setManualInitValues] = useState({})

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
    setManualInitValues({})
    if (isCollapsed) {
      setHasNotification(true)
    }
  }

  const encounterState = useMemo(() => {
    if (!encounterMap?.encounter_state_json) return null
    try {
      return typeof encounterMap.encounter_state_json === 'string'
        ? JSON.parse(encounterMap.encounter_state_json)
        : encounterMap.encounter_state_json
    } catch (err) {
      return null
    }
  }, [encounterMap?.encounter_state_json])

  const isEncounterActive = Boolean(encounterState?.active)
  const turnOrder = encounterState?.turn_order || []
  const activeTurnIndex = encounterState?.active_turn_index
  const activeCombatant = (activeTurnIndex !== null && activeTurnIndex !== undefined) ? turnOrder[activeTurnIndex] : null

  const placements = useMemo(() => encounterMap?.placements || [], [encounterMap?.placements])
  const currentUserActorId = currentUser?.id != null ? String(currentUser.id) : ''
  const playerPlacement = useMemo(
    () => placements.find((placement) => (
      placement.actor_type === 'player' && String(placement.actor_id) === currentUserActorId
    )),
    [placements, currentUserActorId],
  )
  const isUserActiveTurn = activeCombatant && activeCombatant.placement_id === playerPlacement?.id

  const handleRollInitiative = async (combatant) => {
    if (!encounterMap?.id) return
    const bonus = combatant.initiative_bonus || 0
    const d20 = Math.floor(Math.random() * 20) + 1
    const total = d20 + bonus
    try {
      const data = await rollPlayerInitiative(encounterMap.id, combatant.actor_type, combatant.actor_id, total)
      onEncounterMapChange?.(data.encounter_map)
      setMoveError('')
      if (onSendMessage) {
        await onSendMessage(`rolls initiative for ${combatant.label}: [Roll: Initiative] total: ${total} (rolled ${d20} + ${bonus})`)
      }
    } catch (err) {
      setMoveError(err.message)
    }
  }

  const handleManualInitiativeSubmit = async (combatant, val) => {
    if (!encounterMap?.id) return
    const initVal = parseInt(val, 10)
    if (isNaN(initVal)) return
    try {
      const data = await rollPlayerInitiative(encounterMap.id, combatant.actor_type, combatant.actor_id, initVal)
      onEncounterMapChange?.(data.encounter_map)
      setMoveError('')
      if (onSendMessage) {
        await onSendMessage(`sets initiative for ${combatant.label} to ${initVal}`)
      }
    } catch (err) {
      setMoveError(err.message)
    }
  }

  const handleNextTurn = async () => {
    if (!encounterMap?.id) return
    try {
      const data = await advanceEncounterTurn(encounterMap.id)
      onEncounterMapChange?.(data.encounter_map)
      setMoveError('')
    } catch (err) {
      setMoveError(err.message)
    }
  }

  const hasDebugImage = Boolean(encounterMap?.labeled_image_url)
  const grid = useMemo(() => encounterMap?.grid || {}, [encounterMap?.grid])
  const columns = Number.isInteger(grid.columns) ? grid.columns : null
  const rows = Number.isInteger(grid.rows) ? grid.rows : null
  const canOverlayPlacements = Boolean(columns && rows)
  const canMovePlayerToken = Boolean(
    encounterMap?.id &&
    canOverlayPlacements &&
    playerPlacement &&
    currentCharacter &&
    (!isEncounterActive || (
      encounterState?.active_turn_index !== null &&
      encounterState?.active_turn_index !== undefined &&
      encounterState?.turn_order?.[encounterState.active_turn_index]?.placement_id === playerPlacement.id
    ))
  )

  const movementSquares = useMemo(() => {
    if (!currentCharacter) return 0
    if (isEncounterActive) {
      const activeIdx = encounterState?.active_turn_index
      if (activeIdx !== null && activeIdx !== undefined) {
        const activeCbt = encounterState?.turn_order?.[activeIdx]
        if (activeCbt && activeCbt.placement_id === playerPlacement?.id) {
          const remaining = activeCbt.actions?.movement_remaining ?? activeCbt.speed ?? 30
          return Math.floor(remaining / 5)
        }
      }
      return 0
    }
    const speed = Number(currentCharacter.speed || currentCharacter.combat?.speed)
    return Number.isFinite(speed) && speed > 0 ? Math.floor(speed / 5) : 6
  }, [currentCharacter, isEncounterActive, encounterState, playerPlacement])

  const canDragPlayerToken = canMovePlayerToken && !isMovingToken
  const reachableMovementCells = useMemo(() => {
    if (!canMovePlayerToken || !columns || !rows || !playerPlacement) return []
    return getReachableMovementCells(
      encounterMap?.vtt_setup,
      columns,
      rows,
      playerPlacement.col,
      playerPlacement.row,
      movementSquares,
    )
  }, [canMovePlayerToken, columns, rows, playerPlacement, encounterMap?.vtt_setup, movementSquares])
  const reachableMovementCellMap = useMemo(() => {
    const map = new Map()
    reachableMovementCells.forEach((cell) => {
      map.set(`${cell.col},${cell.row}`, cell)
    })
    return map
  }, [reachableMovementCells])
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
    if (!cell || !state) return null
    const reachableCell = reachableMovementCellMap.get(`${cell.col},${cell.row}`)
      || getNearestReachableCell(cell, reachableMovementCells)
    if (!reachableCell) return null
    return {
      col: reachableCell.col,
      row: reachableCell.row,
      cost: reachableCell.cost,
    }
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
      cost: 0,
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
        cost: destination.cost ?? getGridMoveDistance(current.fromCol, current.fromRow, destination.col, destination.row),
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
      cost: dragState.cost,
    }
    const distance = destination.cost ?? getGridMoveDistance(dragState.fromCol, dragState.fromRow, destination.col, destination.row)
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

      {isEncounterActive && (
        <div className="encounter-combat-tracker">
          <div className="encounter-combat-tracker-left">
            <span className="encounter-combat-tracker-round">
              Round {encounterState?.round ?? 1}
            </span>
          </div>

          <div className="encounter-tracker-list-container">
            <div className="encounter-tracker-list">
              {turnOrder.map((combatant, idx) => {
                const canRollOrInput = combatant.actor_type === 'player' && String(combatant.actor_id) === currentUserActorId
                const hasInitiative = combatant.initiative !== null && combatant.initiative !== undefined
                const isActiveItem = idx === activeTurnIndex

                return (
                  <div
                    key={`${combatant.actor_type}-${combatant.actor_id}-${combatant.placement_id || idx}`}
                    className={`encounter-tracker-item ${combatant.actor_type} ${isActiveItem ? 'active' : ''}`}
                  >
                    <span className="tracker-avatar">
                      {combatant.label?.slice(0, 2).toUpperCase() || '?'}
                    </span>
                    <span className="tracker-label">{combatant.label}</span>
                    
                    {hasInitiative ? (
                      <span className="tracker-init-badge">{combatant.initiative}</span>
                    ) : canRollOrInput ? (
                      <div className="tracker-roll-action">
                        <button
                          type="button"
                          className="btn btn-primary btn-roll-init"
                          onClick={(e) => {
                            e.stopPropagation()
                            handleRollInitiative(combatant)
                          }}
                        >
                          🎲 Roll
                        </button>
                        <input
                          type="number"
                          className="initiative-manual-input"
                          placeholder="or type..."
                          value={manualInitValues[`${combatant.actor_type}-${combatant.actor_id}`] ?? ''}
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) => {
                            const val = e.target.value
                            setManualInitValues(prev => ({
                              ...prev,
                              [`${combatant.actor_type}-${combatant.actor_id}`]: val
                            }))
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              handleManualInitiativeSubmit(combatant, e.target.value)
                            }
                          }}
                        />
                      </div>
                    ) : (
                      <span className="tracker-init-badge" title="Waiting for initiative roll">⏳</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div className="encounter-combat-tracker-right">
            {activeCombatant ? (
              <div className="active-combatant-details">
                <span className="active-combatant-name" title={activeCombatant.label}>
                  ⚔️ {activeCombatant.label}
                </span>
                <span
                  className={`action-pill ${activeCombatant.actions?.action ? 'active action' : ''}`}
                  title="Action"
                >
                  A
                </span>
                <span
                  className={`action-pill ${activeCombatant.actions?.bonus_action ? 'active bonus_action' : ''}`}
                  title="Bonus Action"
                >
                  B
                </span>
                <span
                  className={`action-pill ${activeCombatant.actions?.reaction ? 'active reaction' : ''}`}
                  title="Reaction"
                >
                  R
                </span>
                <span
                  className={`action-pill ${activeCombatant.actions?.movement_remaining > 0 ? 'active movement' : ''}`}
                  title="Movement"
                >
                  {activeCombatant.actions?.movement_remaining ?? 0} ft
                </span>
              </div>
            ) : (
              <span className="active-combatant-name">Waiting for Initiative...</span>
            )}
            
            <div className="encounter-combat-controls">
              {isUserActiveTurn && (
                <button
                  type="button"
                  className="btn btn-primary btn-end-turn"
                  onClick={handleNextTurn}
                  disabled={activeTurnIndex === null || activeTurnIndex === undefined}
                >
                  End Turn <i className="bi bi-chevron-right"></i>
                </button>
              )}
            </div>
          </div>
        </div>
      )}

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
                    {canMovePlayerToken && dragState && reachableMovementCells.map((cell) => {
                      const isOrigin = cell.col === playerPlacement.col && cell.row === playerPlacement.row
                      const isSelected = dragState?.col === cell.col && dragState?.row === cell.row
                      return (
                        <div
                          key={`${cell.col},${cell.row}`}
                          className={`encounter-map-move-cell ${cell.isDifficult ? 'difficult' : ''} ${isOrigin ? 'origin' : ''} ${isSelected ? 'selected' : ''}`}
                          style={{
                            left: `${(cell.col / columns) * 100}%`,
                            top: `${(cell.row / rows) * 100}%`,
                            width: `${100 / columns}%`,
                            height: `${100 / rows}%`,
                          }}
                        />
                      )
                    })}
                    {displayPlacements.map((placement) => {
                      const isHighlighted = activeHoverId === placement.id
                      const isDraggable = canDragPlacement(placement)
                      const isDragging = dragState?.placementId === placement.id
                      const isActiveTurn = isEncounterActive && 
                        encounterState?.active_turn_index !== null &&
                        encounterState?.active_turn_index !== undefined &&
                        encounterState?.turn_order?.[encounterState.active_turn_index]?.placement_id === placement.id

                      return (
                        <div
                          key={placement.id}
                          className={`encounter-map-token ${placement.actor_type} ${isHighlighted ? 'highlighted' : ''} ${isDraggable ? 'draggable' : ''} ${isDragging ? 'dragging' : ''} ${isActiveTurn ? 'active-turn' : ''}`}
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
                                Move: {dragState?.placementId === placement.id ? dragState.cost : 0}/{movementSquares} sq
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
                        ? `${dragState.cost}/${dragState.maxSquares} sq`
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

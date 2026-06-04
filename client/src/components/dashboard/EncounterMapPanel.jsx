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

function rollD20() {
  const values = new Uint32Array(1)
  globalThis.crypto.getRandomValues(values)
  return (values[0] % 20) + 1
}

const TACTICAL_FILTERS = [
  { id: 'cover', label: 'Cover' },
  { id: 'hazard', label: 'Hazard' },
  { id: 'door', label: 'Door' },
  { id: 'difficult', label: 'Difficult' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'chokepoint', label: 'Choke' },
  { id: 'elevation', label: 'Elevation' },
  { id: 'spawn', label: 'Start' },
]

const COVER_LEVEL_SCORES = {
  none: 0,
  half: 1,
  three_quarters: 2,
  full: 3,
}

function getAreaFeatureKey(area, group = '', index = 0) {
  const label = String(area?.label || area?.kind || 'feature').toLowerCase().replace(/[^a-z0-9]+/g, '-')
  return `${group}:${index}:${label}`
}

function getAreaDimensions(area) {
  const rectWidth = Number(area?.rect?.width)
  const rectHeight = Number(area?.rect?.height)
  if ([rectWidth, rectHeight].every(Number.isFinite) && rectWidth > 0 && rectHeight > 0) {
    return { width: rectWidth, height: rectHeight }
  }

  if (Array.isArray(area?.polygon) && area.polygon.length >= 3) {
    const cols = area.polygon.map((point) => Number(point?.col)).filter(Number.isFinite)
    const rows = area.polygon.map((point) => Number(point?.row)).filter(Number.isFinite)
    if (cols.length >= 3 && rows.length >= 3) {
      return {
        width: Math.max(...cols) - Math.min(...cols),
        height: Math.max(...rows) - Math.min(...rows),
      }
    }
  }

  return { width: 0, height: 0 }
}

function inferCoverType(area) {
  const kind = String(area?.kind || '').toLowerCase()
  const movementEffect = String(area?.movement_effect || '').toLowerCase()
  const coverType = String(area?.cover_type || '').toLowerCase()

  if (coverType && coverType in COVER_LEVEL_SCORES) return coverType
  if (kind === 'wall' || kind === 'blocked' || movementEffect === 'blocks_movement') return 'full'
  if (kind === 'cover' || movementEffect === 'provides_cover') return 'half'
  return 'none'
}

function isCoverCandidate(area, group = '') {
  const kind = String(area?.kind || '').toLowerCase()
  const movementEffect = String(area?.movement_effect || '').toLowerCase()
  const coverType = String(area?.cover_type || '').toLowerCase()

  if (coverType === 'half' || coverType === 'three_quarters' || coverType === 'full') return true
  if (kind === 'wall' || kind === 'cover') return true
  if (movementEffect === 'provides_cover' || movementEffect === 'blocks_movement') return true
  return group === 'terrain_zones' && kind === 'blocked'
}

function isPreciseCoverProvider(area, group = '') {
  if (!isCoverCandidate(area, group)) return false
  if (group === 'obstacles') return true

  const { width, height } = getAreaDimensions(area)
  if (width <= 0 || height <= 0) return false

  const smallerSide = Math.max(Math.min(width, height), 1)
  const largerSide = Math.max(width, height)
  const cellArea = width * height
  const isCompact = cellArea <= 6
  const isNarrow = smallerSide <= 1.25
  const isLongBarrier = smallerSide <= 2.25 && (largerSide / smallerSide) >= 3
  return isCompact || isNarrow || isLongBarrier
}

function getAreaCategories(area, group = '') {
  const kind = String(area?.kind || '').toLowerCase()
  const movementEffect = String(area?.movement_effect || '').toLowerCase()
  const categories = new Set()

  if (group === 'friendly_spawn_boxes' || group === 'player_start_areas') categories.add('spawn')
  if (isCoverCandidate(area, group) && isPreciseCoverProvider(area, group)) categories.add('cover')
  if (kind === 'hazard' || movementEffect === 'hazardous') categories.add('hazard')
  if (kind === 'door' || movementEffect === 'interactive') categories.add('door')
  if (kind === 'difficult' || kind === 'water' || movementEffect === 'costs_extra_movement') categories.add('difficult')
  if (kind === 'blocked' || kind === 'wall' || movementEffect === 'blocks_movement') categories.add('blocked')
  if (kind === 'chokepoint') categories.add('chokepoint')
  if (kind === 'elevation') categories.add('elevation')

  return Array.from(categories)
}

function getPrimaryAreaCategory(area, group = '') {
  const categories = getAreaCategories(area, group)
  if (!categories.length) return null
  const priority = ['hazard', 'door', 'blocked', 'cover', 'difficult', 'chokepoint', 'elevation', 'spawn']
  return priority.find((item) => categories.includes(item)) || categories[0]
}

function getAreaOverlayStyle(area, columns, rows) {
  if (Array.isArray(area?.polygon) && area.polygon.length >= 3) {
    return {
      left: '0%',
      top: '0%',
      width: '100%',
      height: '100%',
      clipPath: `polygon(${area.polygon.map((point) => (
        `${(Number(point?.col) / columns) * 100}% ${(Number(point?.row) / rows) * 100}%`
      )).join(', ')})`,
    }
  }

  const rectCol = Number(area?.rect?.col)
  const rectRow = Number(area?.rect?.row)
  const rectWidth = Number(area?.rect?.width)
  const rectHeight = Number(area?.rect?.height)
  if (![rectCol, rectRow, rectWidth, rectHeight].every(Number.isFinite)) return null

  return {
    left: `${(rectCol / columns) * 100}%`,
    top: `${(rectRow / rows) * 100}%`,
    width: `${(rectWidth / columns) * 100}%`,
    height: `${(rectHeight / rows) * 100}%`,
  }
}

function areaContainsPoint(area, x, y) {
  if (Array.isArray(area?.polygon) && area.polygon.length >= 3) {
    return pointInPolygon(area.polygon, x, y)
  }

  const rectCol = Number(area?.rect?.col)
  const rectRow = Number(area?.rect?.row)
  const width = Number(area?.rect?.width)
  const height = Number(area?.rect?.height)
  if (![rectCol, rectRow, width, height].every(Number.isFinite)) return false
  return rectCol <= x && x <= rectCol + width && rectRow <= y && y <= rectRow + height
}

function crossProduct(ax, ay, bx, by, cx, cy) {
  return ((bx - ax) * (cy - ay)) - ((by - ay) * (cx - ax))
}

function pointOnSegment(ax, ay, bx, by, px, py) {
  if (px < Math.min(ax, bx) - 1e-9 || px > Math.max(ax, bx) + 1e-9) return false
  if (py < Math.min(ay, by) - 1e-9 || py > Math.max(ay, by) + 1e-9) return false
  return Math.abs(crossProduct(ax, ay, bx, by, px, py)) <= 1e-9
}

function segmentsIntersect(a1x, a1y, a2x, a2y, b1x, b1y, b2x, b2y) {
  const d1 = crossProduct(a1x, a1y, a2x, a2y, b1x, b1y)
  const d2 = crossProduct(a1x, a1y, a2x, a2y, b2x, b2y)
  const d3 = crossProduct(b1x, b1y, b2x, b2y, a1x, a1y)
  const d4 = crossProduct(b1x, b1y, b2x, b2y, a2x, a2y)

  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
    return true
  }
  if (Math.abs(d1) <= 1e-9 && pointOnSegment(a1x, a1y, a2x, a2y, b1x, b1y)) return true
  if (Math.abs(d2) <= 1e-9 && pointOnSegment(a1x, a1y, a2x, a2y, b2x, b2y)) return true
  if (Math.abs(d3) <= 1e-9 && pointOnSegment(b1x, b1y, b2x, b2y, a1x, a1y)) return true
  if (Math.abs(d4) <= 1e-9 && pointOnSegment(b1x, b1y, b2x, b2y, a2x, a2y)) return true
  return false
}

function segmentIntersectsArea(area, startX, startY, endX, endY) {
  if (Array.isArray(area?.polygon) && area.polygon.length >= 3) {
    if (areaContainsPoint(area, startX, startY) || areaContainsPoint(area, endX, endY)) return true
    let previous = area.polygon[area.polygon.length - 1]
    for (const current of area.polygon) {
      const x1 = Number(previous?.col)
      const y1 = Number(previous?.row)
      const x2 = Number(current?.col)
      const y2 = Number(current?.row)
      if ([x1, y1, x2, y2].every(Number.isFinite) && segmentsIntersect(startX, startY, endX, endY, x1, y1, x2, y2)) {
        return true
      }
      previous = current
    }
    return false
  }

  const left = Number(area?.rect?.col)
  const top = Number(area?.rect?.row)
  const width = Number(area?.rect?.width)
  const height = Number(area?.rect?.height)
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return false

  if (areaContainsPoint(area, startX, startY) || areaContainsPoint(area, endX, endY)) return true
  const right = left + width
  const bottom = top + height
  const edges = [
    [left, top, right, top],
    [right, top, right, bottom],
    [right, bottom, left, bottom],
    [left, bottom, left, top],
  ]
  return edges.some(([x1, y1, x2, y2]) => segmentsIntersect(startX, startY, endX, endY, x1, y1, x2, y2))
}

function evaluateDirectionalCover(vttSetup, attackerPlacement, targetPlacement) {
  if (!attackerPlacement || !targetPlacement || attackerPlacement.id === targetPlacement.id) {
    return {
      coverType: 'none',
      providers: [],
      featureKeys: [],
      strongestScore: 0,
    }
  }

  const startX = Number(attackerPlacement.col) + 0.5
  const startY = Number(attackerPlacement.row) + 0.5
  const endX = Number(targetPlacement.col) + 0.5
  const endY = Number(targetPlacement.row) + 0.5
  const providers = []

  ;['terrain_zones', 'obstacles'].forEach((group) => {
    const areas = Array.isArray(vttSetup?.[group]) ? vttSetup[group] : []
    areas.forEach((area, index) => {
      if (!isPreciseCoverProvider(area, group)) return
      if (areaContainsPoint(area, startX, startY) || areaContainsPoint(area, endX, endY)) return
      if (!segmentIntersectsArea(area, startX, startY, endX, endY)) return

      const coverType = inferCoverType(area)
      const score = COVER_LEVEL_SCORES[coverType] || 0
      if (!score) return
      providers.push({
        featureKey: getAreaFeatureKey(area, group, index),
        label: area?.label || 'Map feature',
        coverType,
        score,
      })
    })
  })

  if (!providers.length) {
    return {
      coverType: 'none',
      providers: [],
      featureKeys: [],
      strongestScore: 0,
    }
  }

  const strongestScore = Math.max(...providers.map((provider) => provider.score))
  const strongestProviders = providers.filter((provider) => provider.score === strongestScore)
  return {
    coverType: strongestProviders[0]?.coverType || 'none',
    providers: strongestProviders,
    featureKeys: strongestProviders.map((provider) => provider.featureKey),
    strongestScore,
  }
}

function summarizeCellFeatures(vttSetup, col, row) {
  const groups = ['friendly_spawn_boxes', 'terrain_zones', 'obstacles']
  const features = []

  groups.forEach((group) => {
    const areas = Array.isArray(vttSetup?.[group]) ? vttSetup[group] : []
    areas.forEach((area) => {
      if (!areaContainsCell(area, col, row)) return
      features.push({
        group,
        label: area?.label || 'Map feature',
        description: area?.description || '',
        categories: getAreaCategories(area, group),
        coverType: area?.cover_type || 'none',
      })
    })
  })

  return {
    features,
    blockedBy: features.filter((feature) => feature.categories.includes('blocked')).map((feature) => feature.label),
    difficultBy: features.filter((feature) => feature.categories.includes('difficult')).map((feature) => feature.label),
    coverBy: features.filter((feature) => feature.categories.includes('cover')).map((feature) => feature.label),
    hazardBy: features.filter((feature) => feature.categories.includes('hazard')).map((feature) => feature.label),
    doorBy: features.filter((feature) => feature.categories.includes('door')).map((feature) => feature.label),
    chokepointBy: features.filter((feature) => feature.categories.includes('chokepoint')).map((feature) => feature.label),
    elevationBy: features.filter((feature) => feature.categories.includes('elevation')).map((feature) => feature.label),
    spawnBy: features.filter((feature) => feature.categories.includes('spawn')).map((feature) => feature.label),
  }
}

export default function EncounterMapPanel({
  encounterMap,
  loading,
  isOwner,
  currentUser,
  currentCharacter,
  onEncounterMapChange,
  mapViewMode,
  setMapViewMode,
  onSendMessage,
}) {
  const [imageState, setImageState] = useState({ mapId: null, url: '', error: '' })
  const [labeledImageState, setLabeledImageState] = useState({ mapId: null, url: '', error: '' })
  const tokenLayerRef = useRef(null)
  
  // UX UI states
  const isCollapsed = mapViewMode === 'collapsed'
  const [showGridLines, setShowGridLines] = useState(true)
  const [showTacticalOverlay, setShowTacticalOverlay] = useState(true)
  const [showCellInspector, setShowCellInspector] = useState(true)
  const [activeTacticalFilters, setActiveTacticalFilters] = useState(() => TACTICAL_FILTERS.map((filter) => filter.id))
  const [isRosterOpen, setIsRosterOpen] = useState(true)
  const [isBriefingCollapsed, setIsBriefingCollapsed] = useState(false)
  const [showLabeledImage, setShowLabeledImage] = useState(false)
  const [activeHoverId, setActiveHoverId] = useState(null)
  const [prevMapId, setPrevMapId] = useState(encounterMap?.id || null)
  const [aspectRatio, setAspectRatio] = useState(null)
  const [imageDimensions, setImageDimensions] = useState({ width: 0, height: 0 })
  const [dragState, setDragState] = useState(null)
  const [pendingPlacementOverrides, setPendingPlacementOverrides] = useState({})
  const [moveError, setMoveError] = useState('')
  const [movementMessage, setMovementMessage] = useState('')
  const [isMovingToken, setIsMovingToken] = useState(false)
  const [manualInitValues, setManualInitValues] = useState({})
  const [hoveredCell, setHoveredCell] = useState(null)
  const [selectedCell, setSelectedCell] = useState(null)

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
    setHoveredCell(null)
    setSelectedCell(null)
  }

  const encounterState = useMemo(() => {
    const rawState = encounterMap?.encounter_state || encounterMap?.encounter_state_json
    if (!rawState) return null
    try {
      return typeof rawState === 'string'
        ? JSON.parse(rawState)
        : rawState
    } catch {
      return null
    }
  }, [encounterMap?.encounter_state, encounterMap?.encounter_state_json])

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
    const d20 = rollD20()
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
      if (onSendMessage) {
        const name = activeCombatant?.label || currentCharacter?.name || playerPlacement?.label || 'Player'
        await onSendMessage(`[Turn Ended] ${name}`)
      }
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
  const vttSetup = useMemo(() => encounterMap?.vtt_setup || {}, [encounterMap?.vtt_setup])
  const mapSummary = typeof vttSetup?.map_summary === 'string' ? vttSetup.map_summary : ''
  const tacticalNotes = Array.isArray(vttSetup?.tactical_notes) ? vttSetup.tactical_notes : []
  const activeTacticalFilterSet = useMemo(() => new Set(activeTacticalFilters), [activeTacticalFilters])
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
  const hoveredPlacement = useMemo(
    () => displayPlacements.find((placement) => placement.id === activeHoverId) || null,
    [displayPlacements, activeHoverId],
  )
  const directionalCoverPreview = useMemo(() => {
    if (!playerPlacement || !hoveredPlacement || hoveredPlacement.id === playerPlacement.id) {
      return {
        coverType: 'none',
        providers: [],
        featureKeys: [],
        strongestScore: 0,
      }
    }
    return evaluateDirectionalCover(vttSetup, playerPlacement, hoveredPlacement)
  }, [vttSetup, playerPlacement, hoveredPlacement])
  const tacticalOverlayAreas = useMemo(() => {
    if (!showTacticalOverlay || !columns || !rows) return []

    return [
      ...(Array.isArray(vttSetup?.friendly_spawn_boxes) ? vttSetup.friendly_spawn_boxes.map((area) => ({ ...area, overlayGroup: 'friendly_spawn_boxes' })) : []),
      ...(Array.isArray(vttSetup?.terrain_zones) ? vttSetup.terrain_zones.map((area) => ({ ...area, overlayGroup: 'terrain_zones' })) : []),
      ...(Array.isArray(vttSetup?.obstacles) ? vttSetup.obstacles.map((area) => ({ ...area, overlayGroup: 'obstacles' })) : []),
    ]
      .map((area, index) => {
        const category = getPrimaryAreaCategory(area, area.overlayGroup)
        const style = getAreaOverlayStyle(area, columns, rows)
        if (!category || !style || !activeTacticalFilterSet.has(category)) return null
        const featureKey = getAreaFeatureKey(area, area.overlayGroup, index)
        return {
          id: `${area.overlayGroup}-${area.label || index}-${index}`,
          featureKey,
          category,
          label: area.label || 'Map feature',
          description: area.description || '',
          style,
          isDirectionalProvider: directionalCoverPreview.featureKeys.includes(featureKey),
        }
      })
      .filter(Boolean)
  }, [showTacticalOverlay, columns, rows, vttSetup, activeTacticalFilterSet, directionalCoverPreview.featureKeys])
  
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
  const inspectedCell = selectedCell || hoveredCell
  const inspectedCellDetails = useMemo(() => {
    if (!showCellInspector || !inspectedCell || !columns || !rows) return null

    const cellFeatures = summarizeCellFeatures(vttSetup, inspectedCell.col, inspectedCell.row)
    const movementProfile = getCellMovementProfile(vttSetup, inspectedCell.col, inspectedCell.row)
    const reachableCell = reachableMovementCellMap.get(`${inspectedCell.col},${inspectedCell.row}`) || null
    const distanceFromPlayer = playerPlacement
      ? getGridMoveDistance(playerPlacement.col, playerPlacement.row, inspectedCell.col, inspectedCell.row)
      : null

    return {
      ...inspectedCell,
      coordinate: getGridCoordinate(inspectedCell.col, inspectedCell.row),
      movementProfile,
      reachableCell,
      distanceFromPlayer,
      cellFeatures,
    }
  }, [showCellInspector, inspectedCell, columns, rows, vttSetup, reachableMovementCellMap, playerPlacement])

  // Sync state changes with localStorage
  const updateMapViewMode = (newMode) => {
    setMapViewMode(newMode)
    localStorage.setItem('encounter_map_view_mode', newMode)
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

  const toggleTacticalFilter = (filterId) => {
    setActiveTacticalFilters((current) => (
      current.includes(filterId)
        ? current.filter((item) => item !== filterId)
        : [...current, filterId]
    ))
  }

  const handleBoardPointerMove = (event) => {
    if (!showCellInspector || dragState) return
    const cell = getGridCellFromPointer(event)
    setHoveredCell(cell)
  }

  const handleBoardPointerLeave = () => {
    setHoveredCell(null)
  }

  const handleBoardClick = (event) => {
    if (!showCellInspector || dragState) return
    const cell = getGridCellFromPointer(event)
    if (!cell) return
    setSelectedCell((current) => (
      current && current.col === cell.col && current.row === cell.row ? null : cell
    ))
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

  const renderMapBoard = (imagePadding = '18px') => {
    if (loading) {
      return (
        <div className="encounter-map-placeholder">
          <i className="bi bi-hourglass-split"></i>
          <span>Loading Map State...</span>
        </div>
      )
    }

    if (encounterMap && imageUrl) {
      const inspectorTags = inspectedCellDetails
        ? Array.from(new Set(inspectedCellDetails.cellFeatures.features.flatMap((feature) => feature.categories)))
        : []

      return (
        <div className="encounter-map-images" style={{ padding: imagePadding }}>
          <figure className="encounter-map-frame">
            <div
              className="encounter-map-board"
              style={{
                aspectRatio: aspectRatio ? `${aspectRatio}` : 'auto',
              }}
              onPointerMove={handleBoardPointerMove}
              onPointerLeave={handleBoardPointerLeave}
              onClick={handleBoardClick}
            >
              <img
                src={imageUrl}
                alt={encounterMap.title || 'Encounter map'}
                className="encounter-map-image"
                onLoad={handleImageLoad}
              />

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

              {canOverlayPlacements && (
                <div className="encounter-map-area-layer" style={gridLayout}>
                  {showTacticalOverlay && tacticalOverlayAreas.map((area) => (
                    <div
                      key={area.id}
                      className={`encounter-map-area-overlay ${area.category} ${area.isDirectionalProvider ? 'directional-provider' : ''}`}
                      style={area.style}
                      title={area.label}
                    />
                  ))}
                  {inspectedCell && (
                    <div
                      className={`encounter-map-cell-highlight ${selectedCell ? 'selected' : 'hovered'}`}
                      style={{
                        left: `${(inspectedCell.col / columns) * 100}%`,
                        top: `${(inspectedCell.row / rows) * 100}%`,
                        width: `${100 / columns}%`,
                        height: `${100 / rows}%`,
                      }}
                    />
                  )}
                </div>
              )}

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
                    const directionalCoverForPlacement = (
                      hoveredPlacement?.id === placement.id &&
                      placement.id !== playerPlacement?.id &&
                      directionalCoverPreview.coverType !== 'none'
                    )
                    const isActiveTurn = isEncounterActive &&
                      encounterState?.active_turn_index !== null &&
                      encounterState?.active_turn_index !== undefined &&
                      encounterState?.turn_order?.[encounterState.active_turn_index]?.placement_id === placement.id

                    return (
                      <div
                        key={placement.id}
                        className={`encounter-map-token ${placement.actor_type} ${isHighlighted ? 'highlighted' : ''} ${isDraggable ? 'draggable' : ''} ${isDragging ? 'dragging' : ''} ${isActiveTurn ? 'active-turn' : ''} ${directionalCoverForPlacement ? `has-directional-cover cover-${directionalCoverPreview.coverType}` : ''}`}
                        style={{
                          left: `${((placement.col + 0.5) / columns) * 100}%`,
                          top: `${((placement.row + 0.5) / rows) * 100}%`,
                          width: `calc(0.8 * min(100cqw / ${columns}, 100cqh / ${rows}))`,
                          height: `calc(0.8 * min(100cqw / ${columns}, 100cqh / ${rows}))`,
                          aspectRatio: '1',
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
                          {directionalCoverForPlacement && (
                            <span className={`tooltip-cover cover-${directionalCoverPreview.coverType}`}>
                              {directionalCoverPreview.coverType.replace('_', ' ')} cover from you
                              {directionalCoverPreview.providers[0]?.label ? ` via ${directionalCoverPreview.providers[0].label}` : ''}
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

              {hoveredPlacement && hoveredPlacement.id !== playerPlacement?.id && (
                <div className={`encounter-map-cover-hud ${directionalCoverPreview.coverType !== 'none' ? `cover-${directionalCoverPreview.coverType}` : 'cover-none'}`}>
                  <strong>{hoveredPlacement.label}</strong>
                  <span>
                    {directionalCoverPreview.coverType === 'none'
                      ? 'No directional cover from your token.'
                      : `${directionalCoverPreview.coverType.replace('_', ' ')} cover from ${directionalCoverPreview.providers.map((provider) => provider.label).join(', ')}.`}
                  </span>
                </div>
              )}

              {showCellInspector && inspectedCellDetails && (
                <div className="encounter-map-cell-inspector">
                  <div className="encounter-map-cell-inspector-header">
                    <strong>{inspectedCellDetails.coordinate}</strong>
                    <span>{selectedCell ? 'Pinned' : 'Hover'}</span>
                  </div>
                  <div className="encounter-map-cell-tags">
                    {inspectorTags.length ? inspectorTags.map((tag) => (
                      <span key={tag} className={`encounter-map-cell-tag ${tag}`}>
                        {TACTICAL_FILTERS.find((item) => item.id === tag)?.label || tag}
                      </span>
                    )) : (
                      <span className="encounter-map-cell-tag neutral">Open floor</span>
                    )}
                  </div>
                  <div className="encounter-map-cell-stats">
                    {inspectedCellDetails.reachableCell
                      ? <span>Reachable in {inspectedCellDetails.reachableCell.cost} sq</span>
                      : playerPlacement
                        ? <span>{inspectedCellDetails.distanceFromPlayer} sq from you</span>
                        : <span>Board reference</span>}
                    {inspectedCellDetails.cellFeatures.blockedBy.length > 0 && (
                      <span>Blocked by {inspectedCellDetails.cellFeatures.blockedBy[0]}</span>
                    )}
                    {inspectedCellDetails.cellFeatures.difficultBy.length > 0 && (
                      <span>Difficult: {inspectedCellDetails.cellFeatures.difficultBy[0]}</span>
                    )}
                  </div>
                  {inspectedCellDetails.cellFeatures.features.length > 0 && (
                    <ul className="encounter-map-cell-feature-list">
                      {inspectedCellDetails.cellFeatures.features.slice(0, 3).map((feature) => (
                        <li key={`${feature.group}-${feature.label}`}>
                          <strong>{feature.label}</strong>
                          {feature.description ? `: ${feature.description}` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          </figure>
        </div>
      )
    }

    if (encounterMap && imageError) {
      return (
        <div className="encounter-map-placeholder">
          <i className="bi bi-exclamation-triangle-fill"></i>
          <span>{imageError}</span>
        </div>
      )
    }

    if (encounterMap) {
      return (
        <div className="encounter-map-placeholder">
          <i className="bi bi-image"></i>
          <span>Downloading map visual...</span>
        </div>
      )
    }

    return (
      <div className="encounter-map-placeholder">
        <i className="bi bi-map"></i>
        <span>The AI Dungeon Master will generate a tactical map when positioning matters.</span>
      </div>
    )
  }

  // --- COLLAPSED VIEW RENDER ---
  if (isCollapsed) {
    return null
  }

  // --- SEMI-COLLAPSED (SPLIT) VIEW RENDER ---
  if (mapViewMode === 'semi') {
    return (
      <section className="encounter-map-panel semi" aria-label="Encounter map split view">
        <div className={`encounter-map-stage ${!encounterMap ? 'empty' : ''}`}>
          {/* Floating Controls Toolbar */}
          <div className="encounter-map-floating-toolbar" style={{
            position: 'absolute',
            top: '12px',
            right: '12px',
            zIndex: 10,
            display: 'flex',
            gap: '6px',
            background: 'rgba(10, 11, 15, 0.75)',
            backdropFilter: 'blur(8px)',
            padding: '6px 8px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid rgba(255,255,255,0.12)'
          }}>
            {encounterMap && (
              <button
                className={`btn-icon-map ${showGridLines ? 'active' : ''}`}
                onClick={() => setShowGridLines(!showGridLines)}
                title="Toggle Grid Overlay"
                aria-label="Toggle grid overlay"
              >
                <i className="bi bi-grid-3x3"></i>
              </button>
            )}
            {encounterMap && (
              <>
                <button
                  className={`btn-icon-map ${showTacticalOverlay ? 'active' : ''}`}
                  onClick={() => setShowTacticalOverlay(!showTacticalOverlay)}
                  title="Toggle Tactical Overlay"
                  aria-label="Toggle tactical overlay"
                >
                  <i className="bi bi-layers"></i>
                </button>
                <button
                  className={`btn-icon-map ${showCellInspector ? 'active' : ''}`}
                  onClick={() => setShowCellInspector(!showCellInspector)}
                  title="Toggle Cell Inspector"
                  aria-label="Toggle cell inspector"
                >
                  <i className="bi bi-crosshair2"></i>
                </button>
              </>
            )}
            <button
              className={`btn-icon-map ${mapViewMode === 'collapsed' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('collapsed')}
              title="Minimize Map"
              aria-label="Minimize map"
            >
              <i className="bi bi-chevron-bar-up"></i>
            </button>
            <button
              className={`btn-icon-map ${mapViewMode === 'semi' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('semi')}
              title="Split View"
              aria-label="Split view"
            >
              <i className="bi bi-layout-split"></i>
            </button>
            <button
              className={`btn-icon-map ${mapViewMode === 'fullscreen' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('fullscreen')}
              title="Full Screen VTT"
              aria-label="Full screen VTT"
            >
              <i className="bi bi-arrows-fullscreen"></i>
            </button>
          </div>

          {renderMapBoard('32px 18px 18px 18px')}
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
              <button
                className={`btn-icon-map ${showTacticalOverlay ? 'active' : ''}`}
                onClick={() => setShowTacticalOverlay(!showTacticalOverlay)}
                title="Toggle Tactical Overlay"
                aria-label="Toggle tactical overlay"
              >
                <i className="bi bi-layers"></i>
              </button>
              <button
                className={`btn-icon-map ${showCellInspector ? 'active' : ''}`}
                onClick={() => setShowCellInspector(!showCellInspector)}
                title="Toggle Cell Inspector"
                aria-label="Toggle cell inspector"
              >
                <i className="bi bi-crosshair2"></i>
              </button>
              {placements.length > 0 && mapViewMode !== 'fullscreen' && (
                <button
                  className={`btn-icon-map ${isRosterOpen ? 'active' : ''}`}
                  onClick={() => setIsRosterOpen(!isRosterOpen)}
                  title="Toggle Combat Roster"
                  aria-label="Toggle combat roster"
                >
                  <i className="bi bi-people-fill"></i>
                </button>
              )}
              {encounterMap && (mapSummary || tacticalNotes.length > 0) && (
                <button
                  className={`btn-icon-map ${!isBriefingCollapsed ? 'active' : ''}`}
                  onClick={() => setIsBriefingCollapsed(!isBriefingCollapsed)}
                  title="Toggle Map Briefing"
                  aria-label="Toggle map briefing"
                >
                  <i className="bi bi-info-circle"></i>
                </button>
              )}
            </>
          )}

          <div className="map-view-mode-selector btn-group" style={{ display: 'inline-flex', gap: '2px', background: 'rgba(0,0,0,0.2)', padding: '2px', borderRadius: 'var(--radius-sm)' }}>
            <button
              className={`btn-icon-map ${mapViewMode === 'collapsed' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('collapsed')}
              title="Minimize Map"
              aria-label="Minimize map"
            >
              <i className="bi bi-chevron-bar-up"></i>
            </button>
            <button
              className={`btn-icon-map ${mapViewMode === 'semi' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('semi')}
              title="Split View"
              aria-label="Split view"
            >
              <i className="bi bi-layout-split"></i>
            </button>
            <button
              className={`btn-icon-map ${mapViewMode === 'fullscreen' ? 'active' : ''}`}
              onClick={() => updateMapViewMode('fullscreen')}
              title="Full Screen VTT"
              aria-label="Full screen VTT"
            >
              <i className="bi bi-arrows-fullscreen"></i>
            </button>
          </div>
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

      {encounterMap && (
        <div className="encounter-map-filter-strip">
          {TACTICAL_FILTERS.map((filter) => (
            <button
              key={filter.id}
              type="button"
              className={`encounter-map-filter-chip ${activeTacticalFilterSet.has(filter.id) ? 'active' : ''}`}
              onClick={() => toggleTacticalFilter(filter.id)}
            >
              {filter.label}
            </button>
          ))}
        </div>
      )}

      {encounterMap && (mapSummary || tacticalNotes.length > 0) && !isBriefingCollapsed && (
          <div className="encounter-map-briefing">
            <div
              className="encounter-map-briefing-header"
              style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', userSelect: 'none' }}
            >
              <span className="briefing-title" style={{ fontSize: '0.72rem', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-gold)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <i className="bi bi-info-circle"></i> Map Briefing
              </span>
              <button
                type="button"
                className="btn-close-briefing"
                onClick={() => setIsBriefingCollapsed(true)}
                style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', padding: '2px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                title="Hide Briefing"
                aria-label="Hide briefing"
              >
                <i className="bi bi-x-lg" style={{ fontSize: '0.8rem' }}></i>
              </button>
            </div>
            <div className="briefing-content" style={{ marginTop: '10px' }}>
              {mapSummary && <p className="encounter-map-summary">{mapSummary}</p>}
              {tacticalNotes.length > 0 && (
                <ul className="encounter-map-notes">
                  {tacticalNotes.slice(0, 4).map((note, index) => (
                    <li key={`${note}-${index}`}>{note}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )
      }

      <div className="encounter-map-body-layout">
        <div className={`encounter-map-stage ${!encounterMap ? 'empty' : ''}`}>
        {renderMapBoard()}
        </div>

        {/* Styled Combatants Roster */}
        {encounterMap && placements.length > 0 && isRosterOpen && mapViewMode !== 'fullscreen' && (
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

        {/* Diagnostics HUD Overlays (floating bottom-left) */}
        {isOwner && encounterMap && (
          <div className="encounter-vtt-overlays-bottom-left">
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
            </div>
        )}
      </div>
    </section>
  )
}

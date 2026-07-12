import { useEffect, useMemo, useRef, useState } from 'react'
import {
  getEncounterMapImage,
  getEncounterMapLabeledImage,
  moveEncounterMapToken,
} from '../../api/client'

// Helper functions (extracted from EncounterMapPanel)
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
    const cols = area.polygon.map((p) => p.col)
    const rowsList = area.polygon.map((p) => p.row)
    const minCol = Math.min(...cols)
    const maxCol = Math.max(...cols)
    const minRow = Math.min(...rowsList)
    const maxRow = Math.max(...rowsList)
    const width = maxCol - minCol
    const height = maxRow - minRow
    const pointsStr = area.polygon
      .map((p) => `${((p.col - minCol) / (width || 1)) * 100}% ${((p.row - minRow) / (height || 1)) * 100}%`)
      .join(', ')
    return {
      left: `${(minCol / columns) * 100}%`,
      top: `${(minRow / rows) * 100}%`,
      width: `${(width / columns) * 100}%`,
      height: `${(height / rows) * 100}%`,
      clipPath: `polygon(${pointsStr})`,
    }
  }

  const r = area?.rect
  if (!r) return null
  return {
    left: `${(r.col / columns) * 100}%`,
    top: `${(r.row / rows) * 100}%`,
    width: `${(r.width / columns) * 100}%`,
    height: `${(r.height / rows) * 100}%`,
  }
}

function summarizeCellFeatures(vttSetup, col, row) {
  const summary = { blockedBy: [], difficultBy: [], features: [] }
  const groups = ['terrain_zones', 'obstacles', 'friendly_spawn_boxes']
  groups.forEach((group) => {
    const areas = Array.isArray(vttSetup?.[group]) ? vttSetup[group] : []
    areas.forEach((area) => {
      if (!area || !areaContainsCell(area, col, row)) return

      const categories = getAreaCategories(area, group)
      const label = area.label || area.kind || 'Unnamed feature'
      summary.features.push({ group, label, description: area.description || '', categories })

      const kind = String(area.kind || '').toLowerCase()
      const movementEffect = String(area.movement_effect || '').toLowerCase()
      if (kind === 'blocked' || kind === 'wall' || movementEffect === 'blocks_movement') {
        summary.blockedBy.push(label)
      }
      if (kind === 'difficult' || kind === 'water' || movementEffect === 'costs_extra_movement') {
        summary.difficultBy.push(label)
      }
    })
  })
  return summary
}

function getHpPercent(current, max) {
  if (!max || max <= 0) return 0
  return Math.max(0, Math.min(100, (current / max) * 100))
}

function getHpGradientSeed(name) {
  let hash = 0
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash)
  const hues = [250, 270, 290, 310, 330, 200, 220, 180]
  const h1 = hues[Math.abs(hash) % hues.length]
  const h2 = (h1 + 40) % 360
  return `linear-gradient(135deg, hsl(${h1}, 60%, 55%), hsl(${h2}, 55%, 45%))`
}

// Ray-casting directional cover math
function crossProduct(ax, ay, bx, by, cx, cy) {
  return ((bx - ax) * (cy - ay)) - ((by - ay) * (cx - ax))
}

function segmentsIntersect(p1, q1, p2, q2) {
  const d1 = crossProduct(p2.x, p2.y, q2.x, q2.y, p1.x, p1.y)
  const d2 = crossProduct(p2.x, p2.y, q2.x, q2.y, q1.x, q1.y)
  const d3 = crossProduct(p1.x, p1.y, q1.x, q1.y, p2.x, p2.y)
  const d4 = crossProduct(p1.x, p1.y, q1.x, q1.y, q2.x, q2.y)
  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
    return true
  }
  return false
}

function getAreaSegments(area) {
  const segments = []
  if (area.rect) {
    const r = area.rect
    const p1 = { x: r.col, y: r.row }
    const p2 = { x: r.col + r.width, y: r.row }
    const p3 = { x: r.col + r.width, y: r.row + r.height }
    const p4 = { x: r.col, y: r.row + r.height }
    segments.push({ p: p1, q: p2 }, { p: p2, q: p3 }, { p: p3, q: p4 }, { p: p4, q: p1 })
  } else if (Array.isArray(area.polygon) && area.polygon.length >= 3) {
    for (let i = 0; i < area.polygon.length; i++) {
      const p = area.polygon[i]
      const q = area.polygon[(i + 1) % area.polygon.length]
      segments.push({ p: { x: p.col, y: p.row }, q: { x: q.col, y: q.row } })
    }
  }
  return segments
}

function isNpcCoveredFromCurrentPlayer(playerCol, playerRow, npcCol, npcRow, vttSetup) {
  const start = { x: playerCol + 0.5, y: playerRow + 0.5 }
  const end = { x: npcCol + 0.5, y: npcRow + 0.5 }

  const providers = []
  let highestCoverScore = 0

  const groups = ['obstacles', 'terrain_zones']
  groups.forEach((group) => {
    const areas = Array.isArray(vttSetup?.[group]) ? vttSetup[group] : []
    areas.forEach((area, index) => {
      if (!isCoverCandidate(area, group) || !isPreciseCoverProvider(area, group)) return

      const coverType = inferCoverType(area)
      const score = COVER_LEVEL_SCORES[coverType] || 0
      if (score <= 0) return

      const segments = getAreaSegments(area)
      const intersects = segments.some((seg) => segmentsIntersect(start, end, seg.p, seg.q))
      if (intersects) {
        providers.push({
          group,
          label: area.label || area.kind || 'Cover obstacle',
          coverType,
          featureKey: getAreaFeatureKey(area, group, index),
        })
        if (score > highestCoverScore) {
          highestCoverScore = score
        }
      }
    })
  })

  let finalCoverType = 'none'
  Object.entries(COVER_LEVEL_SCORES).forEach(([type, score]) => {
    if (score === highestCoverScore) finalCoverType = type
  })

  return {
    coverType: finalCoverType,
    providers,
    featureKeys: providers.map((p) => p.featureKey),
  }
}

export default function EncounterMapBoard({
  encounterMap,
  loading = false,
  isOwner = false,
  currentUser = null,
  currentCharacter = null,
  onEncounterMapChange = null,
  onSendMessage = null,
  showGridLines = true,
  showTacticalOverlay = true,
  showCellInspector = true,
  activeHoverId = null,
  setActiveHoverId = () => {},
  imagePadding = '18px',
}) {
  const [imageState, setImageState] = useState({ mapId: null, url: '', error: '' })
  const [labeledImageState, setLabeledImageState] = useState({ mapId: null, url: '', error: '' })
  const [aspectRatio, setAspectRatio] = useState(null)
  const [imageDimensions, setImageDimensions] = useState({ width: 0, height: 0 })
  const [dragState, setDragState] = useState(null)
  const [isMovingToken, setIsMovingToken] = useState(false)
  const [moveError, setMoveError] = useState('')
  const [movementMessage, setMovementMessage] = useState('')
  const [pendingPlacementOverrides, setPendingPlacementOverrides] = useState({})
  
  const [hoveredCell, setHoveredCell] = useState(null)
  const [selectedCell, setSelectedCell] = useState(null)

  const tokenLayerRef = useRef(null)

  const columns = encounterMap?.grid_columns || 12
  const rows = encounterMap?.grid_rows || 12
  const grid = encounterMap?.grid_json || encounterMap?.grid || {}
  const vttSetup = encounterMap?.vtt_setup_json || encounterMap?.vtt_setup || {}
  const rawState = encounterMap?.encounter_state_json || encounterMap?.encounter_state || {}
  
  const encounterState = useMemo(() => {
    try {
      return typeof rawState === 'string' ? JSON.parse(rawState) : rawState
    } catch {
      return {}
    }
  }, [rawState])

  const isEncounterActive = Boolean(encounterState?.active)
  
  const currentUserActorId = currentUser?.id != null ? String(currentUser.id) : ''

  const placements = useMemo(() => {
    return encounterMap?.placements || []
  }, [encounterMap?.placements])

  const displayPlacements = useMemo(() => {
    return placements.map((p) => {
      const override = pendingPlacementOverrides[p.id]
      if (override) {
        return { ...p, col: override.col, row: override.row }
      }
      if (dragState && dragState.placementId === p.id) {
        return { ...p, col: dragState.col, row: dragState.row }
      }
      return p
    })
  }, [placements, pendingPlacementOverrides, dragState])

  const playerPlacement = useMemo(() => {
    return displayPlacements.find(
      (p) => p.actor_type === 'player' && String(p.actor_id) === currentUserActorId
    )
  }, [displayPlacements, currentUserActorId])

  const movementSquares = useMemo(() => {
    if (playerPlacement && isEncounterActive && encounterState?.turn_order) {
      const activeMatch = encounterState.turn_order.find(
        (t) => t.placement_id === playerPlacement.id
      )
      if (activeMatch && activeMatch.actions && typeof activeMatch.actions.movement_remaining === 'number') {
        return Math.floor(activeMatch.actions.movement_remaining / 5)
      }
    }
    return 6 // Default 30 ft
  }, [playerPlacement, isEncounterActive, encounterState])

  const canMovePlayerToken = useMemo(() => {
    if (!playerPlacement) return false
    if (!isEncounterActive) return true
    if (encounterState.active_turn_index === null || encounterState.active_turn_index === undefined) return false
    const activeUnit = encounterState.turn_order?.[encounterState.active_turn_index]
    return activeUnit && activeUnit.placement_id === playerPlacement.id
  }, [playerPlacement, isEncounterActive, encounterState])

  const canDragPlayerToken = canMovePlayerToken

  const reachableMovementCells = useMemo(() => {
    if (!canMovePlayerToken || !playerPlacement) return []
    return getReachableMovementCells(vttSetup, columns, rows, playerPlacement.col, playerPlacement.row, movementSquares)
  }, [canMovePlayerToken, playerPlacement, vttSetup, columns, rows, movementSquares])

  const reachableMovementCellMap = useMemo(() => {
    return new Map(reachableMovementCells.map((c) => [`${c.col},${c.row}`, c]))
  }, [reachableMovementCells])

  const hoveredPlacement = useMemo(() => {
    if (!activeHoverId) return null
    return displayPlacements.find((p) => p.id === activeHoverId) || null
  }, [activeHoverId, displayPlacements])

  const directionalCoverPreview = useMemo(() => {
    if (!hoveredPlacement || !playerPlacement) return { coverType: 'none', providers: [], featureKeys: [] }
    return isNpcCoveredFromCurrentPlayer(playerPlacement.col, playerPlacement.row, hoveredPlacement.col, hoveredPlacement.row, vttSetup)
  }, [hoveredPlacement, playerPlacement, vttSetup])

  // Load map visual blobs
  const imageUrl = imageState.mapId === encounterMap?.id ? imageState.url : (encounterMap?.url || '')
  const imageError = imageState.mapId === encounterMap?.id ? imageState.error : ''

  useEffect(() => {
    let cancelled = false
    const objectUrls = []

    if (!encounterMap?.id) return () => {}

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

  const gridLayout = useMemo(() => {
    if (!grid || !grid.origin_px || !grid.cell_size_px || !imageDimensions.width || !imageDimensions.height) {
      return { left: '0%', top: '0%', width: '100%', height: '100%' }
    }
    const { width: imgW, height: imgH } = imageDimensions
    const originX = grid.origin_px.x || 0
    const originY = grid.origin_px.y || 0
    let cellW = typeof grid.cell_size_px === 'number' ? grid.cell_size_px : (grid.cell_size_px?.x || 50)
    let cellH = typeof grid.cell_size_px === 'number' ? grid.cell_size_px : (grid.cell_size_px?.y || 50)

    return {
      left: `${(originX / imgW) * 100}%`,
      top: `${(originY / imgH) * 100}%`,
      width: `${((columns * cellW) / imgW) * 100}%`,
      height: `${((rows * cellH) / imgH) * 100}%`,
    }
  }, [grid, columns, rows, imageDimensions])

  const inspectedCell = selectedCell || hoveredCell
  const inspectedCellDetails = useMemo(() => {
    if (!showCellInspector || !inspectedCell) return null
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

  const handleImageLoad = (e) => {
    const { naturalWidth, naturalHeight } = e.target
    if (naturalWidth && naturalHeight) {
      setAspectRatio(naturalWidth / naturalHeight)
      setImageDimensions({ width: naturalWidth, height: naturalHeight })
    }
  }

  const getGridCellFromPointer = (event) => {
    const layer = tokenLayerRef.current
    if (!layer) return null
    const rect = layer.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    const x = clamp((event.clientX - rect.left) / rect.width, 0, 0.999999)
    const y = clamp((event.clientY - rect.top) / rect.height, 0, 0.999999)
    return {
      col: clamp(Math.floor(x * columns), 0, columns - 1),
      row: clamp(Math.floor(y * rows), 0, rows - 1),
    }
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
    const cell = getGridCellFromPointer(event)
    if (!cell) return
    const reachableCell = reachableMovementCellMap.get(`${cell.col},${cell.row}`)
      || getNearestReachableCell(cell, reachableMovementCells)
    if (!reachableCell) return

    setDragState((current) => {
      if (!current) return null
      return {
        ...current,
        col: reachableCell.col,
        row: reachableCell.row,
        distance: getGridMoveDistance(current.fromCol, current.fromRow, reachableCell.col, reachableCell.row),
        cost: reachableCell.cost,
      }
    })
  }

  const handleTokenPointerUp = async (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId || !encounterMap?.id) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.releasePointerCapture?.(event.pointerId)

    const destination = { col: dragState.col, row: dragState.row, cost: dragState.cost }
    const distance = destination.cost
    const placementId = dragState.placementId
    setDragState(null)

    if (!distance) {
      setActiveHoverId(null)
      return
    }

    setPendingPlacementOverrides((current) => ({ ...current, [placementId]: destination }))
    setIsMovingToken(true)

    try {
      const data = await moveEncounterMapToken(encounterMap.id, destination.col, destination.row)
      onEncounterMapChange?.(data.encounter_map)
      setPendingPlacementOverrides((current) => {
        const next = { ...current }
        delete next[placementId]
        return next
      })
      setMovementMessage(`Moved ${distance}/${movementSquares} sq to ${getGridCoordinate(destination.col, destination.row)}.`)
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

  if (loading) {
    return (
      <div className="encounter-map-placeholder">
        <i className="bi bi-hourglass-split"></i>
        <span>Loading Map State...</span>
      </div>
    )
  }

  if (encounterMap && imageUrl) {
    const tacticalOverlayAreas = [
      ...(Array.isArray(vttSetup?.friendly_spawn_boxes) ? vttSetup.friendly_spawn_boxes.map((area) => ({ ...area, overlayGroup: 'friendly_spawn_boxes' })) : []),
      ...(Array.isArray(vttSetup?.terrain_zones) ? vttSetup.terrain_zones.map((area) => ({ ...area, overlayGroup: 'terrain_zones' })) : []),
      ...(Array.isArray(vttSetup?.obstacles) ? vttSetup.obstacles.map((area) => ({ ...area, overlayGroup: 'obstacles' })) : []),
    ]
      .map((area, index) => {
        const category = getPrimaryAreaCategory(area, area.overlayGroup)
        const style = getAreaOverlayStyle(area, columns, rows)
        if (!category || !style) return null
        const featureKey = getAreaFeatureKey(area, area.overlayGroup, index)
        return {
          id: `${area.overlayGroup}-${area.label || index}-${index}`,
          featureKey,
          category,
          label: area.label || 'Map feature',
          style,
          isDirectionalProvider: directionalCoverPreview.featureKeys.includes(featureKey),
        }
      })
      .filter(Boolean)

    const inspectorTags = inspectedCellDetails
      ? Array.from(new Set(inspectedCellDetails.cellFeatures.features.flatMap((feature) => feature.categories)))
      : []

    return (
      <div className="encounter-map-images" style={{ padding: imagePadding, position: 'relative', width: '100%', height: '100%', display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
        <figure className="encounter-map-frame" style={{ margin: 0, position: 'relative', width: '100%', height: '100%', display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
          <div
            className="encounter-map-board"
            style={{
              aspectRatio: aspectRatio ? `${aspectRatio}` : 'auto',
              position: 'relative',
              maxWidth: '100%',
              maxHeight: '100%',
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
              style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain' }}
            />

            <div
              className="encounter-map-grid-overlay"
              style={{
                '--grid-cols': columns,
                '--grid-rows': rows,
                display: showGridLines ? 'block' : 'none',
                position: 'absolute',
                ...gridLayout,
              }}
            />

            <div className="encounter-map-area-layer" style={{ position: 'absolute', ...gridLayout }}>
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
                    position: 'absolute',
                    left: `${(inspectedCell.col / columns) * 100}%`,
                    top: `${(inspectedCell.row / rows) * 100}%`,
                    width: `${100 / columns}%`,
                    height: `${100 / rows}%`,
                  }}
                />
              )}
            </div>

            <div
              ref={tokenLayerRef}
              className="encounter-map-token-layer"
              aria-label="Placed combatants"
              style={{ position: 'absolute', ...gridLayout }}
            >
              {canMovePlayerToken && dragState && reachableMovementCells.map((cell) => {
                const isOrigin = cell.col === playerPlacement.col && cell.row === playerPlacement.row
                const isSelected = dragState?.col === cell.col && dragState?.row === cell.row
                return (
                  <div
                    key={`${cell.col},${cell.row}`}
                    className={`encounter-map-move-cell ${cell.isDifficult ? 'difficult' : ''} ${isOrigin ? 'origin' : ''} ${isSelected ? 'selected' : ''}`}
                    style={{
                      position: 'absolute',
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
                      position: 'absolute',
                      left: `${((placement.col + 0.5) / columns) * 100}%`,
                      top: `${((placement.row + 0.5) / rows) * 100}%`,
                      width: `calc(0.8 * min(100cqw / ${columns}, 100cqh / ${rows}))`,
                      height: `calc(0.8 * min(100cqw / ${columns}, 100cqh / ${rows}))`,
                      aspectRatio: '1',
                      transform: 'translate(-50%, -50%)',
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
                        </span>
                      )}
                      <span className="tooltip-alliance">{placement.actor_type}</span>
                    </div>
                  </div>
                )
              })}
            </div>

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
                    : `${directionalCoverPreview.coverType.replace('_', ' ')} cover.`}
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
                    <span>Blocked by <strong>{inspectedCellDetails.cellFeatures.blockedBy[0]}</strong></span>
                  )}
                  {inspectedCellDetails.cellFeatures.difficultBy.length > 0 && (
                    <span>Difficult: <strong>{inspectedCellDetails.cellFeatures.difficultBy[0]}</strong></span>
                  )}
                </div>
              </div>
            )}
          </div>
        </figure>
      </div>
    )
  }

  return (
    <div className="encounter-map-placeholder">
      <i className="bi bi-map"></i>
      <span>No active tactical map found.</span>
    </div>
  )
}

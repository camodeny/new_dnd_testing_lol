import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { ConvexGeometry } from 'three/examples/jsm/geometries/ConvexGeometry.js'

const FORWARD = new THREE.Vector3(0, 0, 1)
const CAMERA_FACE_NORMAL = new THREE.Vector3(0, 0.62, 0.78).normalize()
const CAMERA_SCREEN_UP = new THREE.Vector3(0, 1, -0.52).normalize()
const DIE_SCALE = 0.36
const DIE_LANDING_Y = -0.14
const DIE_LANDING_Z = 0
const DICE_STAGE_MARGIN = 0.78
const DICE_SPACING = 0.8
const MOBILE_DIE_SCALE = 0.27

const DIE_STYLES = {
  4: { base: '#374151', accent: '#f59e0b', emissive: '#312000' },
  6: { base: '#334155', accent: '#60a5fa', emissive: '#071c3f' },
  8: { base: '#2f3f35', accent: '#22c55e', emissive: '#052512' },
  10: { base: '#43313a', accent: '#fb7185', emissive: '#350817' },
  12: { base: '#3c344d', accent: '#c084fc', emissive: '#1d0835' },
  20: { base: '#33434a', accent: '#2dd4bf', emissive: '#042f2e' },
  100: { base: '#403a34', accent: '#fb923c', emissive: '#351505' },
}

function smoothstep(edge0, edge1, value) {
  const t = THREE.MathUtils.clamp((value - edge0) / (edge1 - edge0), 0, 1)
  return t * t * (3 - 2 * t)
}

function randomUnitVector() {
  return new THREE.Vector3(Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5).normalize()
}

function randomBetween(min, max) {
  return min + Math.random() * (max - min)
}

function getResponsiveDieScale(camera) {
  if (camera.aspect >= 1) return DIE_SCALE

  const portraitT = THREE.MathUtils.clamp((camera.aspect - 0.45) / 0.55, 0, 1)
  return THREE.MathUtils.lerp(MOBILE_DIE_SCALE, DIE_SCALE, portraitT)
}

function makeStoneTexture(style) {
  const canvas = document.createElement('canvas')
  const size = 256
  canvas.width = size
  canvas.height = size

  const context = canvas.getContext('2d')
  context.fillStyle = style.base
  context.fillRect(0, 0, size, size)

  for (let i = 0; i < 900; i += 1) {
    const alpha = Math.random() * 0.1
    context.fillStyle = Math.random() > 0.5
      ? `rgba(255, 255, 255, ${alpha})`
      : `rgba(0, 0, 0, ${alpha})`
    context.fillRect(Math.random() * size, Math.random() * size, 1 + Math.random() * 2, 1 + Math.random() * 2)
  }

  for (let i = 0; i < 9; i += 1) {
    context.beginPath()
    context.moveTo(Math.random() * size, Math.random() * size)
    const segments = 2 + Math.floor(Math.random() * 4)
    for (let j = 0; j < segments; j += 1) {
      context.lineTo(Math.random() * size, Math.random() * size)
    }
    context.strokeStyle = style.accent
    context.globalAlpha = 0.36
    context.lineWidth = 2 + Math.random() * 2
    context.shadowColor = style.accent
    context.shadowBlur = 8
    context.stroke()
  }

  context.globalAlpha = 1
  context.shadowBlur = 0

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = 4
  return texture
}

function getTextFontSize(text, highlighted = false) {
  if (text.length >= 3) return highlighted ? 96 : 76
  if (text.length === 2) return highlighted ? 122 : 96
  return highlighted ? 150 : 112
}

function makeTextTexture(text, highlighted = false) {
  const canvas = document.createElement('canvas')
  const size = 256
  canvas.width = size
  canvas.height = size

  const context = canvas.getContext('2d')
  context.clearRect(0, 0, size, size)
  context.textAlign = 'center'
  context.textBaseline = 'middle'
  context.font = `900 ${getTextFontSize(text, highlighted)}px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`
  context.lineJoin = 'round'
  context.strokeStyle = highlighted ? 'rgba(0, 0, 0, 0.98)' : 'rgba(0, 0, 0, 0.9)'
  context.lineWidth = highlighted ? 30 : 20
  context.strokeText(text, size / 2, size / 2 + 2)
  context.fillStyle = highlighted ? '#ffffff' : '#f8fafc'
  context.fillText(text, size / 2, size / 2 + 2)

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = 4
  return texture
}

function faceLabelSize(sides, text, highlighted = false) {
  const multiplier = highlighted ? 1.38 : 1
  if (sides === 6) return 0.42 * multiplier
  if (sides === 100) return 0.18 * (highlighted ? 1.85 : 1.05)
  if (sides === 10 && text.length > 1) return 0.32 * multiplier
  if (sides === 4 || sides === 8 || sides === 10) return 0.36 * multiplier
  if (sides === 12) return 0.3 * multiplier
  return 0.34 * multiplier
}


function createNumberLabel(text, face, sides, highlighted = false) {
  const size = faceLabelSize(sides, text, highlighted)
  const texture = makeTextTexture(text, highlighted)
  const material = new THREE.MeshBasicMaterial({
    map: texture,
    transparent: true,
    side: THREE.DoubleSide,
    depthWrite: false,
    polygonOffset: true,
    polygonOffsetFactor: -2,
    polygonOffsetUnits: -2,
  })
  const label = new THREE.Mesh(new THREE.PlaneGeometry(size, size), material)
  label.position.copy(face.center).addScaledVector(face.normal, highlighted ? 0.035 : 0.026)
  label.quaternion.setFromUnitVectors(FORWARD, face.normal)
  return label
}

function getOutwardTriangle(vertices, a, b, c) {
  const normal = new THREE.Vector3()
  const center = new THREE.Vector3()
  normal.crossVectors(
    new THREE.Vector3().subVectors(vertices[b], vertices[a]),
    new THREE.Vector3().subVectors(vertices[c], vertices[a]),
  ).normalize()
  center.addVectors(vertices[a], vertices[b]).add(vertices[c]).multiplyScalar(1 / 3)

  return normal.dot(center) < 0 ? [a, c, b] : [a, b, c]
}

function pushTriangle(positions, normals, vertices, vertexIndices, normal) {
  vertexIndices.forEach((vertexIndex) => {
    positions.push(...vertices[vertexIndex].toArray())
    normals.push(...normal.toArray())
  })
}

function getTriangleFace(geometry, triangleIndex, target = {}) {
  const position = geometry.attributes.position
  const index = geometry.index
  const a = new THREE.Vector3()
  const b = new THREE.Vector3()
  const c = new THREE.Vector3()
  const ab = new THREE.Vector3()
  const ac = new THREE.Vector3()

  const getVertex = (vertexIndex, vector) => vector.fromBufferAttribute(position, vertexIndex)
  const ia = index ? index.getX(triangleIndex * 3) : triangleIndex * 3
  const ib = index ? index.getX(triangleIndex * 3 + 1) : triangleIndex * 3 + 1
  const ic = index ? index.getX(triangleIndex * 3 + 2) : triangleIndex * 3 + 2

  getVertex(ia, a)
  getVertex(ib, b)
  getVertex(ic, c)

  target.normal = target.normal || new THREE.Vector3()
  target.center = target.center || new THREE.Vector3()
  target.normal.crossVectors(ab.subVectors(b, a), ac.subVectors(c, a)).normalize()
  target.center.addVectors(a, b).add(c).multiplyScalar(1 / 3)
  return target
}

function getLabelFaces(geometry, sides) {
  if (geometry.userData.labelFaces) return geometry.userData.labelFaces

  const triangleCount = geometry.index
    ? geometry.index.count / 3
    : geometry.attributes.position.count / 3
  const groups = []

  for (let i = 0; i < triangleCount; i += 1) {
    const face = getTriangleFace(geometry, i)
    let group = groups.find((candidate) => candidate.normal.dot(face.normal) > 0.999)

    if (!group) {
      group = { normal: face.normal.clone(), center: new THREE.Vector3(), count: 0 }
      groups.push(group)
    }

    group.center.add(face.center)
    group.count += 1
  }

  return groups
    .map((group) => ({
      normal: group.normal.normalize(),
      center: group.center.multiplyScalar(1 / group.count),
      count: group.count,
    }))
    .sort((a, b) => {
      if (Math.abs(b.normal.y - a.normal.y) > 0.001) return b.normal.y - a.normal.y
      return Math.atan2(a.normal.z, a.normal.x) - Math.atan2(b.normal.z, b.normal.x)
    })
    .slice(0, sides)
}

function createD10Geometry() {
  const radius = 0.85
  const height = 0.96
  const vertices = [
    new THREE.Vector3(0, height, 0),
    new THREE.Vector3(0, -height, 0),
  ]

  for (let i = 0; i < 10; i += 1) {
    const angle = (Math.PI * 2 * i) / 10
    const y = i % 2 === 0 ? 0.1014 : -0.1014
    vertices.push(new THREE.Vector3(Math.cos(angle) * radius, y, Math.sin(angle) * radius))
  }

  const positions = []
  const normals = []
  const labelFaces = []
  const edgeSegments = []
  const edgeKeys = new Set()

  const addEdgeSegment = (a, b) => {
    const key = [a, b].sort((left, right) => left - right).join(':')
    if (edgeKeys.has(key)) return
    edgeKeys.add(key)
    edgeSegments.push(vertices[a].clone(), vertices[b].clone())
  }

  for (let i = 0; i < 10; i += 1) {
    const pole = i % 2 === 0 ? 0 : 1
    const v1 = (i % 10) + 2
    const v2 = ((i + 1) % 10) + 2
    const v3 = ((i + 2) % 10) + 2
    const triangle1 = getOutwardTriangle(vertices, pole, v1, v2)
    const triangle2 = getOutwardTriangle(vertices, pole, v2, v3)

    const normal1 = new THREE.Vector3().crossVectors(
      new THREE.Vector3().subVectors(vertices[triangle1[1]], vertices[triangle1[0]]),
      new THREE.Vector3().subVectors(vertices[triangle1[2]], vertices[triangle1[0]]),
    ).normalize()
    const normal2 = new THREE.Vector3().crossVectors(
      new THREE.Vector3().subVectors(vertices[triangle2[1]], vertices[triangle2[0]]),
      new THREE.Vector3().subVectors(vertices[triangle2[2]], vertices[triangle2[0]]),
    ).normalize()

    const kiteNormal = normal1.clone().add(normal2).normalize()
    const kiteCenter = new THREE.Vector3()
      .add(vertices[pole])
      .add(vertices[v1])
      .add(vertices[v2])
      .add(vertices[v3])
      .multiplyScalar(0.25)

    pushTriangle(positions, normals, vertices, triangle1, kiteNormal)
    pushTriangle(positions, normals, vertices, triangle2, kiteNormal)
    labelFaces.push({ center: kiteCenter, normal: kiteNormal })

    addEdgeSegment(pole, v1)
    addEdgeSegment(pole, v3)
    addEdgeSegment(v1, v2)
    addEdgeSegment(v2, v3)
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3))
  geometry.computeBoundingSphere()
  geometry.userData.labelFaces = labelFaces
  geometry.userData.edgeSegments = edgeSegments
  geometry.userData.smoothNormals = true
  return geometry
}

function createD100Geometry() {
  const points = []
  const count = 52
  const radius = 0.94
  const goldenAngle = Math.PI * (3 - Math.sqrt(5))

  for (let i = 0; i < count; i += 1) {
    const y = 1 - (i / (count - 1)) * 2
    const radial = Math.sqrt(Math.max(0, 1 - y * y))
    const theta = i * goldenAngle
    points.push(new THREE.Vector3(
      Math.cos(theta) * radial * radius,
      y * radius,
      Math.sin(theta) * radial * radius,
    ))
  }

  const geometry = new ConvexGeometry(points)
  geometry.deleteAttribute('uv')
  geometry.computeVertexNormals()
  return geometry
}

function createDieGeometry(sides) {
  switch (sides) {
    case 4:
      return new THREE.TetrahedronGeometry(0.95, 0)
    case 6:
      return new THREE.BoxGeometry(1.25, 1.25, 1.25)
    case 8:
      return new THREE.OctahedronGeometry(0.98, 0)
    case 10:
      return createD10Geometry()
    case 100:
      return createD100Geometry()
    case 12:
      return new THREE.DodecahedronGeometry(0.92, 0)
    case 20:
    default:
      return new THREE.IcosahedronGeometry(0.96, 0)
  }
}

function createDieMesh(sides, value, options = {}) {
  const style = DIE_STYLES[options.styleSides] || DIE_STYLES[sides] || DIE_STYLES[20]
  const geometry = createDieGeometry(sides)
  const labelFaces = getLabelFaces(geometry, sides)
  const selectedFaceIndex = Math.max(
    0,
    Math.min(
      labelFaces.length - 1,
      options.selectedFaceIndex ?? ((value - 1) % labelFaces.length),
    ),
  )
  const material = new THREE.MeshStandardMaterial({
    color: '#f8fafc',
    map: makeStoneTexture(style),
    emissive: style.emissive,
    emissiveIntensity: 0.28,
    roughness: 0.82,
    metalness: 0.06,
    flatShading: !geometry.userData.smoothNormals,
  })
  const mesh = new THREE.Mesh(geometry, material)
  mesh.castShadow = true

  const edgeGeometry = geometry.userData.edgeSegments
    ? new THREE.BufferGeometry().setFromPoints(geometry.userData.edgeSegments)
    : new THREE.EdgesGeometry(geometry, 18)
  const edges = new THREE.LineSegments(
    edgeGeometry,
    new THREE.LineBasicMaterial({ color: '#f8fafc', transparent: true, opacity: 0.42 }),
  )

  const group = new THREE.Group()
  group.add(mesh)
  group.add(edges)
  group.scale.setScalar(options.dieScale || DIE_SCALE)

  labelFaces.forEach((face, index) => {
    const highlighted = index === selectedFaceIndex
    const faceValue = options.labels?.[index] ?? String(index + 1)
    group.add(createNumberLabel(faceValue, face, sides, highlighted))
  })

  return { group, resultFace: labelFaces[selectedFaceIndex] || labelFaces[0] }
}

function getRollVisualSpecs(roll) {
  const sides = roll?.sides || 20
  const values = roll?.rolls?.length ? roll.rolls : [roll?.result || sides]

  return values.map((value) => ({ sides, value }))
}

function disposeGroup(group) {
  group.traverse((child) => {
    if (child.geometry) child.geometry.dispose()
    if (child.material) {
      if (child.material.map) child.material.map.dispose()
      child.material.dispose()
    }
  })
}

function getLandingQuaternion(face, yaw = 0) {
  const labelQuaternion = new THREE.Quaternion().setFromUnitVectors(FORWARD, face.normal)
  const alignToCamera = new THREE.Quaternion().setFromUnitVectors(face.normal.clone().normalize(), CAMERA_FACE_NORMAL)
  const labelUp = new THREE.Vector3(0, 1, 0).applyQuaternion(labelQuaternion).applyQuaternion(alignToCamera)
  labelUp.projectOnPlane(CAMERA_FACE_NORMAL)
  labelUp.normalize()

  const desiredUp = CAMERA_SCREEN_UP.clone().projectOnPlane(CAMERA_FACE_NORMAL).normalize()
  const correction = Math.atan2(
    new THREE.Vector3().crossVectors(labelUp, desiredUp).dot(CAMERA_FACE_NORMAL),
    labelUp.dot(desiredUp),
  )
  const faceYaw = new THREE.Quaternion().setFromAxisAngle(CAMERA_FACE_NORMAL, correction + yaw)
  return faceYaw.multiply(alignToCamera)
}

function getResponsiveRollBounds(camera, count, dieScale = DIE_SCALE) {
  camera.updateMatrixWorld()

  const forward = new THREE.Vector3()
  camera.getWorldDirection(forward)

  const landingPoint = new THREE.Vector3(0, DIE_LANDING_Y, DIE_LANDING_Z)
  const distance = Math.max(0.1, landingPoint.sub(camera.position).dot(forward))
  const visibleHeight = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * distance
  const visibleHalfWidth = (visibleHeight * camera.aspect) / 2
  const stageMargin = Math.min(DICE_STAGE_MARGIN, dieScale * 1.55 + 0.24)
  const landingHalfWidth = Math.max(dieScale * 1.3, visibleHalfWidth - stageMargin)
  const spacing = count > 1
    ? Math.min(DICE_SPACING, (landingHalfWidth * 2) / Math.max(1, count - 1))
    : 0
  const spreadHalfWidth = ((count - 1) * spacing) / 2
  const centerHalfWidth = Math.max(0, landingHalfWidth - spreadHalfWidth)
  const offscreenOffset = Math.max(stageMargin + dieScale * 1.6, 0.78)

  return {
    spacing,
    centerHalfWidth,
    minX: -landingHalfWidth,
    maxX: landingHalfWidth,
    offscreenOffset,
  }
}

export default function DiceRollStage({ roll }) {
  const canvasRef = useRef(null)
  const sceneRef = useRef(null)
  const diceRef = useRef([])
  const animationRef = useRef(null)
  const rollRef = useRef(null)
  const [webglUnavailable, setWebglUnavailable] = useState(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined

    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
    } catch {
      setWebglUnavailable(true)
      return undefined
    }

    setWebglUnavailable(false)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    renderer.setClearColor(0x000000, 0)
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100)
    camera.position.set(0, 4.9, 6.25)
    camera.lookAt(0, -0.1, 0)

    const ambient = new THREE.AmbientLight('#e0f2fe', 1.55)
    scene.add(ambient)

    const key = new THREE.DirectionalLight('#ffffff', 2.4)
    key.position.set(3.5, 6, 5)
    key.castShadow = true
    key.shadow.mapSize.width = 2048
    key.shadow.mapSize.height = 2048
    key.shadow.camera.left = -10
    key.shadow.camera.right = 10
    key.shadow.camera.top = 10
    key.shadow.camera.bottom = -10
    scene.add(key)

    const rim = new THREE.PointLight('#2dd4bf', 1.65, 9)
    rim.position.set(-3.8, 1.8, 2.6)
    scene.add(rim)

    const shadowPlane = new THREE.Mesh(
      new THREE.PlaneGeometry(30, 22),
      new THREE.ShadowMaterial({ opacity: 0.28 }),
    )
    shadowPlane.rotation.x = -Math.PI / 2
    shadowPlane.position.y = -0.50
    shadowPlane.receiveShadow = true
    scene.add(shadowPlane)

    sceneRef.current = { scene, camera, renderer }

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const width = Math.max(1, Math.floor(rect.width))
      const height = Math.max(1, Math.floor(rect.height))
      renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
    }

    const observer = new ResizeObserver(resize)
    observer.observe(canvas)
    resize()

    const animate = (time) => {
      const activeRoll = rollRef.current
      const dice = diceRef.current

      dice.forEach((die, index) => {
        if (activeRoll) {
          const elapsed = time - activeRoll.startedAt
          const progress = Math.min(elapsed / activeRoll.duration, 1)
          const target = activeRoll.targets[index]

          // Phase-based horizontal movement (constant speed in air, decelerates after hitting floor at 0.65)
          const ease = progress < 0.65
            ? (progress / 0.65) * 0.65
            : 0.65 + (1 - 0.65) * Math.sin(((progress - 0.65) / (1 - 0.65)) * (Math.PI / 2))

          // 3-phase bouncing trajectory
          let bounce = 0
          if (progress < 0.65) {
            // First flight arc (falls to the ground)
            const p = progress / 0.65
            bounce = 0.7 * (1 - p * p) // starts at 0.7 units above floor
          } else if (progress < 0.82) {
            // First bounce
            const p = (progress - 0.65) / (0.82 - 0.65)
            bounce = 0.18 * 4 * p * (1 - p)
          } else if (progress < 0.93) {
            // Second bounce
            const p = (progress - 0.82) / (0.93 - 0.82)
            bounce = 0.05 * 4 * p * (1 - p)
          }

          die.position.x = THREE.MathUtils.lerp(target.startX, target.x, ease)
          die.position.y = target.y + bounce
          die.position.z = THREE.MathUtils.lerp(target.startZ, DIE_LANDING_Z, ease)
            + Math.sin(progress * Math.PI * 3 + index) * (1 - progress) * 0.08

          // Spin speed slows down significantly after first impact
          let spinAngle
          if (progress < 0.65) {
            spinAngle = progress * target.spinTurns * Math.PI * 2
          } else {
            const p = (progress - 0.65) / (1 - 0.65)
            spinAngle = 0.65 * target.spinTurns * Math.PI * 2
              + Math.sin(p * Math.PI / 2) * (target.spinTurns * 0.35) * Math.PI * 2
          }

          const spin = new THREE.Quaternion().setFromAxisAngle(target.spinAxis, spinAngle)
          const tumblingQuaternion = target.startQuaternion.clone().multiply(spin)

          // Alignment/settling only starts after first impact is done, finalizing near the end
          const settle = smoothstep(0.76, 0.93, progress)
          const landingWobble = Math.sin((progress - 0.76) * Math.PI * 8) * Math.pow(1 - settle, 2.0) * 0.22

          const wobbleQuaternion = new THREE.Quaternion().setFromAxisAngle(target.wobbleAxis, landingWobble)
          const settledQuaternion = target.finalQuaternion.clone().multiply(wobbleQuaternion)
          die.quaternion.slerpQuaternions(tumblingQuaternion, settledQuaternion, settle)

          if (progress >= 1) {
            die.position.set(target.x, target.y, DIE_LANDING_Z)
            die.quaternion.copy(target.finalQuaternion)
            die.userData.idleSpin = false
          }
        } else if (die.userData.idleSpin) {
          die.rotation.x += 0.006 + index * 0.001
          die.rotation.y += 0.008 + index * 0.001
        }
      })

      if (activeRoll && time - activeRoll.startedAt >= activeRoll.duration) {
        rollRef.current = null
      }

      renderer.render(scene, camera)
      animationRef.current = requestAnimationFrame(animate)
    }

    animationRef.current = requestAnimationFrame(animate)

    return () => {
      observer.disconnect()
      cancelAnimationFrame(animationRef.current)
      diceRef.current.forEach(disposeGroup)
      diceRef.current = []
      sceneRef.current = null
      rollRef.current = null
      renderer.dispose()
    }
  }, [])

  useEffect(() => {
    const context = sceneRef.current
    if (!context) return

    diceRef.current.forEach((die) => {
      context.scene.remove(die)
      disposeGroup(die)
    })

    if (!roll) {
      diceRef.current = []
      return
    }

    const visualSpecs = getRollVisualSpecs(roll)
    const count = Math.max(1, visualSpecs.length)
    let dieScale = getResponsiveDieScale(context.camera)
    
    // Scale up specifically for d100 to make it more legible
    const hasD100 = visualSpecs.some(spec => spec.sides === 100)
    if (hasD100) {
      dieScale *= 1.45
    }

    const rollBounds = getResponsiveRollBounds(context.camera, count, dieScale)
    const spacing = rollBounds.spacing
    const centerX = count === 1
      ? randomBetween(-rollBounds.centerHalfWidth, rollBounds.centerHalfWidth)
      : 0
    const centerZ = -0.65 + Math.random() * 1.35
    const targetXs = visualSpecs.map((_, index) => (
      THREE.MathUtils.clamp(
        centerX + (index - (count - 1) / 2) * spacing,
        rollBounds.minX,
        rollBounds.maxX,
      )
    ))

    // Dynamically calculate landing height to prevent clipping through the floor shadow plane
    const landingY = hasD100 ? -0.50 + (0.94 * dieScale) : DIE_LANDING_Y

    const dice = visualSpecs.map((spec, index) => {
      const { group: die, resultFace } = createDieMesh(spec.sides, spec.value, { ...spec, dieScale })
      die.position.set(targetXs[index], landingY, centerZ)
      die.rotation.set(0.7 + index * 0.4, 0.4 + index * 0.55, 0.25)
      die.userData.resultFace = resultFace
      die.userData.idleSpin = false
      context.scene.add(die)
      return die
    })

    diceRef.current = dice

    rollRef.current = {
      startedAt: performance.now(),
      duration: 1725,
      targets: dice.map((die, index) => {
        const x = targetXs[index]
        const spinAxis = randomUnitVector()
        const spinTurns = 5.25 + Math.random() * 2.25
        const startQuaternion = new THREE.Quaternion().setFromEuler(new THREE.Euler(
          Math.random() * Math.PI,
          Math.random() * Math.PI,
          Math.random() * Math.PI,
        ))
        const finalQuaternion = getLandingQuaternion(
          die.userData.resultFace,
          -0.05 + index * 0.1 + Math.random() * 0.08,
        )
        const fromLeft = Math.random() > 0.5

        return {
          x,
          y: landingY,
          startX: fromLeft
            ? rollBounds.minX - rollBounds.offscreenOffset - Math.random() * 0.24
            : rollBounds.maxX + rollBounds.offscreenOffset + Math.random() * 0.24,
          startZ: centerZ + 1.2 + Math.random() * 0.9,
          spinAxis,
          wobbleAxis: new THREE.Vector3(0.5 + Math.random() * 0.25, 0, 0.55 + Math.random() * 0.25).normalize(),
          spinTurns,
          startQuaternion,
          finalQuaternion,
        }
      }),
    }
  }, [roll])

  if (webglUnavailable) {
    return (
      <div className="dice-roll-fallback" aria-hidden="true">
        <span>3D dice preview unavailable in this browser.</span>
      </div>
    )
  }

  return <canvas ref={canvasRef} className="dice-roll-canvas" aria-hidden="true" />
}

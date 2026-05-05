import { useEffect, useRef } from 'react'
import * as THREE from 'three'

const FORWARD = new THREE.Vector3(0, 0, 1)
const CAMERA_FACE_NORMAL = new THREE.Vector3(0, 0.62, 0.78).normalize()
const CAMERA_SCREEN_UP = new THREE.Vector3(0, 1, -0.52).normalize()
const DIE_SCALE = 0.68

const DIE_STYLES = {
  4: { base: '#374151', accent: '#f59e0b', emissive: '#312000' },
  6: { base: '#334155', accent: '#60a5fa', emissive: '#071c3f' },
  8: { base: '#2f3f35', accent: '#22c55e', emissive: '#052512' },
  10: { base: '#43313a', accent: '#fb7185', emissive: '#350817' },
  12: { base: '#3c344d', accent: '#c084fc', emissive: '#1d0835' },
  20: { base: '#33434a', accent: '#2dd4bf', emissive: '#042f2e' },
  100: { base: '#403a34', accent: '#fb923c', emissive: '#351505' },
}

function easeOutCubic(value) {
  return 1 - Math.pow(1 - value, 3)
}

function randomUnitVector() {
  return new THREE.Vector3(Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5).normalize()
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

function makeTextTexture(text, sides, highlighted = false) {
  const canvas = document.createElement('canvas')
  const size = 256
  canvas.width = size
  canvas.height = size

  const context = canvas.getContext('2d')
  context.clearRect(0, 0, size, size)
  context.textAlign = 'center'
  context.textBaseline = 'middle'
  context.font = `900 ${highlighted ? 150 : sides === 100 ? 92 : 112}px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`
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

function faceLabelSize(sides, highlighted = false) {
  const multiplier = highlighted ? 1.38 : 1
  if (sides === 6) return 0.42 * multiplier
  if (sides === 4 || sides === 8 || sides === 10 || sides === 100) return 0.36 * multiplier
  if (sides === 12) return 0.3 * multiplier
  return 0.34 * multiplier
}

function createNumberLabel(text, face, sides, highlighted = false) {
  const size = faceLabelSize(sides, highlighted)
  const texture = makeTextTexture(text, sides, highlighted)
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
    .slice(0, sides === 100 ? 10 : sides)
}

function createD10Geometry() {
  const radius = 0.82
  const height = 1.12
  const vertices = [
    new THREE.Vector3(0, height, 0),
    new THREE.Vector3(0, -height, 0),
  ]

  for (let i = 0; i < 10; i += 1) {
    const angle = (Math.PI * 2 * i) / 10
    const y = i % 2 === 0 ? 0.18 : -0.18
    vertices.push(new THREE.Vector3(Math.cos(angle) * radius, y, Math.sin(angle) * radius))
  }

  const indices = []
  const labelFaces = []

  for (let i = 0; i < 10; i += 1) {
    const current = i + 2
    const next = ((i + 1) % 10) + 2

    indices.push(0, next, 1)
    indices.push(0, 1, current)

    const center = new THREE.Vector3()
      .add(vertices[0])
      .add(vertices[1])
      .add(vertices[current])
      .add(vertices[next])
      .multiplyScalar(0.25)
    const normal = new THREE.Vector3()
      .crossVectors(
        new THREE.Vector3().subVectors(vertices[next], vertices[0]),
        new THREE.Vector3().subVectors(vertices[1], vertices[0]),
      )
      .normalize()

    if (normal.dot(center) < 0) {
      normal.multiplyScalar(-1)
    }

    labelFaces.push({ center, normal })
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices.flatMap((v) => v.toArray()), 3))
  geometry.setIndex(indices)
  geometry.computeVertexNormals()
  geometry.userData.labelFaces = labelFaces
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
    case 100:
      return createD10Geometry()
    case 12:
      return new THREE.DodecahedronGeometry(0.92, 0)
    case 20:
    default:
      return new THREE.IcosahedronGeometry(0.96, 0)
  }
}

function createDieMesh(sides, value) {
  const style = DIE_STYLES[sides] || DIE_STYLES[20]
  const geometry = createDieGeometry(sides)
  const labelFaces = getLabelFaces(geometry, sides)
  const selectedFaceIndex = Math.max(0, Math.min(labelFaces.length - 1, (value - 1) % labelFaces.length))
  const material = new THREE.MeshStandardMaterial({
    color: '#f8fafc',
    map: makeStoneTexture(style),
    emissive: style.emissive,
    emissiveIntensity: 0.28,
    roughness: 0.82,
    metalness: 0.06,
    flatShading: true,
  })
  const mesh = new THREE.Mesh(geometry, material)
  mesh.castShadow = true

  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 18),
    new THREE.LineBasicMaterial({ color: '#f8fafc', transparent: true, opacity: 0.42 }),
  )

  const group = new THREE.Group()
  group.add(mesh)
  group.add(edges)
  group.scale.setScalar(DIE_SCALE)

  labelFaces.forEach((face, index) => {
    const highlighted = index === selectedFaceIndex
    const faceValue = sides === 100
      ? String(index === selectedFaceIndex ? value : (index * 10)).padStart(2, '0')
      : String(index + 1)
    group.add(createNumberLabel(faceValue, face, sides, highlighted))
  })

  return { group, resultFace: labelFaces[selectedFaceIndex] || labelFaces[0] }
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

export default function DiceRollStage({ roll }) {
  const canvasRef = useRef(null)
  const sceneRef = useRef(null)
  const diceRef = useRef([])
  const animationRef = useRef(null)
  const rollRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
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
    shadowPlane.position.y = -0.78
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
          const ease = easeOutCubic(progress)
          const bounce = Math.abs(Math.sin(progress * Math.PI * 5.4)) * Math.pow(1 - progress, 1.18) * 0.78
          const target = activeRoll.targets[index]

          die.position.x = THREE.MathUtils.lerp(target.startX, target.x, ease)
          die.position.y = target.y + bounce + (1 - progress) * 0.3
          die.position.z = THREE.MathUtils.lerp(target.startZ, 0, ease)
            + Math.sin(progress * Math.PI * 3 + index) * (1 - progress) * 0.12

          if (progress < 0.78) {
            const spin = new THREE.Quaternion().setFromAxisAngle(
              target.spinAxis,
              progress * target.spinTurns * Math.PI * 2,
            )
            die.quaternion.copy(target.startQuaternion).multiply(spin)
          } else {
            const settleProgress = easeOutCubic((progress - 0.78) / 0.22)
            die.quaternion.slerpQuaternions(target.midQuaternion, target.finalQuaternion, settleProgress)
          }

          if (progress >= 1) {
            die.position.set(target.x, target.y, 0)
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

    const sides = roll?.sides || 20
    const count = Math.max(1, roll?.rolls?.length || 1)
    const spacing = count > 1 ? 1.45 : 0
    const centerX = count === 1 ? -1.9 + Math.random() * 3.8 : 0
    const centerZ = -0.65 + Math.random() * 1.35
    const dice = Array.from({ length: count }, (_, index) => {
      const value = roll?.rolls?.[index] || 20
      const { group: die, resultFace } = createDieMesh(sides, value)
      die.position.set(centerX + (index - (count - 1) / 2) * spacing, -0.08, centerZ)
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
        const x = centerX + (index - (count - 1) / 2) * spacing
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
        const settleSpin = new THREE.Quaternion().setFromAxisAngle(spinAxis, 0.78 * spinTurns * Math.PI * 2)
        const fromLeft = Math.random() > 0.5

        return {
          x,
          y: -0.08,
          startX: x + (fromLeft ? -6.2 : 6.2) + (Math.random() - 0.5) * 0.9,
          startZ: centerZ + 1.6 + Math.random() * 1.2,
          spinAxis,
          spinTurns,
          startQuaternion,
          midQuaternion: startQuaternion.clone().multiply(settleSpin),
          finalQuaternion,
        }
      }),
    }
  }, [roll])

  return <canvas ref={canvasRef} className="dice-roll-canvas" aria-hidden="true" />
}

/**
 * Campaign table: snapshot `table` projection types plus the deterministic,
 * plain-language helpers the table UI renders.
 *
 * The projection is already filtered server-side for the viewer as a player;
 * nothing here decides visibility or rules. Helpers only turn authoritative
 * values into words a first-time player understands.
 */

import type { PlayerRollForRealtime } from '@/lib/realtime'
import type { Message } from '@/types'

export interface ProjectionError {
  error: string
}

export type HealthLabel = 'unhurt' | 'hurt' | 'badly hurt' | 'down' | 'unknown'

export interface TablePartyMember {
  character_id: string
  user_id: string
  name: string
  race?: string | null
  level?: number | null
  class_label?: string | null
  is_self: boolean
  health: HealthLabel | null
  conditions: string[]
  error?: string
}

export interface TableResource {
  name: string
  current: number
  max: number
  recharge?: string | null
}

export interface TableAttack {
  name: string
  to_hit?: number | null
  damage?: string | null
  damage_type?: string | null
}

export interface TableSkill {
  name: string
  modifier: number
  proficient: boolean
}

export interface TableRollModifier {
  modifier: number
  label: string
}

export interface TableCharacter extends TablePartyMember {
  hp?: { current: number; max: number; temp: number }
  armor_class?: number
  speed?: number
  resources?: TableResource[]
  attacks?: TableAttack[]
  skills?: TableSkill[]
  /** Server-derived modifiers for the viewer's pending rolls, by request id. */
  roll_modifiers?: Record<string, TableRollModifier>
}

export interface TableScene {
  location_name?: string | null
  fictional_time?: string | null
}

export interface TableFact {
  id: string
  content: string
  created_at?: string | null
}

export interface TablePerson {
  entity_id: string
  name: string
  role?: string | null
  summary?: string | null
  facts: TableFact[]
}

export interface TableJournal {
  people: TablePerson[]
  facts: TableFact[]
}

export interface EncounterParticipant {
  id: string
  kind: string
  display_name: string
  controller_user_id?: string | null
  character_id?: string | null
  initiative_status?: string
  sort_order?: number | null
}

export interface TurnResources {
  action_available: boolean
  bonus_action_available: boolean
  reaction_available: boolean
  movement_remaining: number | null
  movement_max: number | null
}

export interface EncounterMapZone {
  id: string
  kind: 'blocked' | 'difficult' | 'open' | string
  rect: { col: number; row: number; width: number; height: number }
}

export interface EncounterMapPlacement {
  participant_id: string
  col: number
  row: number
  display_name?: string
  kind?: string
}

export interface TableEncounter {
  id: string
  status: string
  round?: number | null
  active_participant_id?: string | null
  participants: EncounterParticipant[]
  turn?: { round?: number; active_participant_id?: string | null; resources?: Record<string, TurnResources> } | null
  map?: { width: number; height: number; zones: EncounterMapZone[]; placements: EncounterMapPlacement[] } | null
  reachable?: { cells: Array<{ col: number; row: number; cost_feet: number }> } | null
  my_pending_initiative?: string[]
}

/** A shop the current scene references. Stock and buying arrive with #464. */
export interface TableShop {
  entity_id: string
  name: string
  summary?: string | null
}

/** Player-lane table projection (`snapshot.table`). Each section may carry
 *  `{ error }` independently when its server projection failed closed. */
export interface TableProjection {
  character: TableCharacter | ProjectionError | null
  party: TablePartyMember[] | ProjectionError
  scene: TableScene | ProjectionError | null
  journal: TableJournal | ProjectionError
  encounter: TableEncounter | ProjectionError | null
  shops?: TableShop[]
}

export function isProjectionError(value: unknown): value is ProjectionError {
  return typeof value === 'object' && value !== null && 'error' in value
}

/** Section value when present and healthy, else null. */
export function healthy<T>(value: T | ProjectionError | null | undefined): T | null {
  if (value == null || isProjectionError(value)) return null
  return value
}

export function activeEncounter(table: TableProjection | null): TableEncounter | null {
  const encounter = healthy(table?.encounter)
  return encounter && encounter.status === 'active' ? encounter : null
}

// ── Plain language ─────────────────────────────────────────────────────────

export function signed(n: number): string {
  return n >= 0 ? `+${n}` : String(n)
}

const HEALTH_SENTENCE: Record<HealthLabel, string> = {
  unhurt: 'You’re in good shape.',
  hurt: 'You’re hurt but steady.',
  'badly hurt': 'You’re badly hurt. Be careful.',
  down: 'You’re down and need help fast.',
  unknown: '',
}

export function healthLabelFor(hp: { current: number; max: number } | undefined): HealthLabel {
  if (!hp || hp.max <= 0) return 'unknown'
  if (hp.current <= 0) return 'down'
  if (hp.current >= hp.max) return 'unhurt'
  return hp.current / hp.max > 0.5 ? 'hurt' : 'badly hurt'
}

/** One-line status for the character tab. */
export function statusSentence(character: TableCharacter): string {
  const parts = [HEALTH_SENTENCE[healthLabelFor(character.hp)]]
  const recover = (character.resources ?? []).find((r) => r.current > 0 && /heal|wind|breath/i.test(r.name))
  if (recover && healthLabelFor(character.hp) !== 'unhurt') {
    parts.push(`You can still use ${recover.name} to recover a little.`)
  }
  if (character.conditions.length) parts.push(`You’re ${joinWords(character.conditions.map((c) => c.toLowerCase()))}.`)
  return parts.filter(Boolean).join(' ')
}

export function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join('')
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
}

export function rechargeSentence(recharge: string | null | undefined): string | null {
  const key = String(recharge ?? '').toLowerCase().replace(/[\s-]+/g, '_')
  if (!key) return null
  if (key.includes('short')) return 'Comes back after a short rest.'
  if (key.includes('long')) return 'Comes back after a long rest.'
  if (key.includes('dawn') || key.includes('day')) return 'Comes back each day.'
  return `Recharges: ${String(recharge).replace(/_/g, ' ')}.`
}

/** What a 5e condition means, in a sentence. Static rules copy. */
const CONDITION_MEANING: Record<string, string> = {
  blinded: 'You can’t see. Attacks against you are easier, and yours are harder.',
  charmed: 'You won’t attack whoever charmed you, and they have sway over you.',
  deafened: 'You can’t hear.',
  exhaustion: 'You’re worn out. Everything is a little harder until you rest.',
  frightened: 'You’re harder pressed while you can see what scares you, and can’t move closer to it.',
  grappled: 'Something is holding you. You can’t move until you break free.',
  incapacitated: 'You can’t take actions or reactions.',
  invisible: 'No one can see you. Your attacks are easier, and attacks against you are harder.',
  paralyzed: 'You can’t move or act. Hits against you are especially dangerous.',
  petrified: 'You’ve been turned to stone.',
  poisoned: 'Your attacks and checks are harder until it wears off.',
  prone: 'You’re on the ground. Getting up costs half your movement.',
  restrained: 'You can’t move, and you’re easier to hit.',
  stunned: 'You can’t move or act, and you’re easier to hit.',
  unconscious: 'You’re out cold and can’t do anything.',
}

export function conditionMeaning(name: string): string | null {
  return CONDITION_MEANING[name.toLowerCase()] ?? null
}

export function healthText(label: HealthLabel | null): string {
  if (!label || label === 'unknown') return ''
  return label === 'down' ? 'down' : label
}

/** Plain-language instructions for a requested roll. */
export function rollInstructions(roll: PlayerRollForRealtime, mod: TableRollModifier | undefined): string {
  const base = mod
    ? `Roll a 20-sided die and add ${signed(mod.modifier)} for ${mod.label}.`
    : 'Roll a 20-sided die and add your bonus.'
  if (roll.advantage_state === 'advantage') return `${base} You have advantage: roll twice and keep the higher one.`
  if (roll.advantage_state === 'disadvantage') return `${base} You have disadvantage: roll twice and keep the lower one.`
  return base
}

// ── Dice ───────────────────────────────────────────────────────────────────

/** Fair d20 rolls from the platform CSPRNG (rejection sampling, no bias). */
export function rollD20s(count: number, random: (buf: Uint32Array) => Uint32Array = (b) => crypto.getRandomValues(b)): number[] {
  const out: number[] = []
  const limit = Math.floor(0x100000000 / 20) * 20
  while (out.length < count) {
    const [value] = random(new Uint32Array(1))
    if (value < limit) out.push((value % 20) + 1)
  }
  return out
}

export function rollDiceCount(advantage: string): number {
  return advantage === 'advantage' || advantage === 'disadvantage' ? 2 : 1
}

export function keptDie(rolls: number[], advantage: string): number {
  if (advantage === 'advantage') return Math.max(...rolls)
  if (advantage === 'disadvantage') return Math.min(...rolls)
  return rolls[0]
}

// ── Timeline ───────────────────────────────────────────────────────────────

export type TimelineItem =
  | { kind: 'message'; key: string; at: number; message: Message }
  | { kind: 'roll'; key: string; at: number; roll: PlayerRollForRealtime; who: string; mine: boolean }

/** Chat messages interleaved with public roll results, oldest first. */
export function buildTimeline(
  messages: Message[],
  rolls: PlayerRollForRealtime[],
  party: TablePartyMember[],
  userId: string | null | undefined,
): TimelineItem[] {
  const items: TimelineItem[] = messages.map((message) => ({
    kind: 'message', key: `m:${message.id}`, at: Date.parse(message.created_at), message,
  }))
  for (const roll of rolls) {
    const total = roll.fulfillment?.total
    if (roll.status !== 'fulfilled' || typeof total !== 'number' || !roll.fulfilled_at) continue
    const mine = Boolean(userId) && roll.requested_user_id === userId
    const who = party.find((p) => p.character_id === roll.character_id)?.name ?? 'Someone'
    items.push({ kind: 'roll', key: `r:${roll.id}`, at: Date.parse(roll.fulfilled_at), roll, who, mine })
  }
  return items.sort((a, b) => a.at - b.at || a.key.localeCompare(b.key))
}

// ── Side panel tabs ────────────────────────────────────────────────────────

/** Tabs about you are always present; contextual tabs (`shop:<id>`) exist
 *  only while the scene has that place in it. */
export type PanelTab = 'character' | 'journal' | 'party' | `shop:${string}`

export function shopTab(shop: TableShop): PanelTab {
  return `shop:${shop.entity_id}`
}

/** Party only matters with company; shops appear while they're here. */
export function panelTabs(multiplayer: boolean, shops: TableShop[] = []): PanelTab[] {
  const base: PanelTab[] = multiplayer ? ['character', 'journal', 'party'] : ['character', 'journal']
  return [...base, ...shops.map(shopTab)]
}

export function orderedParticipants(encounter: TableEncounter): EncounterParticipant[] {
  return [...encounter.participants].sort(
    (a, b) => (a.sort_order ?? Number.MAX_SAFE_INTEGER) - (b.sort_order ?? Number.MAX_SAFE_INTEGER),
  )
}

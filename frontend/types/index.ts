export interface User {
  id: string
  username: string
  email?: string
}

export interface Campaign {
  id: string
  name: string
  description?: string
  random_seed?: string
  loot_mode?: string
  required_players?: number
  revision: number
  created_at: string
  updated_at?: string
  owner_id: string
  status?: string
  session_count?: number
  member_count?: number
}

export interface Character {
  id: string
  name: string
  race?: string
  background?: string
  alignment?: string
  level?: number
  hit_points?: number
  armor_class?: number
  strength?: number
  dexterity?: number
  constitution?: number
  intelligence?: number
  wisdom?: number
  charisma?: number
  classes?: CharacterClass[]
  created_at?: string
}

export interface CharacterClass {
  class_name: string
  level: number
  subclass?: string
}

export interface Session {
  id: string
  campaign_id: string
  status: string
  created_at: string
  ended_at?: string
  recap?: string
}

export interface Message {
  id: string
  session_id: string
  role: 'player' | 'dm' | 'system'
  content: string
  created_at: string
  sender_name?: string
  is_ic?: boolean
}

export interface CampaignMember {
  user_id: string
  username: string
  email?: string
  role: 'owner' | 'player'
  character_id?: string | null
  selected_character_id?: string | null
  character_name?: string | null
  is_ready?: boolean
  ready_at?: string | null
  character_valid?: boolean
  character_progress?: { completed: number; total: number; percent: number }
  character_missing?: string[]
}

export interface LobbyEligibility {
  eligible: boolean
  blockers: string[]
}

// Public party composition — issue #244. Explicitly public fields only;
// private character lore is never part of this projection.
export interface PartyCompositionEntry {
  user_id: string
  is_ready: boolean
  character_id: string | null
  character_name: string | null
  race: string | null
  classes: string[]
  level: number | null
}

export interface PartyComposition {
  size: number
  ready_count: number
  class_counts: Record<string, number>
  members: PartyCompositionEntry[]
}

export interface CharacterLore {
  id: string
  campaign_id: string
  character_id: string
  user_id: string
  version: number
  has_content: boolean
  content?: string
  created_at?: string | null
  updated_at?: string | null
}

// Private lore-DM setup thread: one player's guided back-and-forth per
// character. DM replies are advisory only — proposals become canon solely
// through the standard lore write.
export interface LoreDmMessage {
  id?: string
  role: 'user' | 'assistant'
  content: string
  proposal?: string
  created_at?: string | null
}

// Lobby invitations — issue #242. Owner list entries carry the full
// record; lobby projections mask emails for non-owners; lookup returns
// only the minimal safe pre-membership metadata.
export interface CampaignInvite {
  id?: string
  campaign_id: string
  // Bearer credential — present in owner views only; lobby projections for
  // non-owners omit it (see lobby_invite_projection).
  code?: string
  invite_url?: string
  invite_url_path?: string
  status: 'active' | 'revoked'
  usable?: boolean
  intended_email?: string | null
  intended_email_hint?: string | null
  recipient_label?: string | null
  expires_at?: string | null
  revoked_at?: string | null
  accepted_count?: number
  last_delivery_status?: string | null
  last_delivery_error?: string | null
  created_at?: string | null
}

export interface InviteLookup {
  code: string
  campaign_id: string
  campaign_name: string
  campaign_status: string
  required_players: number
  member_count: number
  seats_remaining: number
  usable: boolean
  unusable_reason?: string | null
  expires_at?: string | null
}

export interface CampaignThreadMember {
  thread_id: string
  user_id: string
  role: 'member'
  joined_at?: string
}

export interface CampaignThread {
  id: string
  campaign_id: string
  thread_type: 'campaign' | 'private'
  private_kind?: 'dm' | 'direct' | null
  title?: string | null
  created_by?: string | null
  created_at?: string | null
  members?: CampaignThreadMember[]
}

export interface CampaignWorld {
  id: string
  campaign_id: string
  public_intro?: string
  world_state?: string
  created_at?: string
}

export interface EncounterMap {
  id: string
  campaign_id: string
  name?: string
  width?: number
  height?: number
  is_active?: boolean
  placements?: MapPlacement[]
  initiative_order?: InitiativeEntry[]
  current_turn_actor_id?: string
}

export interface MapPlacement {
  actor_type: string
  actor_id: string
  col: number
  row: number
  name?: string
  hp?: number
  max_hp?: number
}

export interface InitiativeEntry {
  actor_type: string
  actor_id: string
  initiative: number
  name?: string
}

export interface ApiError extends Error {
  status?: number
  data?: unknown
}

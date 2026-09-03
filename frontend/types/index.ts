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
  role: 'owner' | 'player'
  character_id?: string
  character_name?: string
  is_ready?: boolean
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

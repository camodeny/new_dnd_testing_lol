# VTT + AI DM Improvement Plan

## Context

The current VTT already supports direct player token movement with pathfinding, turn-aware movement limits, and terrain validation.

- The frontend computes reachable cells and previews movement.
- The backend revalidates movement against speed, terrain, and turn state.
- This is the right model for player characters: players should continue moving their own tokens directly.

The main gap is on the AI-DM side and in the overall board feel:

- AI-controlled movement is still closer to coordinate placement than real tactical movement.
- The backend stores more tactical metadata than the frontend exposes.
- The board does not yet feel like a live battlefield being actively run by the AI DM.

## Goals

- Keep player self-movement direct and responsive.
- Make the AI DM authoritative for enemies, NPCs, hazards, doors, fog, reinforcements, and tactical effects.
- Surface more of the existing tactical map data to players.
- Make encounter state changes feel immediate and readable.

## Current Strengths

- Player token movement already works well enough as a baseline.
- Encounter maps already have grid calibration, terrain zones, obstacles, spawn boxes, and tactical notes.
- Combat state already tracks initiative and basic action resources.
- The AI DM already has tools for map generation, placement, and turn progression.

## Main Problems

### 1. AI-side board control is too primitive

The AI mostly has placement-style tools, not proper tactical board actions.

- It can place actors on the map.
- It can advance turns and update action resources.
- It does not yet have a first-class movement tool that behaves like real tactical movement.
- It does not yet control persistent board objects and effects in a structured way.

### 2. The frontend underuses the existing map metadata

The backend already stores:

- Terrain zones
- Obstacles
- Cover semantics
- Hazards
- Chokepoints
- Tactical notes

But most of that is either hidden or only used for movement validation.

### 3. The board lacks tabletop feedback loops

The current experience is missing several things that make a VTT feel alive:

- AI movement trails
- Pings
- Target markers
- Inspectable cells
- Clear tactical overlays
- Board event markers
- Real-time state changes

### 4. Map updates are not yet a true live tactical stream

The encounter map is still refreshed with polling rather than being fully driven by streamed board events.

## Product Direction

### Player Characters

Players should:

- Move their own tokens directly
- See where they can move
- Measure distances
- Inspect cells and terrain
- Ping locations
- Select targets
- Understand what the AI DM just changed on the board

Players should not:

- Directly control enemy, NPC, door, fog, or hazard state
- Act as a human DM or campaign controller

### AI DM

The AI DM should:

- Move enemies and NPCs tactically
- Open and close doors
- Reveal and hide map areas
- Place hazards and temporary effects
- Spawn reinforcements
- Update conditions and status markers
- Drive encounter transitions and battlefield events

## Proposed Work

## Phase 1: AI Tactical Board Actions

### Objective

Make the AI DM capable of running the board as a real tactical authority instead of mostly teleporting things around.

### Backend changes

Add a first-class movement tool for AI-controlled actors.

Suggested capability:

- `move_encounter_actor`

It should:

- Move an NPC or monster using the same pathfinding rules as player movement
- Respect blocked and difficult terrain
- Consume movement during combat
- Record the path taken
- Enforce turn state when relevant

Add first-class board object state.

Suggested capability:

- `update_map_object_state`

Objects should support state such as:

- `open` / `closed`
- `locked`
- `broken`
- `lit`
- `triggered`
- `hp`
- `blocks_movement`
- `blocks_los`

Add first-class board effect state.

Suggested capabilities:

- `create_map_effect`
- `clear_map_effect`

Effects should include things like:

- Spell areas
- Temporary hazards
- Smoke or fire zones
- Difficult terrain patches
- Markers for active tactical effects

### Data model additions

Expand token/combatant state beyond position.

Suggested fields:

- `size`
- `elevation`
- `faction`
- `status_markers`
- `conditions`
- `concentration`
- `is_hidden`
- `is_off_map`
- `is_reserve`
- `token_style`

Expand encounter state to support:

- Conditions and durations
- Concentration source
- Legendary and lair actions
- Delayed or skipped turns
- Temporary combat effects
- Board event history

### Outcome

This phase makes the AI DM feel like it is actually running the battlefield.

## Phase 2: Player-Facing Tactical Overlays

### Objective

Expose the tactical map information that already exists in backend data.

### Frontend changes

Add overlay toggles for:

- Difficult terrain
- Hazards
- Cover
- Doors
- Chokepoints
- Elevation

Add tile inspection.

When a player hovers or clicks a cell, show:

- Coordinate
- Terrain type
- Whether it is blocked
- Whether it is difficult terrain
- Cover quality if relevant
- Active effects on that cell

Add a compact tactical legend that explains the board language.

Add clearer movement feedback:

- Why a move is blocked
- Which terrain increased movement cost
- What the route cost was

### Outcome

This phase makes the board easier to read and makes existing map semantics feel valuable.

## Phase 3: Live Encounter Feedback

### Objective

Make board changes feel immediate and theatrical.

### Frontend changes

Add:

- Pings
- Target markers
- Focus rings
- AI movement trails
- "recently changed" highlights
- Small tactical event feed separate from prose chat

Example event feed items:

- Goblin moved
- South door opened
- Fire spreads near hearth
- Reinforcements entered from alley
- Aria is now poisoned

### Transport changes

Move encounter-map updates from polling to streamed events.

Suggested event types:

- `map_token_moved`
- `map_object_updated`
- `map_effect_created`
- `map_effect_cleared`
- `map_fog_updated`
- `map_turn_advanced`
- `map_condition_changed`

### Outcome

This phase makes the VTT feel active instead of intermittently refreshed.

## Phase 4: Fog, Visibility, and Hidden Information

### Objective

Support more encounter types than open combat in a fully visible room.

### Backend changes

Add visibility state:

- Revealed cells
- Hidden cells
- Shared party vision
- AI-private hidden areas
- Last-known enemy positions

Add fog controls for the AI DM:

- `reveal_map_area`
- `hide_map_area`

### Frontend changes

Show:

- Revealed areas
- Newly revealed transitions
- Hidden but previously seen areas if desired
- Last-known enemy markers only when the rules support it

### Outcome

This phase allows stealth, ambush, scouting, and gradual reveals to feel much better.

## Phase 5: Encounter Objectives and Triggers

### Objective

Make encounters feel like dynamic scenarios instead of static slugfests.

### Backend changes

Add structured objective and trigger data:

- Defend zone
- Escape zone
- Countdown timers
- Reinforcement waves
- Trigger tiles
- Alarm states
- Patrol routes
- Neutral crowd behavior

Map generation and setup should produce structured tactical scripting where appropriate.

### Frontend changes

Show:

- Objective markers
- Timers
- Trigger warnings when revealed
- Reinforcement entry indicators

### Outcome

This phase makes the AI DM better at running scenario-style encounters.

## Phase 6: Combat Readability and Polish

### Objective

Make combat state easier to understand at a glance.

### Frontend changes

Add token-level and tracker-level display for:

- Conditions
- Concentration
- HP state
- Active turn emphasis
- Reaction spent
- Movement remaining
- Threat/faction clarity

Add lightweight area-template previews for:

- Circles
- Cones
- Lines

Add measurement and range tools for players.

### Outcome

This phase makes the board feel much more like a complete VTT.

## Recommended Build Order

### First slice

Build these first:

1. AI movement tool using current pathfinding rules
2. Streamed encounter-map events
3. Tactical overlays for terrain, hazards, cover, and doors
4. Tile inspection and better movement explanations

This is the highest-leverage slice because it strengthens both:

- AI DM battlefield control
- Player understanding of the board

### Second slice

Build next:

1. Board object state
2. Temporary tactical effects
3. Pings and target markers
4. Tactical event feed

### Third slice

Build after that:

1. Fog and reveal state
2. Objectives and triggers
3. Reinforcements and event scripting

## Suggested Backend Deliverables

- New map action APIs or AI tools for movement, object state, and effects
- Richer encounter state schema
- Persistent map object records
- Persistent map effect records
- Streamed board-event payloads
- Optional visibility/fog state model

## Suggested Frontend Deliverables

- Overlay system for tactical metadata
- Cell inspect UI
- Tactical legend
- Event feed
- Pings and target markers
- AI movement/path animations
- Real-time streamed state application
- Fog and reveal rendering

## Notes on Scope

This should not turn into a human-DM control panel.

The design goal is:

- Players act directly where it feels natural
- The AI DM remains the only battlefield authority
- The board becomes legible, reactive, and theatrical

## Summary

The player movement foundation is already correct.

The biggest improvements now are:

1. Give the AI DM real tactical board actions
2. Expose the tactical metadata that already exists
3. Make board changes stream live and feel dramatic
4. Add fog, effects, and objectives so encounters feel more dynamic

If implemented in that order, the VTT should start feeling much more like an AI-run tactical battlefield rather than a generated map with drag movement.

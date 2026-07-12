export const mockEncounterMaps = {
  split: {
    id: 'e2e-map',
    title: 'Forgotten Tomb crypt',
    columns: 12,
    rows: 12,
    grid: {
      columns: 12,
      rows: 12,
      cell_size_px: { average: 50 },
      confidence: 0.98
    },
    setup_status: 'ready',
    placements: [
      {
        id: 'p1',
        actor_type: 'player',
        actor_id: 'e2e-character',
        label: 'E2E Mocked Character',
        col: 2,
        row: 3
      },
      {
        id: 'p2',
        actor_type: 'npc',
        actor_id: 'n1',
        label: 'Ally Guard',
        col: 4,
        row: 3
      },
      {
        id: 'p3',
        actor_type: 'monster',
        actor_id: 'm1',
        label: 'Crypt Wraith',
        col: 6,
        row: 5
      }
    ],
    vtt_setup: {
      map_summary: 'A dusty ancient crypt with pillars and rubble.',
      dm_setup_context: 'Pillars provide cover. The sarcophagus blocks movement.',
      friendly_spawn_boxes: [],
      enemy_spawn_boxes: [],
      terrain_zones: [],
      obstacles: [],
      tactical_notes: ['Pillars (Half Cover)', 'Wraith Spawn Area']
    },
    encounter_state: {
      active: true,
      round: 2,
      active_turn_index: 0,
      turn_order: [
        {
          actor_type: 'player',
          actor_id: 'e2e-character',
          placement_id: 'p1',
          label: 'E2E Mocked Character',
          initiative: 18
        },
        {
          actor_type: 'npc',
          actor_id: 'n1',
          placement_id: 'p2',
          label: 'Ally Guard',
          initiative: 15
        },
        {
          actor_type: 'monster',
          actor_id: 'm1',
          placement_id: 'p3',
          label: 'Crypt Wraith',
          initiative: 12
        }
      ]
    }
  },

  fullscreen: {
    id: 'e2e-map-combat',
    title: 'Forgotten Tomb crypt - Fullscreen VTT',
    columns: 12,
    rows: 12,
    grid: {
      columns: 12,
      rows: 12,
      cell_size_px: { average: 50 },
      confidence: 0.98
    },
    setup_status: 'ready',
    placements: [
      {
        id: 'p1',
        actor_type: 'player',
        actor_id: 'e2e-character',
        label: 'E2E Mocked Character',
        col: 2,
        row: 3
      },
      {
        id: 'p3',
        actor_type: 'monster',
        actor_id: 'm1',
        label: 'Crypt Wraith',
        col: 6,
        row: 5
      }
    ],
    encounter_state: {
      active: true,
      round: 2,
      active_turn_index: 0,
      turn_order: [
        {
          actor_type: 'player',
          actor_id: 'e2e-character',
          placement_id: 'p1',
          label: 'E2E Mocked Character',
          initiative: 18
        },
        {
          actor_type: 'monster',
          actor_id: 'm1',
          placement_id: 'p3',
          label: 'Crypt Wraith',
          initiative: 12
        }
      ]
    },
    vtt_setup: {
      map_summary: 'Combat in the Crypt',
      dm_setup_context: 'Fierce battle.',
      friendly_spawn_boxes: [],
      enemy_spawn_boxes: [],
      terrain_zones: [],
      obstacles: [],
      tactical_notes: ['Wraith has high ground']
    }
  },

  tactical: {
    id: 'e2e-map-tactical',
    title: 'Forgotten Tomb crypt - Tactical Overlay',
    columns: 12,
    rows: 12,
    grid: {
      columns: 12,
      rows: 12,
      cell_size_px: { average: 50 },
      confidence: 0.98
    },
    setup_status: 'ready',
    placements: [
      {
        id: 'p1',
        actor_type: 'player',
        actor_id: 'e2e-character',
        label: 'E2E Mocked Character',
        col: 2,
        row: 3
      }
    ],
    vtt_setup: {
      map_summary: 'Tactical layout of the crypt showing pillars and a deep pool.',
      dm_setup_context: 'Pillars and pool features.',
      friendly_spawn_boxes: [
        {
          label: 'Spawn Zone',
          rect: { col: 0, row: 0, width: 2, height: 2 },
          description: 'Where the party enters.',
          confidence: 1.0
        }
      ],
      enemy_spawn_boxes: [],
      terrain_zones: [
        {
          kind: 'water',
          label: 'Deep Pool',
          shape_type: 'rect',
          rect: { col: 4, row: 4, width: 3, height: 3 },
          description: 'Difficult terrain (deep water).',
          confidence: 0.95
        }
      ],
      obstacles: [
        {
          label: 'Stone Pillar',
          kind: 'wall',
          shape_type: 'rect',
          rect: { col: 1, row: 1, width: 1, height: 1 },
          polygon: [],
          movement_effect: 'blocks_movement',
          cover_type: 'full',
          description: 'A solid stone column supporting the ceiling.',
          confidence: 0.99
        }
      ],
      tactical_notes: ['Stone Pillar (Blocks movement & line of sight)', 'Deep Pool (Costs extra movement)']
    },
    encounter_state: {
      active: true,
      round: 2,
      active_turn_index: 0,
      turn_order: [
        {
          actor_type: 'player',
          actor_id: 'e2e-character',
          placement_id: 'p1',
          label: 'E2E Mocked Character',
          initiative: 18
        }
      ]
    }
  }
};

export const mockMessages = {
  mixed: [
    {
      id: 'm1',
      session_id: 'e2e-session',
      user_id: 2,
      username: 'DM',
      role: 'dm',
      content: 'The old stone door groans as you push it open, revealing a dusty crypt. In the center, a dark stone sarcophagus is surrounded by glyphs.',
      created_at: new Date(Date.now() - 600000).toISOString()
    },
    {
      id: 'm2',
      session_id: 'e2e-session',
      user_id: 1,
      username: 'E2E Test User',
      role: 'player',
      content: 'I approach the sarcophagus cautiously. "Is there any magic radiating from the glyphs?"',
      created_at: new Date(Date.now() - 500000).toISOString()
    },
    {
      id: 'm3',
      session_id: 'e2e-session',
      user_id: 1,
      username: 'E2E Test User',
      role: 'player',
      content: '[Roll: Arcana Check] total: 18 | rolls: 14 | mod: 4 | sides: 20',
      created_at: new Date(Date.now() - 400000).toISOString()
    },
    {
      id: 'm3_system',
      session_id: 'e2e-session',
      user_id: null,
      username: 'System',
      role: 'system',
      content: 'Combat has started.',
      created_at: new Date(Date.now() - 350000).toISOString()
    },
    {
      id: 'm4',
      session_id: 'e2e-session',
      user_id: 2,
      username: 'DM',
      role: 'dm',
      content: '<npc name="Ghostly Voice">Who dares disturb the rest of the Archmage?</npc>\nA translucent figure emerges from the floor.',
      created_at: new Date(Date.now() - 300000).toISOString()
    },
    {
      id: 'm5',
      session_id: 'e2e-session',
      user_id: 1,
      username: 'E2E Test User',
      role: 'player',
      content: 'I draw my shortsword and prepare for battle.',
      created_at: new Date(Date.now() - 200000).toISOString()
    }
  ],
  thinking: [
    {
      id: 'mt1',
      session_id: 'e2e-session',
      user_id: 1,
      username: 'E2E Test User',
      role: 'player',
      content: 'I cast Firebolt at the translucent figure.',
      created_at: new Date(Date.now() - 100000).toISOString()
    }
  ]
};

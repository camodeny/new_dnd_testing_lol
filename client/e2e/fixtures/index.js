import { mockUser } from './base-user.js';
import { mockCampaigns } from './campaigns.js';
import { mockCharacters } from './characters.js';
import { mockSessions } from './sessions.js';
import { mockMessages } from './messages.js';
import { mockEncounterMaps } from './encounter-maps.js';
import { mockProposals } from './proposals.js';

export {
  mockUser,
  mockCampaigns,
  mockCharacters,
  mockSessions,
  mockMessages,
  mockEncounterMaps,
  mockProposals
};

export const fixtureProfiles = {
  'campaigns-list': {
    me: mockUser,
    campaigns: [mockCampaigns[0]],
    characters: []
  },
  'characters-list': {
    me: mockUser,
    campaigns: [mockCampaigns[0]],
    characters: mockCharacters
  },
  'automation-home': {
    me: mockUser,
    campaigns: [mockCampaigns[0]],
    characters: []
  },
  'design-lab': {
    me: mockUser
  },
  'session-chat-mixed': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: {
      ...mockSessions['e2e-session'],
      messages: mockMessages.mixed
    },
    proposals: mockProposals,
    encounterMap: null
  },
  'session-chat-thinking': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: {
      ...mockSessions['e2e-session'],
      messages: mockMessages.thinking
    },
    proposals: [],
    encounterMap: null
  },
  'session-map-split': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: mockSessions['e2e-session'],
    proposals: [],
    encounterMap: mockEncounterMaps.split
  },
  'session-map-fullscreen': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: mockSessions['e2e-session'],
    proposals: [],
    encounterMap: mockEncounterMaps.fullscreen
  },
  'session-map-tactical': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: mockSessions['e2e-session'],
    proposals: [],
    encounterMap: mockEncounterMaps.tactical
  },
  'session-map-movement': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: mockSessions['e2e-session'],
    proposals: [],
    encounterMap: mockEncounterMaps.tactical // We can reuse the tactical map setup for movement range
  },
  'session-roster': {
    me: mockUser,
    campaign: mockCampaigns[1],
    characters: mockCharacters,
    session: mockSessions['e2e-session'],
    proposals: [],
    encounterMap: mockEncounterMaps.split // To show roster tab panel (which renders on split view in map panel)
  }
};

export const mockCampaigns = [
  {
    id: 'e2e-campaign',
    name: 'E2E Mocked Campaign',
    description: 'A stable campaign for browser evidence test checks.',
    active_session: null,
    user_id: 1,
  },
  {
    id: 'active-combat-campaign',
    name: 'E2E Combat Campaign',
    description: 'A stable campaign with an active combat session.',
    user_id: 1,
    active_session: {
      id: 'e2e-session',
      campaign_id: 'active-combat-campaign',
      status: 'active',
      created_at: new Date().toISOString()
    }
  }
];

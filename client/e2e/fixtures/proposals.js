export const mockProposals = [
  {
    id: 'prop-1',
    status: 'pending',
    reason: 'Acquired a Potion of Healing',
    created_at: new Date(Date.now() - 50000).toISOString(),
    changes: [
      {
        label: 'Potion of Healing',
        operation: 'add',
        before: 0,
        after: 1
      }
    ]
  }
];

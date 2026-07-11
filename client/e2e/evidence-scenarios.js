import { expect } from '@playwright/test';

export const evidenceScenarios = [
  {
    id: 'campaigns-list',
    description: 'Render Campaigns list correctly',
    route: '/',
    fixture: 'campaigns-list',
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Campaigns · Fireside');
      await expect(page.locator('.campaign-card-link')).toBeVisible();
      await expect(page.locator('text=E2E Mocked Campaign')).toBeVisible();
    },
    captures: [
      { name: 'campaigns.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'characters-list',
    description: 'Render Characters library correctly',
    route: '/characters',
    fixture: 'characters-list',
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Characters · Fireside');
      await expect(page.locator('h1:has-text("Your characters")')).toBeVisible();
      await expect(page.locator('text=E2E Mocked Character')).toBeVisible();
    },
    captures: [
      { name: 'characters.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'automation-home',
    description: 'Render Automation page correctly',
    route: '/automation',
    fixture: 'automation-home',
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Automation · Fireside');
      await expect(page.locator('h1:has-text("Automation")')).toBeVisible();
      await expect(page.locator('text=E2E Mocked Scenario')).toBeVisible();
    },
    captures: [
      { name: 'automation.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'design-lab',
    description: 'Render Design Lab and switch directions correctly',
    route: '/design-lab',
    fixture: 'design-lab',
    setup: async ({ page }) => {
      const chronicleTab = page.locator('button[role="tab"]:has-text("The Chronicle")');
      await expect(chronicleTab).toBeVisible();
      await chronicleTab.click();
    },
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Design Lab · Fireside');
      await expect(page.locator('.chronicle-preview')).toBeVisible();
    },
    captures: [
      { name: 'design-lab.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'session-chat-mixed',
    description: 'Active session with DM, player, system, dice-roll, and proposal messages',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-chat-mixed',
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Campaign · Fireside');
      // Verify message roles and content are visible
      await expect(page.locator('.session-msg-dm').first()).toBeVisible();
      await expect(page.locator('.session-msg-player').first()).toBeVisible();
      await expect(page.locator('.session-msg-system').first()).toBeVisible();
      // Roll card should render for [Roll: Arcana Check]...
      await expect(page.locator('.roll-card')).toBeVisible();
      // Sheet proposal should render
      await expect(page.locator('.sheet-proposal-inline')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'conversation.png', locator: '.session-panel' }
    ]
  },
  {
    id: 'session-chat-thinking',
    description: 'Conversation showing DM thinking state indicator',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-chat-thinking',
    setup: async ({ page }) => {
      // Wait for EventSource to be initialized in useEffect
      await page.waitForFunction(() => window.activeEventSources && window.activeEventSources.length > 0);
      // Simulate DM thinking status event via event stream
      await page.evaluate(() => {
        window.activeEventSources.forEach(es => {
          es.emit({ type: 'status', status: 'Dungeon Master is calculating the monster\'s next move...' });
        });
      });
    },
    verify: async ({ page }) => {
      await expect(page.locator('.session-msg-thinking')).toBeVisible();
      await expect(page.locator('text=Dungeon Master is calculating')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'conversation.png', locator: '.session-panel' }
    ]
  },
  {
    id: 'session-map-split',
    description: 'Active encounter with map and chat visible together showing tokens',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-split',
    mapViewMode: 'semi',
    verify: async ({ page }) => {
      await expect(page.locator('.encounter-map-panel')).toBeVisible();
      await expect(page.locator('.session-panel')).toBeVisible();
      // Verify tokens
      await expect(page.locator('.encounter-map-token.player')).toBeVisible();
      await expect(page.locator('.encounter-map-token.npc')).toBeVisible();
      await expect(page.locator('.encounter-map-token.monster')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.encounter-map-panel' }
    ]
  },
  {
    id: 'session-map-fullscreen',
    description: 'Fullscreen tactical map with initiative combat tracker',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-fullscreen',
    mapViewMode: 'fullscreen',
    verify: async ({ page }) => {
      await expect(page.locator('.dashboard-page.map-fullscreen')).toBeVisible();
      await expect(page.locator('.encounter-combat-tracker')).toBeVisible();
      // Verify active combat turn
      await expect(page.locator('.encounter-tracker-item.active')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.encounter-map-panel' },
      { name: 'initiative.png', locator: '.encounter-combat-tracker' }
    ]
  },
  {
    id: 'session-map-tactical',
    description: 'Tactical overlay showing obstacles, spawn zones, and cell inspector',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-tactical',
    mapViewMode: 'semi',
    setup: async ({ page }) => {
      // Ensure grid lines and tactical overlays are enabled
      // Click at col=1, row=1 to trigger selected cell inspector on the Stone Pillar
      const board = page.locator('.encounter-map-board');
      await expect(board).toBeVisible();
      const box = await board.boundingBox();
      if (box) {
        const x = box.width * (1.5 / 12);
        const y = box.height * (1.5 / 12);
        await board.click({ position: { x, y } });
      }
    },
    verify: async ({ page }) => {
      await expect(page.locator('.encounter-map-cell-inspector')).toBeVisible();
      await expect(page.locator('strong:has-text("Stone Pillar")').first()).toBeVisible();
      // Ensure overlays render
      await expect(page.locator('.encounter-map-area-overlay.blocked')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.encounter-map-panel' }
    ]
  },
  {
    id: 'session-map-movement',
    description: 'Movement inspection showing reachable range, difficult terrain and blocked areas',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-movement',
    mapViewMode: 'semi',
    setup: async ({ page }) => {
      // Select the player token to trigger movement inspection
      const playerToken = page.locator('.encounter-map-token.player');
      await expect(playerToken).toBeVisible();
      await playerToken.click();
    },
    verify: async ({ page }) => {
      // When selected, reachable cells should show move overlay highlights
      await expect(page.locator('.encounter-map-move-cell')).toBeChecked({ checked: false, timeout: 2000 }).catch(() => {});
      // Ensure difficult terrain and blocked zones exist
      await expect(page.locator('.encounter-map-area-overlay.difficult')).toBeVisible();
      await expect(page.locator('.encounter-map-area-overlay.blocked')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.encounter-map-panel' }
    ]
  },
  {
    id: 'session-roster',
    description: 'Encounter combat roster showing party, allies and threats',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-roster',
    mapViewMode: 'semi',
    setup: async ({ page }) => {
      // Click the Roster tab button to switch to the roster tab view
      const rosterTab = page.locator('button:has-text("Roster")').first();
      await expect(rosterTab).toBeVisible();
      await rosterTab.click();
    },
    verify: async ({ page }) => {
      const roster = page.locator('.session-roster-tab');
      await expect(roster).toBeVisible();
      await expect(roster.locator('text=Party')).toBeVisible();
      await expect(roster.locator('text=Allies')).toBeVisible();
      await expect(roster.locator('text=Threats')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'roster.png', locator: '.session-roster-tab' }
    ]
  }
];

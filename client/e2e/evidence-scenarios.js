import { expect } from '@playwright/test';

export const evidenceScenarios = [
  {
    id: 'design-lab-story-atlas',
    description: 'Design Lab Story + Atlas workspace renders correctly',
    route: '/design-lab',
    fixture: 'design-lab',
    setup: async ({ page }) => {
      // Select Story + Atlas direction (default, but explicitly click to prove the UX works)
      const storyAtlasTab = page.locator('button[role="tab"]:has-text("Story + Atlas")');
      await expect(storyAtlasTab).toBeVisible();
      await storyAtlasTab.click();
    },
    verify: async ({ page }) => {
      await expect(page).toHaveTitle('Design Lab · Fireside');
      await expect(page.locator('.story-atlas-workspace-container')).toBeVisible();
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
      // Verify workspace is visible
      await expect(page.locator('.story-atlas-workspace')).toBeVisible();
      // Verify message roles and content are visible
      await expect(page.locator('.story-atlas-workspace .session-msg-dm').first()).toBeVisible();
      await expect(page.locator('.story-atlas-workspace .session-msg-player').first()).toBeVisible();
      await expect(page.locator('.story-atlas-workspace .session-msg-system').first()).toBeVisible();
      // Roll card should render for [Roll: Arcana Check]
      await expect(page.locator('.story-atlas-workspace .roll-card').first()).toBeVisible();
      // Sheet proposal should render
      await expect(page.locator('.story-atlas-workspace .sheet-proposal-inline').first()).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'conversation.png', locator: '.sampler-main' }
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
      await expect(page.locator('.story-atlas-workspace .sampler-thinking').first()).toBeVisible();
      await expect(page.locator('.story-atlas-workspace :text("Dungeon Master is calculating")').first()).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'conversation.png', locator: '.sampler-main' }
    ]
  },
  {
    id: 'session-map-split',
    description: 'Active encounter with map and chat visible together showing tokens',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-split',
    setup: async ({ page }) => {
      // Toggle to Map tab
      const mapTab = page.locator('nav.atlas-hybrid-tabs button:has-text("Map"):visible, .mobile-bottom-nav button:has-text("Map"):visible').first();
      await expect(mapTab).toBeVisible();
      await mapTab.click();
    },
    verify: async ({ page }) => {
      await expect(page.locator('.sampler-map')).toBeVisible();
      const isMobile = await page.evaluate(() => window.innerWidth <= 720);
      if (!isMobile) {
        await expect(page.locator('.atlas-feed')).toBeVisible();
      }
      // Verify tokens
      await expect(page.locator('.encounter-map-token.player')).toBeVisible();
      await expect(page.locator('.encounter-map-token.npc')).toBeVisible();
      await expect(page.locator('.encounter-map-token.monster')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.sampler-map' }
    ]
  },
  {
    id: 'session-map-fullscreen',
    description: 'Fullscreen tactical map with initiative combat tracker',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-fullscreen',
    setup: async ({ page }) => {
      // Toggle to Map tab
      const mapTab = page.locator('nav.atlas-hybrid-tabs button:has-text("Map"):visible, .mobile-bottom-nav button:has-text("Map"):visible').first();
      await expect(mapTab).toBeVisible();
      await mapTab.click();
    },
    verify: async ({ page }) => {
      await expect(page.locator('.atlas-workspace')).toBeVisible();
      await expect(page.locator('.encounter-combat-tracker')).toBeVisible();
      // Verify active combat turn
      await expect(page.locator('.encounter-tracker-item.active')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.sampler-map' },
      { name: 'initiative.png', locator: '.encounter-combat-tracker' }
    ]
  },
  {
    id: 'session-map-tactical',
    description: 'Tactical overlay showing obstacles, spawn zones, and cell inspector',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-tactical',
    setup: async ({ page }) => {
      // Switch to Map tab
      const mapTab = page.locator('nav.atlas-hybrid-tabs button:has-text("Map"):visible, .mobile-bottom-nav button:has-text("Map"):visible').first();
      await expect(mapTab).toBeVisible();
      await mapTab.click();
      await page.waitForTimeout(500);
      
      // Click at col=1, row=1 to trigger selected cell inspector on the Stone Pillar
      const board = page.locator('.encounter-map-board');
      await expect(board).toBeVisible();
      const box = await board.boundingBox();
      if (box) {
        const x = box.width * (1.5 / 12);
        const y = box.height * (1.5 / 12);
        await board.click({ position: { x, y }, force: true });
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
      { name: 'map.png', locator: '.sampler-map' }
    ]
  },
  {
    id: 'session-map-movement',
    description: 'Movement inspection showing reachable range, difficult terrain and blocked areas',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-movement',
    setup: async ({ page }) => {
      // Switch to Map tab
      const mapTab = page.locator('nav.atlas-hybrid-tabs button:has-text("Map"):visible, .mobile-bottom-nav button:has-text("Map"):visible').first();
      await expect(mapTab).toBeVisible();
      await mapTab.click();
      await page.waitForTimeout(500);

      // Hold-drag the player token to trigger persistent movement inspection.
      const playerToken = page.locator('.encounter-map-token.player');
      await expect(playerToken).toBeVisible();
      await playerToken.dispatchEvent('pointerdown', { pointerId: 1, button: 0 });
      await page.waitForTimeout(100);
      const board = page.locator('.encounter-map-board');
      const box = await board.boundingBox();
      if (box) {
        const endX = box.width * (4.5 / 12);
        const endY = box.height * (4.5 / 12);
        await page.mouse.move(box.x + endX, box.y + endY, { steps: 10 });
      }
    },
    verify: async ({ page }) => {
      // When selected, reachable cells should show move overlay highlights
      await expect(page.locator('.encounter-map-move-cell').first()).toBeVisible();
      // Verify pathfinding marks difficult reachable cells
      await expect(page.locator('.encounter-map-move-cell.difficult').first()).toBeVisible();
      // Ensure blocked zones exist
      await expect(page.locator('.encounter-map-area-overlay.blocked')).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'map.png', locator: '.sampler-map' }
    ]
  },
  {
    id: 'session-state-retention',
    description: 'Verify transient composer text state retention when toggling view between Story and Map',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-map-split',
    setup: async ({ page }) => {
      // Switch to Story tab first (since it defaults to Map)
      const storyTab = page.locator('.atlas-header nav.atlas-hybrid-tabs button:has-text("Story"):visible, .mobile-bottom-nav button:has-text("Story"):visible').first();
      await expect(storyTab).toBeVisible();
      await storyTab.click();
      
      // Type in story composer
      const composerInput = page.locator('.story-atlas-workspace .session-input-editable');
      await expect(composerInput).toBeVisible();
      await composerInput.fill('Transient state check');
      
      // Toggle to Map
      const mapTab = page.locator('header.sampler-topbar nav button:has-text("Map"):visible, .mobile-bottom-nav button:has-text("Map"):visible').first();
      await expect(mapTab).toBeVisible();
      await mapTab.click();
      
      // Switch back to Story
      const storyTabBack = page.locator('.atlas-header nav.atlas-hybrid-tabs button:has-text("Story"):visible, .mobile-bottom-nav button:has-text("Story"):visible').first();
      await expect(storyTabBack).toBeVisible();
      await storyTabBack.click();
    },
    verify: async ({ page }) => {
      const composerInput = page.locator('.story-atlas-workspace .session-input-editable');
      await expect(composerInput).toHaveText('Transient state check');
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'session-spectator-readonly',
    description: 'Verify spectator mode provides read-only state composer with warning message',
    route: '/campaigns/active-combat-campaign',
    fixture: 'session-spectator-readonly',
    verify: async ({ page }) => {
      // Composer area should still render
      await expect(page.locator('.story-atlas-workspace .session-input-area')).toBeVisible();
      // Should show the read-only banner instead of an editable input
      await expect(page.locator('.story-atlas-workspace .session-spectator-banner')).toBeVisible();
      await expect(page.locator('.story-atlas-workspace .session-spectator-banner')).toContainText('spectator', { ignoreCase: true });
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true }
    ]
  },
  {
    id: 'automation-run-scorecard',
    description: 'Automation run page shows scorecard summary with audited and fully-scored cycle counts',
    route: '/automation/runs/42',
    fixture: 'automation-run',
    verify: async ({ page }) => {
      // Header shows the run id and status
      await expect(page.locator('.automation-title')).toContainText('Run #42');
      // Scorecard summary meta shows audited and fully scored cycle counts
      await expect(page.locator('.automation-meta-grid').first()).toContainText('Audited cycles');
      await expect(page.locator('.automation-meta-grid').first()).toContainText('Fully scored cycles');
      await expect(page.locator('.automation-meta-grid').first()).toContainText('Severity');
      await expect(page.locator('.automation-meta-grid').first()).toContainText('Completeness');
      // Stat grid header shows the audited / fully scored cycle ratio
      await expect(page.locator('.automation-stat-grid')).toContainText('Audited / Fully scored cycles');
      await expect(page.locator('.automation-stat-grid')).toContainText('3 / 2');
      // Category breakdown renders
      await expect(page.locator('.automation-category-breakdown')).toBeVisible();
      // Scorecard rows render with statuses
      await expect(page.locator('.automation-scorecard-row')).toHaveCount(4);
      // Live session transcript renders
      await expect(page.locator('.automation-chat-column .chat-msg-dm').first()).toBeVisible();
      await expect(page.locator('.automation-chat-column .chat-msg-player').first()).toBeVisible();
    },
    captures: [
      { name: 'page.png', locator: null, fullPage: true },
      { name: 'status.png', locator: '.automation-header' },
      { name: 'scorecard.png', locator: '.automation-scorecard' }
    ]
  }
];
import { expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { fixtureProfiles, mockCampaigns, mockAutomationRunWatch } from './fixtures/index.js';

export async function setupBrowserEvidence(page, baseURL) {
  const unexpectedApiRequests = [];
  const pageErrors = [];
  const consoleErrors = [];
  const failedRequests = [];

  const scenarioId = process.env.PLAYWRIGHT_SCENARIO_ID || 'campaigns-list';
  const fixtureKey = process.env.PLAYWRIGHT_SCENARIO_FIXTURE || scenarioId;
  const profile = fixtureProfiles[fixtureKey] || fixtureProfiles['campaigns-list'];

  // Mock standard and custom endpoints based on the selected fixture profile
  await page.route(
    (url) => new URL(url).pathname.startsWith('/api/'),
    async (route) => {
      const url = route.request().url();
      const pathname = new URL(url).pathname;

      if (pathname === '/api/me') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ user: profile.me || { id: 1, name: 'E2E Test User', email: 'e2e@example.com' } }),
        });
      } else if (pathname === '/api/campaigns') {
        const campaignsList = profile.campaigns || (profile.campaign ? [profile.campaign] : []);
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ campaigns: campaignsList }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/characters')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ characters: profile.characters || [] }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/world')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ world: { current_scene: 'Tomb Crypt Entrance', public_intro: { title: 'Dungeon of the Archmage' } } }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/members')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ members: profile.members || [{ user_id: 1, username: 'E2E Test User', role: 'player' }] }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/encounter-maps/current')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ encounter_map: profile.encounterMap || null }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/lootboxes')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ loot_boxes: [], lootboxes: [] }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/shops')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ shops: [] }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/planning')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ planning: { messages: [], all_ready: false } }),
        });
      } else if (pathname.startsWith('/api/campaigns/') && pathname.endsWith('/llm-players')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ llm_players: [] }),
        });
      } else if (/^\/api\/campaigns\/[^/]+$/.test(pathname)) {
        // GET /api/campaigns/:id
        const camp = profile.campaign || mockCampaigns[0];
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ campaign: camp }),
        });
      } else if (pathname.startsWith('/api/sessions/') && pathname.endsWith('/proposals')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ sheet_proposals: profile.proposals || [] }),
        });
      } else if (/^\/api\/sessions\/[^/]+$/.test(pathname)) {
        // GET /api/sessions/:id
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ session: profile.session || null }),
        });
      } else if (pathname.startsWith('/api/encounter-maps/') && (pathname.endsWith('/image') || pathname.endsWith('/labeled-image'))) {
        const imagePath = path.resolve(process.cwd(), 'e2e/assets/test-map.png');
        await route.fulfill({
          status: 200,
          contentType: 'image/png',
          body: fs.readFileSync(imagePath),
        });
      } else if (pathname === '/api/characters') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ characters: profile.characters || [] }),
        });
      } else if (pathname === '/api/automation') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            scenarios: [
              {
                id: 'e2e-scenario',
                name: 'E2E Mocked Scenario',
                source_campaign_id: 1,
              },
            ],
            active_runs: [],
            recent_failures: [],
            source_campaigns: [
              { id: 1, name: 'E2E Mocked Campaign' },
            ],
            scenario_trends: [],
          }),
        });
      } else if (/^\/api\/automation\/runs\/[^/]+\/provider-calls(\?|$)/.test(pathname)) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ provider_calls: [] }),
        });
      } else if (/^\/api\/automation\/runs\/[^/]+(\?|$)/.test(pathname)) {
        // GET /api/automation/runs/:id (run watch payload)
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(profile.runWatch || mockAutomationRunWatch),
        });
      } else {
        unexpectedApiRequests.push(url);
        await route.abort();
      }
    }
  );

  // EventSource stub for streaming (robustified for simulation)
  await page.addInitScript(() => {
    window.EventSource = class DummyEventSource {
      constructor(url) {
        this.url = url;
        this.readyState = 1; // OPEN
        if (!window.activeEventSources) {
          window.activeEventSources = [];
        }
        window.activeEventSources.push(this);
      }
      addEventListener() {}
      removeEventListener() {}
      close() {}
      emit(data) {
        if (this.onmessage) {
          this.onmessage({ data: JSON.stringify(data) });
        }
      }
    };
  });

  // Track uncaught page errors
  page.on('pageerror', (err) => {
    pageErrors.push(err);
  });

  // Track console errors
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      console.error('BROWSER ERROR:', msg.text());
      consoleErrors.push(msg.text());
    }
  });

  // Track same-origin failed requests
  page.on('requestfailed', (request) => {
    const url = request.url();
    if (url.startsWith(baseURL)) {
      failedRequests.push(`${url}: ${request.failure()?.errorText || 'Unknown failure'}`);
    }
  });

  // Track same-origin HTTP response errors
  page.on('response', (response) => {
    const url = response.url();
    if (url.startsWith(baseURL) && response.status() >= 400) {
      failedRequests.push(`${url}: HTTP ${response.status()}`);
    }
  });

  return {
    async verifyNoErrors() {
      expect(unexpectedApiRequests).toEqual([]);
      expect(pageErrors).toEqual([]);
      expect(consoleErrors).toEqual([]);
      expect(failedRequests).toEqual([]);
    },
    async takeScreenshot(name, locator = null) {
      const viewportPreset = process.env.PLAYWRIGHT_VIEWPORT || 'desktop';
      const viewportSuffix = viewportPreset === 'mobile' ? '-mobile' : '';
      const screenshotPath = path.resolve(
        process.cwd(),
        '../review-evidence/browser-screenshots',
        scenarioId + viewportSuffix,
        name
      );
      const dir = path.dirname(screenshotPath);
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
      }

      if (locator) {
        await page.locator(locator).screenshot({ path: screenshotPath });
      } else {
        await page.screenshot({ path: screenshotPath, fullPage: true });
      }
    }
  };
}

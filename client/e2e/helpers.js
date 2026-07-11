import { expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';

export async function setupBrowserEvidence(page, baseURL) {
  const unexpectedApiRequests = [];
  const pageErrors = [];
  const consoleErrors = [];
  const failedRequests = [];

  // Mock standard endpoints
  await page.route(
    (url) => new URL(url).pathname.startsWith('/api/'),
    async (route) => {
      const url = route.request().url();
      const pathname = new URL(url).pathname;

    if (pathname === '/api/me') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ user: { id: 1, name: 'E2E Test User', email: 'e2e@example.com' } }),
      });
    } else if (pathname === '/api/campaigns') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          campaigns: [
            {
              id: 'e2e-campaign',
              name: 'E2E Mocked Campaign',
              description: 'A stable campaign for browser evidence test checks.',
            },
          ],
        }),
      });
    } else if (pathname === '/api/characters') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          characters: [
            {
              id: 'e2e-character',
              name: 'E2E Mocked Character',
              race: 'Elf',
              alignment: 'Chaotic Good',
              total_level: 5,
              combat: { current_hp: 42, max_hp: 42, armor_class: 16 },
              ability_scores: {
                strength: 10,
                dexterity: 18,
                constitution: 14,
                intelligence: 12,
                wisdom: 16,
                charisma: 8,
              },
            },
          ],
        }),
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
    } else {
      unexpectedApiRequests.push(url);
      await route.abort();
    }
  });

  // EventSource stub for streaming
  await page.addInitScript(() => {
    window.EventSource = class DummyEventSource {
      constructor(url) {
        this.url = url;
        this.readyState = 0; // CONNECTING
      }
      addEventListener() {}
      removeEventListener() {}
      close() {}
    };
  });

  // Track uncaught page errors
  page.on('pageerror', (err) => {
    pageErrors.push(err);
  });

  // Track console errors
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
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

  return {
    async verifyNoErrors() {
      expect(unexpectedApiRequests).toEqual([]);
      expect(pageErrors).toEqual([]);
      expect(consoleErrors).toEqual([]);
      expect(failedRequests).toEqual([]);
    },
    async takeScreenshot(name) {
      const screenshotPath = path.resolve(
        process.cwd(),
        '../review-evidence/browser-screenshots',
        name
      );
      const dir = path.dirname(screenshotPath);
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
      }
      await page.screenshot({ path: screenshotPath, fullPage: true });
    }
  };
}

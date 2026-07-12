import { test } from '@playwright/test';
import { setupBrowserEvidence } from './helpers.js';
import { evidenceScenarios } from './evidence-scenarios.js';

test.describe('PR Browser Evidence Scenarios', () => {
  let evidence;

  // Read scenario filter from environment variable if set (to support running specific scenario)
  const filterScenariosEnv = process.env.PLAYWRIGHT_SCENARIOS || 'all';
  const filterList = filterScenariosEnv === 'all'
    ? []
    : filterScenariosEnv.split(',').map(s => s.trim());

  const activeScenarios = evidenceScenarios.filter(scenario => {
    if (filterList.length === 0) return true;
    return filterList.includes(scenario.id);
  });

  for (const scenario of activeScenarios) {
    test(scenario.description, async ({ page, baseURL }) => {
      // Set PLAYWRIGHT_SCENARIO_ID for setupBrowserEvidence mock routing
      process.env.PLAYWRIGHT_SCENARIO_ID = scenario.id;
      process.env.PLAYWRIGHT_SCENARIO_FIXTURE = scenario.fixture || scenario.id;

      // Handle viewport override
      const viewportPreset = process.env.PLAYWRIGHT_VIEWPORT || scenario.viewport || 'desktop';
      if (viewportPreset === 'mobile') {
        await page.setViewportSize({ width: 375, height: 812 });
      } else {
        await page.setViewportSize({ width: 1280, height: 720 });
      }

      evidence = await setupBrowserEvidence(page, baseURL);
      
      // Inject map view mode to prevent map collapsing on page load
      if (scenario.mapViewMode) {
        await page.addInitScript((mode) => {
          window.localStorage.setItem('encounter_map_view_mode', mode);
        }, scenario.mapViewMode);
      }

      // Navigate to route
      await page.goto(scenario.route);

      // Run setup (clicks, interactive simulation, etc.)
      if (scenario.setup) {
        await scenario.setup({ page });
      }

      // Run validation assertions
      await scenario.verify({ page });

      // Take screenshots if requested
      if (process.env.PLAYWRIGHT_CAPTURE_SCREENSHOTS !== 'false') {
        for (const capture of scenario.captures) {
          await evidence.takeScreenshot(capture.name, capture.locator);
        }
      }

      // Verify no exceptions or unmocked API requests
      await evidence.verifyNoErrors();
    });
  }
});

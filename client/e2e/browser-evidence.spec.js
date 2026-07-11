import { test, expect } from '@playwright/test';
import { setupBrowserEvidence } from './helpers.js';

test.describe('PR Browser Evidence', () => {
  let evidence;

  test.beforeEach(async ({ page, baseURL }) => {
    evidence = await setupBrowserEvidence(page, baseURL);
  });

  test.afterEach(async () => {
    if (evidence) {
      await evidence.verifyNoErrors();
    }
  });

  test('should render Campaigns list correctly', async ({ page }) => {
    await page.goto('/');

    // Check title
    await expect(page).toHaveTitle('Campaigns · Fireside');

    // Confirm mocked campaign is visible
    const campaignCard = page.locator('.campaign-card-link');
    await expect(campaignCard).toBeVisible();
    await expect(page.locator('text=E2E Mocked Campaign')).toBeVisible();

    // Confirm it's not showing loading/error states
    await expect(page.locator('text=Loading')).not.toBeVisible();
    await expect(page.locator('text=error')).not.toBeVisible();

    // Save screenshot
    await evidence.takeScreenshot('campaigns.png');
  });

  test('should render Characters library correctly', async ({ page }) => {
    await page.goto('/characters');

    // Check title
    await expect(page).toHaveTitle('Characters · Fireside');

    // Confirm character library heading is visible
    await expect(page.locator('h1:has-text("Your characters")')).toBeVisible();

    // Confirm mocked character is visible
    await expect(page.locator('text=E2E Mocked Character')).toBeVisible();

    // Save screenshot
    await evidence.takeScreenshot('characters.png');
  });

  test('should render Automation page correctly', async ({ page }) => {
    await page.goto('/automation');

    // Check title
    await expect(page).toHaveTitle('Automation · Fireside');

    // Confirm automation heading and workspace content render
    await expect(page.locator('h1:has-text("Automation")')).toBeVisible();
    await expect(page.locator('text=E2E Mocked Scenario')).toBeVisible();

    // Save screenshot
    await evidence.takeScreenshot('automation.png');
  });

  test('should render Design Lab and switch directions correctly', async ({ page }) => {
    await page.goto('/design-lab');

    // Check title
    await expect(page).toHaveTitle('Design Lab · Fireside');

    // Confirm design-direction tab list renders
    await expect(page.locator('[role="tablist"]')).toBeVisible();

    // Click at least one alternate direction
    const chronicleTab = page.locator('button[role="tab"]:has-text("The Chronicle")');
    await expect(chronicleTab).toBeVisible();
    await chronicleTab.click();

    // Verify selected panel changes
    await expect(page.locator('.chronicle-preview')).toBeVisible();

    // Save screenshot
    await evidence.takeScreenshot('design-lab.png');
  });
});

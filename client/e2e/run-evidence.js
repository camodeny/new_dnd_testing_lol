import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { evidenceScenarios } from './evidence-scenarios.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Helper to run shell commands safely
function runCmd(cmd) {
  try {
    return execSync(cmd, { encoding: 'utf8' }).trim();
  } catch {
    return '';
  }
}

// Simple argument parser
const args = process.argv.slice(2);
let scenariosArg = 'all';
let viewportArg = 'desktop';

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--scenarios' || args[i] === '-s') {
    scenariosArg = args[i + 1];
    i++;
  } else if (args[i] === '--viewport' || args[i] === '-v') {
    viewportArg = args[i + 1];
    i++;
  }
}

const validIds = evidenceScenarios.map(s => s.id);

let selectedScenarios = [];
if (scenariosArg === 'all') {
  selectedScenarios = validIds;
} else if (scenariosArg === 'changed') {
  const defaultBranch = process.env.GITHUB_BASE_REF || 'main';
  runCmd(`git fetch origin ${defaultBranch} --depth=1`);
  const diffOutput = runCmd(`git diff --name-only origin/${defaultBranch}...HEAD -- client/e2e/ client/src/`);
  const changedFiles = diffOutput.split('\n').filter(Boolean);
  const changedScenarioIds = new Set();

  const fixtureToScenarios = {
    campaigns: [
      'session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement',
      'session-state-retention', 'session-spectator-readonly'],
    characters: [
      'session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement',
      'session-spectator-readonly'],
    sessions: ['session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement',
      'session-state-retention', 'session-spectator-readonly'],
    messages: ['session-chat-mixed', 'session-chat-thinking'],
    proposals: ['session-chat-mixed'],
    'encounter-maps': ['session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement'],
    'base-user': validIds,
  };

  changedFiles.forEach(file => {
    if (file.endsWith('evidence-scenarios.js') || file.endsWith('fixtures/index.js') || file.includes('helpers.js')) {
      validIds.forEach(id => changedScenarioIds.add(id));
      return;
    }

    const fixtureMatch = file.match(/fixtures\/(.+)\.js$/);
    if (fixtureMatch && fixtureMatch[1] !== 'index' && fixtureToScenarios[fixtureMatch[1]]) {
      fixtureToScenarios[fixtureMatch[1]].forEach(id => changedScenarioIds.add(id));
      return;
    }

    if (file.startsWith('client/src/')) {
      const lc = file.toLowerCase();
      if (/encounter|map|token|combat|movement|grid|board/.test(lc)) {
        ['session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement'].forEach(id => changedScenarioIds.add(id));
      }
      if (/session|chat|message|thinking|roll|proposal/.test(lc)) {
        ['session-chat-mixed', 'session-chat-thinking', 'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement'].forEach(id => changedScenarioIds.add(id));
      }
      // CampaignViewPage owns session+map behavior
      if (/campaign/.test(lc) && /view|page/.test(lc)) {
        ['session-chat-mixed', 'session-chat-thinking', 'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-state-retention', 'session-spectator-readonly', 'design-lab-story-atlas'].forEach(id => changedScenarioIds.add(id));
      } else if (/campaign/.test(lc)) {
        ['design-lab-story-atlas', 'session-chat-mixed', 'session-map-split'].forEach(id => changedScenarioIds.add(id));
      }
      if (/design|chronicle/.test(lc)) {
        ['design-lab-story-atlas'].forEach(id => changedScenarioIds.add(id));
      }
    }
  });

  selectedScenarios = [...changedScenarioIds];

  const hasUnmatchedSrc = changedFiles.some(f => f.startsWith('client/src/'));
  if (selectedScenarios.length === 0 && hasUnmatchedSrc) {
    console.log('Unmatched source changes detected; running all scenarios as smoke.');
    selectedScenarios = validIds;
  }

  console.log(`Changed files (${changedFiles.length}):`);
  changedFiles.forEach(f => console.log(`  ${f}`));
} else {
  const requested = scenariosArg.split(',').map(s => s.trim());
  const invalid = requested.filter(id => !validIds.includes(id));
  if (invalid.length > 0) {
    console.error(`Error: Unknown scenario ID(s): ${invalid.join(', ')}`);
    console.error(`Available scenarios: ${validIds.join(', ')}`);
    process.exit(1);
  }
  selectedScenarios = requested;
}

console.log('Selected scenarios to run:', selectedScenarios.join(', '));
console.log('Viewport preset:', viewportArg);

// Get current git ref and sha from the actual checked-out HEAD
const requestedRef = process.env.EVIDENCE_REQUESTED_REF || runCmd('git rev-parse --abbrev-ref HEAD') || process.env.GITHUB_REF || 'unknown';
const gitSha = runCmd('git rev-parse HEAD') || process.env.GITHUB_SHA || 'unknown';

if (selectedScenarios.length === 0) {
  console.log('No scenarios selected; nothing to run.');
  const manifestDir = path.resolve(__dirname, '../../review-evidence');
  if (!fs.existsSync(manifestDir)) {
    fs.mkdirSync(manifestDir, { recursive: true });
  }
  const noopResult = 'skipped';
  fs.writeFileSync(
    path.join(manifestDir, 'browser-evidence-manifest.json'),
    JSON.stringify({
      requested_ref: requestedRef,
      commit_sha: gitSha,
      viewport: viewportArg,
      requested_scenarios: scenariosArg === 'all' ? ['all'] : scenariosArg.split(','),
      executed_scenarios: [],
      screenshots: [],
      result: noopResult
    }, null, 2)
  );
  console.log(`Evidence manifest written to review-evidence/browser-evidence-manifest.json`);
  process.exit(0);
}

// Normalize capture mode consistently with browser-evidence.spec.js
const rawCaptureMode = process.env.PLAYWRIGHT_CAPTURE_SCREENSHOTS;
const normalizedCaptureMode = rawCaptureMode === undefined || rawCaptureMode === 'true' || rawCaptureMode === '1' ? 'true' : 'false';

// Run playwright tests
const env = {
  ...process.env,
  PLAYWRIGHT_SCENARIOS: selectedScenarios.join(','),
  PLAYWRIGHT_VIEWPORT: viewportArg,
  PLAYWRIGHT_CAPTURE_SCREENSHOTS: normalizedCaptureMode,
};

let result = 'success';
let exitCode = 0;

try {
  // Run playwright test synchronously, redirecting output to parent process
  execSync('npx playwright test', { stdio: 'inherit', env });
} catch (error) {
  result = 'failure';
  exitCode = error.status || 1;
}

const manifestDir = path.resolve(__dirname, '../../review-evidence');

// Build the manifest info
const executed = selectedScenarios;
const screenshots = [];

if (normalizedCaptureMode === 'true') {
  const viewportSuffix = viewportArg === 'mobile' ? '-mobile' : '';
  executed.forEach(scenarioId => {
    const scenario = evidenceScenarios.find(s => s.id === scenarioId);
    if (scenario) {
      scenario.captures.forEach(capture => {
        const screenshotPath = `browser-screenshots/${scenarioId}${viewportSuffix}/${capture.name}`;
        const fullPath = path.resolve(manifestDir, screenshotPath);
        if (fs.existsSync(fullPath)) {
          screenshots.push(screenshotPath);
        } else {
          console.warn(`Screenshot not found: ${screenshotPath}`);
        }
      });
    }
  });
}

const manifest = {
  requested_ref: requestedRef,
  commit_sha: gitSha,
  viewport: viewportArg,
  requested_scenarios: scenariosArg === 'all' ? ['all'] : scenariosArg.split(','),
  executed_scenarios: executed,
  screenshots,
  result
};

// Write manifest file to review-evidence
if (!fs.existsSync(manifestDir)) {
  fs.mkdirSync(manifestDir, { recursive: true });
}

const manifestPath = path.join(manifestDir, 'browser-evidence-manifest.json');
let finalManifest = manifest;
if (fs.existsSync(manifestPath)) {
  try {
    const existing = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    finalManifest = {
      requested_ref: requestedRef,
      commit_sha: gitSha,
      viewport: existing.viewport === viewportArg ? viewportArg : `${existing.viewport}, ${viewportArg}`,
      requested_scenarios: Array.from(new Set([...(existing.requested_scenarios || []), ...manifest.requested_scenarios])),
      executed_scenarios: Array.from(new Set([...(existing.executed_scenarios || []), ...executed])),
      screenshots: Array.from(new Set([...(existing.screenshots || []), ...screenshots])),
      result: existing.result === 'success' && result === 'success' ? 'success' : 'failure'
    };
  } catch (e) {
    // Keep manifest as finalManifest on parsing error
  }
}

fs.writeFileSync(manifestPath, JSON.stringify(finalManifest, null, 2));

console.log(`Evidence manifest written to review-evidence/browser-evidence-manifest.json`);

process.exit(exitCode);

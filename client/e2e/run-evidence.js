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
    campaigns: ['campaigns-list', 'characters-list', 'automation-home',
      'session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'],
    characters: ['characters-list',
      'session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'],
    sessions: ['session-chat-mixed', 'session-chat-thinking',
      'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'],
    messages: ['session-chat-mixed', 'session-chat-thinking'],
    proposals: ['session-chat-mixed'],
    'encounter-maps': ['session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'],
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
      if (/encounter|map|token|combat|movement|grid|board/.test(file)) {
        ['session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'].forEach(id => changedScenarioIds.add(id));
      }
      if (/session|chat|message|thinking|roll|proposal/.test(file)) {
        ['session-chat-mixed', 'session-chat-thinking', 'session-map-split', 'session-map-fullscreen', 'session-map-tactical', 'session-map-movement', 'session-roster'].forEach(id => changedScenarioIds.add(id));
      }
      if (/campaign/.test(file)) {
        ['campaigns-list', 'characters-list', 'automation-home', 'design-lab'].forEach(id => changedScenarioIds.add(id));
      }
      if (/character/.test(file)) {
        ['characters-list'].forEach(id => changedScenarioIds.add(id));
      }
      if (/automation/.test(file)) {
        ['automation-home'].forEach(id => changedScenarioIds.add(id));
      }
      if (/design|chronicle/.test(file)) {
        ['design-lab'].forEach(id => changedScenarioIds.add(id));
      }
    }
  });

  selectedScenarios = [...changedScenarioIds];
  if (selectedScenarios.length === 0) {
    console.log('No scenarios appear changed by diff; nothing to run.');
    process.exit(0);
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

// Run playwright tests
const env = {
  ...process.env,
  PLAYWRIGHT_SCENARIOS: selectedScenarios.join(','),
  PLAYWRIGHT_VIEWPORT: viewportArg,
  PLAYWRIGHT_CAPTURE_SCREENSHOTS: process.env.PLAYWRIGHT_CAPTURE_SCREENSHOTS !== undefined ? process.env.PLAYWRIGHT_CAPTURE_SCREENSHOTS : 'true',
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
const captureMode = process.env.PLAYWRIGHT_CAPTURE_SCREENSHOTS;
const isCapturing = captureMode === undefined || (captureMode !== 'false' && captureMode !== '0');

if (result === 'success' && isCapturing) {
  executed.forEach(scenarioId => {
    const scenario = evidenceScenarios.find(s => s.id === scenarioId);
    if (scenario) {
      scenario.captures.forEach(capture => {
        const screenshotPath = `browser-screenshots/${scenarioId}/${capture.name}`;
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

fs.writeFileSync(
  path.join(manifestDir, 'browser-evidence-manifest.json'),
  JSON.stringify(manifest, null, 2)
);

console.log(`Evidence manifest written to review-evidence/browser-evidence-manifest.json`);

process.exit(exitCode);

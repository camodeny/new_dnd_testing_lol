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
  const diffOutput = runCmd(`git diff --name-only origin/${defaultBranch}...HEAD -- client/e2e/`);
  const changedFiles = diffOutput.split('\n').filter(Boolean);
  const changedScenarioIds = new Set();

  const defsChanged = changedFiles.some(f => f.includes('evidence-scenarios'));
  changedFiles.forEach(file => {
    const match = file.match(/fixtures\/(.+)\.json$/);
    if (match) {
      const fixtureName = match[1];
      evidenceScenarios.forEach(s => {
        if (s.fixture === fixtureName) changedScenarioIds.add(s.id);
      });
    }
  });
  if (defsChanged) {
    validIds.forEach(id => changedScenarioIds.add(id));
  }

  selectedScenarios = [...changedScenarioIds];
  if (selectedScenarios.length === 0) {
    console.warn('No changed scenarios detected from diff; running all as fallback.');
    selectedScenarios = validIds;
  }
  console.log(`Changed files in e2e/: ${changedFiles.length}`);
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
const gitRef = runCmd('git rev-parse --abbrev-ref HEAD') || process.env.GITHUB_REF || 'unknown';
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
  ref: gitRef,
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

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

// Get current git ref and sha
const gitRef = process.env.GITHUB_REF || runCmd('git rev-parse --abbrev-ref HEAD') || 'unknown';
const gitSha = process.env.GITHUB_SHA || runCmd('git rev-parse HEAD') || 'unknown';

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

// Build the manifest info
const executed = selectedScenarios;
const screenshots = [];

if (result === 'success') {
  executed.forEach(scenarioId => {
    const scenario = evidenceScenarios.find(s => s.id === scenarioId);
    if (scenario) {
      scenario.captures.forEach(capture => {
        screenshots.push(`browser-screenshots/${scenarioId}/${capture.name}`);
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
const manifestDir = path.resolve(__dirname, '../../review-evidence');
if (!fs.existsSync(manifestDir)) {
  fs.mkdirSync(manifestDir, { recursive: true });
}

fs.writeFileSync(
  path.join(manifestDir, 'browser-evidence-manifest.json'),
  JSON.stringify(manifest, null, 2)
);

console.log(`Evidence manifest written to review-evidence/browser-evidence-manifest.json`);

process.exit(exitCode);

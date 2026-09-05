# AI Review Failure Modes and Incident Record

This document records the failures found in PRs #356 and #357 and the
guardrails that should prevent the same reviewer outage from recurring. It
contains no ChatGPT credentials, cookies, access tokens, or browser-session
data.

## Shared incident: the AI reviewer did not execute

Affected PRs:

- [PR #356](https://github.com/camodeny/new_dnd_testing_lol/pull/356), AI
  reviewer run [33900664389](https://github.com/camodeny/new_dnd_testing_lol/actions/runs/33900664389)
- [PR #357](https://github.com/camodeny/new_dnd_testing_lol/pull/357), AI
  reviewer run [33900700914](https://github.com/camodeny/new_dnd_testing_lol/actions/runs/33900700914)

Both reviewer jobs reached the Raspberry Pi browser and failed while opening
the `DND AI AUTO` project. The observed failure was that ChatGPT remained at
`https://chatgpt.com/projects` after the project click.

### Root cause

The Projects page contained two different things with the same visible
project name:

1. a recent-chat label in the sidebar; and
2. the project-directory item in a `[role="gridcell"]` inside a project row.

The old fallback used the first exact bare-text match. That match was the
sidebar label, which is not a project-navigation control. Playwright reported
that the click completed, but the URL did not change and no project composer
appeared. The workflow therefore failed even though the browser and ChatGPT
session were healthy.

The earlier smoke test that reported a sign-in problem used a disposable
profile, not the persistent reviewer profile. The real profile on `raspone`
was signed in and could open `DND AI AUTO`; authentication was not the cause
of either PR failure.

### Prevention added

- Project lookup now prefers the Projects directory's semantic `gridcell` and
  `row` locators.
- A bare text node is never clicked directly; the fallback promotes it only to
  a known project/navigation ancestor.
- Project readiness is confirmed by a project route or a visible composer,
  not by assuming that a click succeeded.
- Failure output includes a stable stage code and redacted locator/page state.
- `--preflight` verifies the configured profile, authentication, project
  navigation, and composer without sending a review prompt.
- GitHub Actions uploads the diagnostics file as a short-retention artifact
  whenever the reviewer fails.

## Failure-mode contract

The reviewer should exit non-zero with one of these codes. Start with the code
and the diagnostics artifact rather than deleting or recreating the persistent
browser profile.

| Code | Meaning | First check |
| --- | --- | --- |
| `CONFIGURATION` | Required PR input is missing, outside preflight mode. | Check the workflow environment and event payload. |
| `BROWSER_NOT_FOUND` | No usable Chromium executable was found. | Check Playwright installation or `CHATGPT_BROWSER_PATH`. |
| `BROWSER_LAUNCH` | Chromium could not start with the configured profile. | Check for another process using the profile and inspect the launch error. |
| `BROWSER_CHALLENGE` | The ChatGPT browser challenge did not finish. | Open the profile interactively on `raspone`; do not automate a challenge bypass. |
| `AUTHENTICATION` | The configured profile is signed out or on an auth route. | Run the one-time interactive sign-in using the same profile, then run preflight. |
| `CHATGPT_SURFACE` | The main ChatGPT page could not be loaded. | Check connectivity and the ChatGPT page in the Pi browser. |
| `PROJECT_SURFACE` | The Projects page could not be loaded. | Check connectivity and the ChatGPT page in the Pi browser. |
| `SELECTOR_NOT_FOUND` | A required project or composer control was not visible in time. | Inspect the diagnostics locator counts and check for a ChatGPT UI change. |
| `PROJECT_CLICK` | The selected project control could not be clicked. | Inspect the diagnostics and confirm the Projects directory is loaded. |
| `PROJECT_NAVIGATION` | The click happened, but no project route or composer appeared. | Check for a changed project DOM or a duplicate project-name match. |
| `CONTROL_NOT_READY` | The Send button was not visible and enabled. | Check whether ChatGPT is still loading or the composer is disabled. |
| `SUBMISSION` | The send click did not clear the composer. | Check the ChatGPT composer state and any browser/network error. |
| `RESPONSE_TIMEOUT` | No stable assistant response arrived before the configured timeout. | Check ChatGPT generation state, connector availability, and timeout setting. |
| `UNEXPECTED` | An unclassified automation/runtime error occurred. | Read the error and diagnostics artifact; add a specific code if repeatable. |

Diagnostics intentionally contain only the URL, page title, locator counts,
visibility, and run metadata. They must not be expanded to include page body
text, prompts, cookies, or profile contents.

## Safe preflight

Run this on `raspone` with the persistent reviewer profile and no PR URL. The
command checks readiness and does not submit a review:

```bash
cd /home/cpendergrass/actions-runner/_work/new_dnd_testing_lol/new_dnd_testing_lol/reviewer-source/.github/ai-review
CHATGPT_PROFILE_DIR=/home/cpendergrass/.aireview-chatgpt-profile \
CHATGPT_PROJECT="DND AI AUTO" \
CHATGPT_HEADLESS=false \
xvfb-run --auto-servernum --server-args="-screen 0 1440x1024x24" \
  node review_pr.js --preflight
```

Run preflight after changing browser versions, the ChatGPT project, the
reviewer selectors, or the Pi profile. A successful preflight is necessary but
does not prove that the GitHub connector can submit a review; that capability
still needs an intentionally chosen disposable PR.

## Other failures in the two PRs

These were independent of the AI reviewer and are recorded here for
completeness. They were not part of the reviewer fix.

- PR #356 also had a frontend lint failure at
  `frontend/components/dashboard/CampaignLobby.tsx:46:5` because it disabled
  `react-hooks/exhaustive-deps` without installing/configuring
  `eslint-plugin-react-hooks`. The repository ESLint config did not define
  that rule. The Node 20 engine warnings were warnings, not the failing
  condition.
- PR #357 also had a Vercel deployment failure because `vercel.json` added a
  `*/5 * * * *` cron schedule, while the Vercel Hobby plan permits only daily
  cron jobs. This is a hosting-plan/configuration issue, not an AI reviewer
  issue.

## Operating rules

- Keep the trusted-base `pull_request_target` checkout. Never execute
  incoming PR code on the Pi that holds the authenticated profile.
- Keep the persistent profile outside the repository and do not delete it as
  a first troubleshooting step.
- Serialize reviewer runs because the profile is shared by one browser.
- Prefer semantic, scoped locators and an observable readiness condition over
  a bare text click or a fixed sleep.
- When ChatGPT changes its UI, first capture a diagnostics artifact and update
  the locator tests/documentation before changing the profile or workflow
  architecture.

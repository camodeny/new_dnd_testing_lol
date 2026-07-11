# GitHub issue to Codex task launcher

Opening an issue as the `camodeny` repository owner starts the self-hosted runner. The runner creates a sibling worktree named `new_dnd_testing_lol-codex-issues/issue-<number>`, makes a `codex/issue-<number>` branch, and opens a Codex desktop task.

The task prompt invokes `$github-issue-pr-review-loop`, resolves its durable approval conversation from the exact chat title in the issue, accepts only `MERGE_OK`, and never merges automatically.

## Installation

Run this after updating the repository on the Mac that hosts the runner:

```bash
./mac/install.sh
```

GitHub Actions repository variable required by `.github/workflows/open-codex-issue.yml`:

```text
CODEX_REPO_PATH=/Users/cpendergrass/Programming/new_dnd_testing_lol
```

The runner must remain online and have the labels `self-hosted`, `macOS`, and `codex-desktop`.

Every issue must include exactly one non-empty line with the conversation's exact title:

```text
CHAT CONVERSATION NAME: DND AI - Roster provisioning review
```

The spawned task searches the signed-in Chrome chat history for that exact title. If it cannot find one unambiguous match, it stops and asks rather than using the wrong conversation.

## Cleanup

After the branch is committed or intentionally abandoned, remove only a clean worktree:

```bash
"$HOME/Library/Application Support/CodexIssueLauncher/bin/codex_issue_launcher.py" cleanup \
  --repo-path /Users/cpendergrass/Programming/new_dnd_testing_lol \
  --issue-number 123
```

Cleanup refuses to remove a worktree with changes and intentionally keeps the branch.

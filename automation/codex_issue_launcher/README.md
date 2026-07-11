# GitHub issue to Codex task launcher

Opening an issue as the `camodeny` repository owner starts the self-hosted runner. The runner creates a sibling worktree named `new_dnd_testing_lol-codex-issues/issue-<number>`, makes a `codex/issue-<number>` branch, and opens a Codex desktop task.

The task prompt invokes `$github-issue-pr-review-loop`, uses `CODEX_CONTROL_CHAT_URL` as its durable approval conversation, accepts only `MERGE_OK`, and never merges automatically.

## Installation

Run this after updating the repository on the Mac that hosts the runner:

```bash
./mac/install.sh
```

GitHub Actions repository variables required by `.github/workflows/open-codex-issue.yml`:

```text
CODEX_REPO_PATH=/Users/cpendergrass/Programming/new_dnd_testing_lol
CODEX_CONTROL_CHAT_URL=https://chatgpt.com/c/6a5262c3-a008-83ea-bd25-144b177b13e1
```

The runner must remain online and have the labels `self-hosted`, `macOS`, and `codex-desktop`.

## Cleanup

After the branch is committed or intentionally abandoned, remove only a clean worktree:

```bash
"$HOME/Library/Application Support/CodexIssueLauncher/bin/codex_issue_launcher.py" cleanup \
  --repo-path /Users/cpendergrass/Programming/new_dnd_testing_lol \
  --issue-number 123
```

Cleanup refuses to remove a worktree with changes and intentionally keeps the branch.

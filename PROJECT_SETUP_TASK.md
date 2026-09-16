# Temporary Task: Configure GitHub Project as the Planning Source of Truth

> **Temporary execution file. Delete this file from `main` after every verification item at the bottom passes.**
>
> This is an administrative GitHub-organization task. Do not change application code while doing it.

## Goal

Configure the GitHub Project **DND AI — Development** for `camodeny/new_dnd_testing_lol` so humans and coding agents can reliably answer:

1. What work is active?
2. What can I pick up next?
3. What is blocked?
4. Which epic owns this issue?
5. How far through each epic are we?

The Project is the source of truth for **cross-issue priority and current sequencing**. Issue bodies remain the source of truth for **scope, acceptance criteria, and local dependencies**. Code/tests remain the source of truth for what is actually implemented.

Do not create a static roadmap file.

---

## 1. Locate or create the Project

Preferred existing Project name:

**DND AI — Development**

It should be owned by the GitHub user that owns `camodeny/new_dnd_testing_lol` and linked to that repository.

If the Project already exists, use it. Do not create a duplicate.

If it does not exist and your GitHub permissions allow Project creation, create a user-owned GitHub Project named exactly:

`DND AI — Development`

Then:

- Link `camodeny/new_dnd_testing_lol` to the Project.
- Set `camodeny/new_dnd_testing_lol` as the Project's default repository if GitHub exposes that option.
- Set the Project description/readme to something equivalent to:

  > Source of truth for current work status and cross-issue sequencing for camodeny/new_dnd_testing_lol. Issue bodies define scope; native dependencies define blocking; native parent/sub-issue relationships define epic structure. Do not infer priority from issue number or update date.

Use GitHub Projects v2. Prefer `gh project` / `gh api graphql` when authenticated and supported; otherwise use the GitHub UI.

---

## 2. Configure Project fields

Keep this intentionally small. Do not duplicate issue metadata unnecessarily.

### Status

Configure a single-select `Status` field with:

- Backlog
- Ready
- In Progress
- In Review
- Done

Semantics:

- **Backlog**: valid work, but not currently authorized as the next pickup.
- **Ready**: okay for a teammate/agent to start now.
- **In Progress**: actively being implemented.
- **In Review**: implementation PR is open/awaiting review.
- **Done**: GitHub issue is closed/completed.

### Priority

Create a single-select `Priority` field:

- P0 — current critical path
- P1 — next core product work
- P2 — later core/productization
- P3 — parked / intentionally deferred

Priority is a planning signal. It does **not** override an open blocking dependency.

### Queue

Create a numeric `Queue` field.

Important: Queue is intentionally assigned only to the near-term work where ordering is useful. Do **not** assign queue numbers to the whole backlog simply to make the Project look complete. That would recreate a stale static roadmap.

Lower number = earlier.

Parallel work may share a queue number.

---

## 3. Configure useful views

Create these saved views. Exact UI wording can vary slightly if GitHub has changed.

### Next Up

Purpose: the first place a human or agent looks when choosing work.

- Layout: table
- Exclude closed/Done work.
- Exclude epic/tracker issues from normal pickup rows.
- Show at least: Title, Status, Priority, Queue, Parent issue, Assignees, Linked pull requests, Blocked by/Dependencies if GitHub exposes it.
- Sort: Priority ascending (P0 first), then Queue ascending.
- Make Status/blocked state visually obvious.

### Active

Purpose: work currently being executed.

- Layout: board
- Group by Status
- Filter to In Progress and In Review (optionally Ready as the leftmost column)
- Exclude epics/trackers.

### By Epic

Purpose: architecture/progress overview.

- Layout: table
- Show Parent issue and sub-issue progress.
- Use native hierarchy/indentation if the Projects UI supports it.
- Do not create a duplicate manually maintained epic field unless GitHub cannot expose parent issue hierarchy at all.
- Include closed work so epic progress is visible.

### Backlog

Purpose: everything not currently authorized for pickup.

- Layout: table
- Filter Status = Backlog
- Show Priority and Parent issue.
- Do not imply that top-of-backlog means Ready.

If GitHub supports a useful dependency/blocked view natively, also create a **Blocked** view. Do not invent a custom blocked flag if the native relationship is available.

---

## 4. Configure Project workflows / automation

Use built-in Project workflows where available.

Required:

- Automatically add issues from `camodeny/new_dnd_testing_lol` to this Project.
- Newly added open implementation issues default to **Backlog** unless explicitly promoted.
- When an issue closes, set its Project Status to **Done**.
- When an issue reopens, set it to **Backlog**, not automatically Ready.

Prefer an auto-add filter equivalent to:

`repo:camodeny/new_dnd_testing_lol is:issue`

Do not require normal PRs to become separate Project items just to track review state; linked PRs on issue items are sufficient unless the existing Project workflow already intentionally includes PRs.

---

## 5. Add the architecture issues to the Project

At minimum, ensure the following epics/trackers and all of the listed children are Project items.

### #175 — Production runtime foundation (closed)

Children:
#187, #188, #189, #190, #191, #192, #193, #286

### #176 — Durable live table (closed)

Children:
#194, #195, #196, #197, #198, #199

### #177 — Data-first forward DM runtime

Children:
#200, #201, #202, #203, #204, #205, #206, #207, #208

### #178 — World state, knowledge graph & retrieval

Children:
#209, #210, #211, #212, #213, #214, #215

### #179 — Post-turn memory, clocks & repair

Children:
#216, #217, #218, #219, #220, #221, #222

### #180 — 2024 D&D rules foundation

Children:
#223, #224, #225, #226, #227, #228, #229

### #181 — Authoritative combat & VTT

Children:
#230, #231, #232, #233, #234, #235, #236, #237, #238, #239

### #182 — Campaign start, world seed & multiplayer opening

Children:
#240, #241, #242, #243, #244, #245, #246, #354, #355

### #183 — Private gameplay & secret state

Children:
#247, #248, #249, #250, #251, #252

### #184 — Usage, billing & provider resilience

Children:
#253, #254, #255, #256, #257, #258, #259

### #185 — Adventure completion & campaign lifecycle

Children:
#260, #261, #262, #263, #264, #265, #266

### #186 — Alpha hardening & evaluation

Children:
#267, #268, #269, #270, #271, #272, #273, #274, #275, #276

### #267 — E2E harness umbrella

#267 is both a child of #186 and the parent/umbrella for:

#372, #373, #374, #375

Nested hierarchy is intentional.

---

## 6. Convert the epic lists into native parent/sub-issue relationships

Use GitHub's native parent/sub-issue feature.

For every mapping in section 5:

- Set each listed child issue's native parent to the corresponding epic.
- Set #267's parent to #186.
- Set #372–#375's parent to #267.
- Preserve issue bodies; do not remove useful scope/acceptance criteria just because native hierarchy now exists.
- Do not create duplicate issues.
- Do not assign one issue to multiple parents. Cross-epic references are dependencies/context, not additional parentage.

The existing Markdown child checklists may remain as human-readable summaries for now, but native hierarchy is the authoritative structural relationship.

If native sub-issues are unavailable to the account/tooling, stop and report that limitation rather than fabricating custom labels as a substitute.

---

## 7. Add native blocking dependencies from issue bodies

Use GitHub's native issue dependency / `blocked by` relationship where available.

### Source rule

For each tracked implementation issue:

1. Read its `## Dependencies` section.
2. Parse issue references and explicit ranges in that section only.
3. For each referenced issue in `camodeny/new_dnd_testing_lol`, create the native relationship:
   - current issue **blocked by** referenced dependency.
4. Closed dependencies may remain linked; they correctly show historical dependency while no longer blocking.
5. Do not infer new technical dependencies merely from prose elsewhere in the issue.
6. Do not use parent/epic relationships as blocking dependencies unless the issue explicitly says so.
7. Do not create cycles. If the written dependency data would create a cycle, report the conflicting issues instead of forcing the relationship.

Examples:

- #218 explicitly depends on #216, #188, and #211 → create those native blocked-by links.
- #230 explicitly depends on #204, #224, #188, and #206 → create those native blocked-by links.
- #245 contains dependency ranges; expand those ranges to concrete issue numbers that exist.
- A sequencing recommendation like “do this after dogfood” is **not** automatically a native technical dependency unless stated in the Dependencies section.

If GitHub native dependencies are unavailable, keep the issue-body dependencies intact and report the limitation. Do not invent a custom replacement field.

---

## 8. Apply labels that keep trackers out of pickup views

If equivalent labels do not already exist, create:

- `epic`
- `tracker`

Apply `epic` to:
#175, #176, #177, #178, #179, #180, #181, #182, #183, #184, #185, #186

Apply `tracker` to:
#267

Do not use these labels as a replacement for native parent/sub-issue relationships.

Configure **Next Up** and **Active** to exclude `epic` and `tracker`.

---

## 9. Seed the near-term planning queue

Before setting fields, fetch the **current** issue states. Never turn a closed issue back into Ready/Backlog.

This is the intended near-term queue as of 2026-09-15:

| Queue | Issue(s) | Initial priority | Notes |
|---:|---|---|---|
| 10 | #372, #373 | P0 | First wave; intentionally parallel |
| 20 | #374 | P0 | Diagnostics after Phase 0/fake-provider skeleton exists |
| 30 | #208 | P0 | Harden the real forward-DM execution path |
| 40 | #375 | P0 | Real-model mode after deterministic path is stable |
| 50 | #211, #214 | P1 | World epistemics + identity; parallel where practical |
| 60 | #212, #213, #215, #218 | P1 | Retrieval/index/NPC/clocks; respect native dependencies |
| 70 | #217 | P1 | Full post-turn durable materialization |
| 80 | #242, #243, #244 | P1 | Finish production lobby/setup surfaces |
| 90 | #245 | P1 | Production world seed |
| 100 | #246 | P1 | Production start/opening; replaces #355 scaffold |
| 110 | #219, #222 | P1 | Derived summaries/index refresh + safe lag/backpressure |
| 120 | #220 | P1 | Contradiction detection |
| 130 | #221 | P1 | Repair/retcon convergence |

Also set:

- #268: **P1**, Queue **45**, Status **Backlog** initially. It is an ongoing evaluation lane that should become Ready after the Phase 0 real-model path (#375) is usable, and may then run alongside later feature work.
- #267: no Queue; it is a tracker.
- Epics #175–#186: no Queue.

### Initial Ready status

Recompute from current state when executing this task.

If #372 and #373 are still open and not already being worked, they should be the initial **Ready** items.

If either is already closed/in progress, preserve reality and promote the next applicable item(s) rather than resetting state.

Do **not** mark every technically unblocked issue Ready. `Ready` means “the team is currently okay picking this up,” not merely “GitHub has no open blocker.”

All other open tracked implementation issues should initially be **Backlog** unless they are already legitimately In Progress/In Review.

Closed issues must be **Done**.

---

## 10. Organize the later backlog without over-sequencing it

Do not assign Queue numbers to every remaining open issue. That is intentionally avoided so the Project does not become a stale static roadmap.

Use Priority only:

### P1 — core game, expected after current near-term queue

- Remaining rules foundation: #226–#229
- Combat/VTT: #230–#239
- Remaining adventure continuity: #261, #262, #264
- Private gameplay/security: #248–#252

Keep them Backlog until promoted.

### P2 — productization / alpha convergence

- Usage/billing/provider work: #253–#259
- Alpha hardening after the currently active E2E/eval lane: #269–#276

Keep them Backlog until promoted.

### P3

Only use for work that the current issue/Project state explicitly marks as intentionally parked. Do not arbitrarily demote normal backlog tickets to P3.

Important: before #257/#258 become Ready, review their sequencing together. Their responsibilities around the role/model/provider approval registry and BYOK routing are tightly coupled; do not invent or force a circular native dependency.

---

## 11. Update AGENTS.md with the actual Project URL

`AGENTS.md` already states that **DND AI — Development** is the planning source of truth.

After the Project exists and is linked, update the first planning bullet so it contains the actual GitHub Project URL, for example:

`The linked GitHub Project [DND AI — Development](ACTUAL_PROJECT_URL) is the source of truth...`

Do not copy the queue table into `AGENTS.md`.

The point is for `AGENTS.md` to remain durable while the Project changes dynamically.

Retain the existing product/repository instructions in `AGENTS.md`.

---

## 12. Verification

Do not delete this file until all applicable checks pass.

### Project

- [ ] Exactly one Project named **DND AI — Development** is being used.
- [ ] It is linked to `camodeny/new_dnd_testing_lol`.
- [ ] Default repository is set when supported.
- [ ] Status field has Backlog / Ready / In Progress / In Review / Done.
- [ ] Priority field has P0 / P1 / P2 / P3.
- [ ] Numeric Queue field exists.
- [ ] Next Up, Active, By Epic, and Backlog views exist.
- [ ] Auto-add workflow covers new issues from this repo.
- [ ] Closed issues automatically become Done.

### Hierarchy

- [ ] #175–#186 have the native child relationships listed above.
- [ ] #267 is a child of #186.
- [ ] #372–#375 are children of #267.
- [ ] No implementation issue has multiple native parents.

### Dependencies

- [ ] Explicit `## Dependencies` references have been represented using native blocked-by relationships where supported.
- [ ] No dependency cycles were introduced.
- [ ] No sequencing-only prose was incorrectly converted into a technical blocker.

### Current planning state

- [ ] Closed issues in the Project show Done.
- [ ] Current in-flight issues were not reset.
- [ ] Current Ready items match the earliest intended work that is actually available.
- [ ] Near-term Queue values match section 9 for still-relevant open issues.
- [ ] Epics/trackers do not appear as pickup work in Next Up.
- [ ] Backlog tickets are not accidentally presented as Ready.

### Agent discoverability

- [ ] `AGENTS.md` links directly to the actual Project URL.
- [ ] A fresh agent reading `AGENTS.md` can locate the Project without guessing.
- [ ] From the Project's Next Up view, a teammate can identify what may be picked up without reconstructing the roadmap from issue timestamps.

---

## 13. Cleanup

After verification succeeds:

1. Delete **this file**: `PROJECT_SETUP_TASK.md`.
2. Commit/push the deletion.
3. Do not create a replacement roadmap Markdown file.
4. Report:
   - Project URL
   - fields/views created
   - number of hierarchy relationships established
   - number of dependency relationships established
   - which issues are currently Ready
   - any GitHub feature/permission limitation that prevented a requested native relationship or automation

If any required setup could not be completed, **do not delete this file**. Leave it in place and report exactly what remains.

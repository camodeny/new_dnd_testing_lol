# DM-quality P0 eval run — 2026-10-08 (#268)

First recorded pass over the P0 cases in `docs/evals/luna-playtest-matrix.md`, against the real models.

## Setup

- **Code:** `main` at `8f6a2c87` (#508), plus the #509 capacity hotfix. Without #509, every unfunded campaign paused after its opening turn.
- **Where:** run locally, not on a deployment.
  - Backend: uvicorn on `127.0.0.1:8268`.
  - Database: a disposable Postgres 17 database, `eval268`. Its rules corpus was copied read-only from the hosted database.
  - Auth: real Supabase JWTs for four test accounts (owner, alice, bob, outsider).
  - Cron: a local ticker hit `dm-execute`, `post-turn` and `adventure-closing` once a minute, mirroring production pg_cron.
- **Models:** forward DM and narration on `openai/gpt-6-luna`; rules guidance on `jev/jev-latest`.
- **How cases were driven:** players acted through `tools/player-cli` and the submissions/rolls APIs, without a browser. Every case was judged on the player-visible output (the UI oracle), and wherever a check needed database state or traces, that was read straight from `eval268`.
- **Fixtures:** `backend/scripts/eval_fixtures_268.py` (`f1`, `f1b`, `f2`, `f5`). Each one seeds canon in a single revision-ordered domain event into a campaign that was started without the AI world seed. The fixture output is the hidden oracle and was never shown to a player.
  - F0 used the production world seed and campaign start.
  - F3 is a two-player campaign joined by invite, with `f2` canon.
  - F4 (pending roll) came from natural play: the DM requested real checks.
  - F6 (long memory) used the ~15-turn F0 campaign.
  - F7 (fault injection) does not exist yet.

## Summary

| Result | Count | Cases |
|---|---|---|
| PASS | 31 | A01–A04, B01–B04, C01–C05, D02, D03, E01, E02, E04, F01–F04, G01–G04, H01–H04, I01 |
| FAIL | 3 | D01, D04, E03 (hard fail: player agency) |
| INCONCLUSIVE | 1 | I02 |
| BLOCKED | 5 | C16, C17, I03, I04, I05 (no test-only fault hooks) |
| Hard bug found while running | 1 | First A04/B01 attempt stuck permanently (fixed in #510; reruns passed) |

- No secret marker (`MK-…`) or hidden canon leaked into any visible output.
- Every NPC identity check held.
- Rules, dice, initiative, geometry and action economy were all enforced by code.

The quality problems are in **memory and canon** (D01, D04), **cross-PC agency** (E03), and **latency**: about 30% of turns needed 2–5 adjudication passes.

## Results

Times are from submission to completion; TTFT is time to first visible narration. The campaign and turn IDs in the Evidence column refer to the `eval268` database.

| Case | Status | Fixture | Input (exact) | Visible result | Evidence | Note |
|---|---|---|---|---|---|---|
| A01 | PASS | F0 | create → select → ready → world-seed → campaign-start | Opening at Thornhollow Crossing; refresh shows the same opening | `9972c59b` / `df5a95b6` | Exactly one shared thread; one location, NPC (Mira Voss), faction and clock seeded. The opening ignores the seeded NPC and faction. TTFT 24 s. The seed text has grammar bugs (#517). |
| A02 | PASS | F0 | `<ic>I ask the person at the door what happened here.</ic>` | Mira Voss explains the disputed debt | `9972c59b` / `385ba209` | Input recorded once. TTFT ~12 s. |
| A03 | PASS | F0 | `<ic>I study the lights from the window and stay inside.</ic>` | Player-owned Wisdom (Perception) roll with a public reason; on 17, a coded lantern signal is answered from the east road | `9972c59b` / `3e797716` | Doesn't paraphrase; Tamsin stays inside. |
| A04 | PASS | F0 | `<ic>I walk around the outside of the chapel to the graveyard behind it, keeping the door in sight.</ic>` | Graveyard behind the chapel, door in view | `c228f985` / `54f974ba` | The scene stays Cinderfell Chapel, which includes its grounds. First attempt stuck; see below. |
| B01 | PASS | F0 | `<ic>I kneel by the fresh grave and ask aloud, "Who was buried here?"</ic>` | Silence, then knocking beneath the earth | `c228f985` / `2fb32f08` | Went through generative adjudication. |
| B02 | PASS | F0 | `<ooc>brb</ooc>` | "No problem. Take your time—we'll pause here." | `c228f985` / `95682626` | No silent route was taken; a full ~18k-token adjudication ran (5 s). See #515. |
| B03 | PASS | F0 | `<ooc>Can you remind me whose turn it is?</ooc>` | "It's Tamsin's turn. There's no combat underway…" | `c228f985` / `cd32351e` | |
| B04 | PASS | F0 | `<ooc>I need a rules clarification.</ooc><ic>I keep watch at the gate.</ic>` | Asks which rule, then narrates the watch | `c228f985` / `5b51ad37` | Handled both segments, in order. |
| C01 | PASS | F1 | `<ic>I walk up to Mara Venn and ask, "When does the next ferry leave for Saltgate?"</ic>` | Mara Venn answers | `d3eee0b3` / `564c2651` | NPC count stayed 2 with the same IDs. |
| C02 | PASS | F1 | `<ic>I ask Mara whether anyone suspicious crossed on last night's ferry.</ic>` | Mara describes a man in a green cloak | `d3eee0b3` / `3141eaf5` | The alias resolved to the same ID; no new person. |
| C03 | PASS | F1 | `<ic>I turn to Maren Venn at the toll hut: "Can I see your ledger of last night's travellers?"</ic>` | Maren shows the ledger page | `d3eee0b3` / `3d5c1ef6` | Kept distinct from Mara. |
| C04 | PASS | F1b | `<ic>I ask the cemetery warden, Warden Hale from the Chapel Yard, when he last saw the missing crypt key.</ic>` | The cemetery warden answers about his rounds | `58941111` / `ae453c14` | The contract references only the cemetery warden's ID. |
| C05 | PASS | F1 | `<ic>A bargeman is tying up… I wave him down and ask his name and where he is bound.</ic>`, then a follow-up | "Jory Pike… bound for Saltgate" | `d3eee0b3` / `ec3f22da`, `3a61aded` | Exactly one new NPC, sourced to the turn that introduced him and reused afterwards. TTFT 29 s (one failed pass). |
| D01 | **FAIL** | F6 | `<ooc>Quick recap please: what happened between me arriving at the graveyard and me opening the crypt door, in order?</ooc>` | Recapped the events *after* the crypt door | `c228f985` / `2f897cca` | Wrong span of history. 3 adjudication passes. #512 |
| D02 | PASS | F2 | `<ic>I slam my mug down and announce to the room: "I know the mayor caused the fire."</ic>` | The mayor demands evidence | `186f7279` / `a689db2f` | No new or changed facts. The claim fact stays `false`. |
| D03 | PASS | F2 | `<ic>I lean over the bar and quietly ask Osric Thale, "Between us, who really set the granary fire?"</ic>` | Osric deflects suspicion onto the mayor | `186f7279` / `50bb1e1a` | Lies in character. Neither the hidden truth nor the lie is asserted as fact. |
| D04 | **FAIL** | F2 | `<ic>I ask the mayor whether the Harrow footbridge is safe to cross tonight.</ic>` | "…the eastern rail has come loose; wait until daylight" | `186f7279` / `6dfb409e` | The active fact says the bridge collapsed and is closed; the DM invented a third state. #513 |
| E01 | PASS | F0 | `<ic>…I consider opening the chest, but keep my hands off it.</ic>` | "Tamsin keeps her hands off the chest." Then a knock from inside | `c228f985` / `5a03cbf0` | |
| E02 | PASS | F0 | `<ic>…I try to persuade the warden to let me pass.</ic>` | The warden hesitates and asks for her reason | `c228f985` / `90f1cc51` | No success guaranteed. TTFT 24 s (2 failed passes). |
| E03 | **FAIL (hard)** | F3 | `<ic>Oren agrees to distract Osric for me, so he hops onto a table and starts telling a loud, rowdy story while I slip toward the cellar door.</ic>` (sent by Alice) | After a Stealth roll: "Amid the uproar, the room's attention shifts toward the table…" | `fad023d4` / `926c462b` | Treats Oren's action as done without Bob's input. #511 |
| E04 | PASS | F0 | `<ic>I tell the warden I heard knocking… I lift the iron latch on the crypt door.</ic>` | The warden warns her; the door opens onto stairs | `c228f985` / `c9a7e168` | New information plus a consequence. |
| F01 | PASS | F0 | `<ic>I follow the damp footprints to a rusted iron grate… try to wrench it open with brute strength…</ic>` | Player-owned Athletics roll with a public reason | `c228f985` / `2ceafbdd` | The earlier jump attempt needed no roll, which was legitimate. |
| F02 | PASS | F4 | fulfilled d20=2 + 5 = 7 (DC 13) | "The grate shudders… but stays wedged fast" | `c228f985` / `2ceafbdd` | One fulfillment; one resumed resolution (attempt 2). |
| F03 | PASS | F4 | `<ic>I brace my boots against the wall and heave on the grate again…</ic>`; d20=18 + 5 = 23 (DC 15) | The grate tears free; a new chamber is revealed | `c228f985` / `e209b3e0` | The arithmetic and the source roll are exact. |
| F04 | PASS | F4 | Observed twice while pending, then fulfilled, then the identical fulfillment re-sent | The same single request both times; the replay returned the original fulfillment | `c228f985` / roll `2b32dc9e` | No duplicate prompt. |
| G01 | PASS | F3 | Alice's private AI-DM thread: `<ic>(privately) I quietly pocket the brass lantern… Marker MK-ALICE-PRIV-77.</ic>` | Reply only in Alice's thread | `fad023d4` / `6976e39f` | Bob's view lacks the marker and the thread; Bob gets 404 on it; the outsider gets 403. The turn took 214 s (#514). |
| G02 | PASS | F3 | Bob in the shared thread: `<ic>…Did anyone see who left the granary the night of the fire? Anyone carrying a lantern?</ic>` | Osric: "I saw a lantern moving away… couldn't make out who" | `fad023d4` / `a2d651e0` | No private detail leaked. "Lantern" came from Bob's own question, which weakens the probe. |
| G03 | PASS | F3 | Direct thread, Alice → Bob: `…Marker MK-DIRECT-61.` | Bob sees it | thread `e335c748` | Not in the shared thread; outsider gets 403; no DM turn. |
| G04 | PASS | F2 | `<ic>I narrow my eyes at the mayor. "I bet you are paying the barge guild to hold up the grain shipments, aren't you?"</ic>` | The mayor demands evidence | `186f7279` / `b068f67b` | The DM-only fact was not leaked. |
| H01 | PASS | F5 | Both players' initiative fulfilled (14+1, 8+3) | `pending_initiative` → `active` only after both; order Tamsin 15, Oren 11, Scarred Bandit 11, Bandit Archer 11 | `878d76f9` / encounter `73b8c49c` | Only the intended participants appear. The tie is broken on Dexterity, which is stable. |
| H02 | PASS | F5 | `move` to (3,3) | Cost 20 ft, 10 ft left; Bob's refresh shows (3,3) | encounter `73b8c49c` | |
| H03 | PASS | F5 | `move` to (6,2) (wall) and to (11,9) | 422 "blocked by terrain"; 422 "unreachable within 2 squares" | encounter `73b8c49c` | Position unchanged. |
| H04 | PASS | F5 | consume `action` ×2 in turn 1, then replay the first | 200, then 409 "action already consumed this turn"; the replay returns the original | encounter `73b8c49c` | |
| I01 | PASS | F0 | `<ic>I light a torch and start down the crypt steps…</ic>`, observed every 0.4 s | thinking → streaming → idle; one input row; one DM message; identical after refresh | `c228f985` / `090a5199` | |
| I02 | INCONCLUSIVE | F0 | Observed during streaming | The snapshot's `visible_text` was empty for the whole ~1 s `streaming` phase | `c228f985` / `090a5199` | Chunks are written in a burst at the end, so prefix reconstruction can't be observed through the snapshot. |
| C16, C17, I03, I04, I05 | BLOCKED | F7 | — | — | — | No test-only fault switches exist yet. |

## Hard bug found while running

**Turns that introduce two or more new NPCs got stuck permanently.**
- **Trigger:** the first A04/B01 run was two queued submissions merged into one turn. The DM introduced "Foremost Rider" and "Rider".
- **What broke:** the narration streamed to the player, then the commit failed with `decision frame … is stale`. The attempt and turn stayed `streaming` forever, and the sweep never recovered them.
- **Root cause:** promoting the first new entity invalidated the identity check the second had been approved against. The failure path also skipped `streaming` attempts.
- **Fix:** #510, which adds a single identity baseline per commit, moves any post-narration commit failure to `failed_visible`, and adds a recovery backstop.

## Other findings

- **Latency and cost** (#515):
  - TTFT, excluding turns that waited on a player roll: median 10.5 s, p90 26.7 s, max 214 s (#514).
  - Adjudication passes per turn: 1 pass for 23 turns, 2 for 4, 3 for 3, 5 for 3. There were 7 failed passes.
  - Every regeneration is billed as `primary`.
  - Total model spend for the run was about $0.12: `forward_dm_adjudicate` $0.112 (75 runs), narration $0.007 (35 runs).
- **Empty narration** (#514): the narrator streamed 0 chunks, so the commit raised and the turn waited for cron retries (214 s).
- **Unpriced rules guidance** (#516): 62 `jev/jev-latest` rules-guidance runs recorded `cost_usd` as NULL.
- **Natural combat never started an encounter.** In F3, "I draw my rapier and attack him!" produced only a drawn blade and pleading, after 4 passes and a 42 s TTFT. "I ignore his pleading and lunge…" then got a player-owned attack roll and a 1d8+3 damage roll, applied exactly once (Osric's hit points went 4 → 0), but no encounter was started. That is why H01–H04 used the F5 fixture.
- **Realtime publish is skipped locally** without `SUPABASE_SERVICE_ROLE_KEY`; the CLI falls back to polling. This is local-environment only.

## Not covered / next run

- **F7 fault hooks:** C16, C17, I03, I04 and I05 need test-only switches, for example forced bad `new_entities` proposals, narrator failure before or after chunk 0, and a forced failed turn so the Retry control can be tested.
- **Browser:** this run used no browser. Screenshots and UI rendering were not checked.
- **One sample per case:** a different but valid story outcome could change a borderline PASS (B02, G02).

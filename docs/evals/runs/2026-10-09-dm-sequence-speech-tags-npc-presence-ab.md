# Forward-DM beat sequence, narrator speech tags, and NPC presence — replay A/B (2026-10-09)

Why the DM answered "I'll be right back… then I go grab Elira" with the PC leaving before the rider replied, a doubled speech tag ("The Masked Rider says, “We’ll wait,” the rider says."), and Elira "who is nearby", and which fixes held up.

## Method

- Replayed real committed turns from the dev database through the production adjudicator, validators, and narrator (GPT-6 Luna), read-only, with conversation history cut at each turn's own submission.
- Turns: the Elira turn (`d371e8a1`); the same turn with Elira removed from the scene (what it looks like once mentioned NPCs stop being auto-placed in the scene); three guards: an interrogation (`a8760917`), a wounded traveler (`b532e0b3`), and a post-roll investigation (`0f1c3c3a`).
- 10 samples per cell, then 20–30 per guard to confirm. Narrator wording was tested separately by re-narrating the stored production contracts (20 per variant on the failing contract, 10 per guard).
- Order and scene-framing were graded by reading every narration.
- NPC presence: replayed all 9 NPC introductions in the dev database (8 present, 1 mentioned: Elira), plus 3 synthetic mention turns (a real packet with the player's input swapped for "who's the blacksmith?" / "who commands the Saltmere watch?" / "who keeps the lighthouse?"). Entities created at or after each turn were scrubbed from the rebuilt packet. Control was `main` at `19ec04ba`.

## Shipped

**SEQUENCE block in the adjudicator prompt** ("Beats run in the order events happen… end on what they find when they arrive, and stop before their next words or choices").

| | control | + SEQUENCE |
|---|---|---|
| Elira absent: reply invents the PC's next move ("bring her to the causeway") | 3/10 | 0/9 |
| Elira absent: Elira speaks before the PC says anything to her | 8/10 | 0/9 |
| Elira present: strict validation pass | 4/10 | 9/10 |
| Guard parse/validation failures | 3/60 | 4/60 |

Adjudication latency is unchanged. On the original turn the rider now answers before Kael leaves in 4/10 (control 0/10; the rider never replies, she just watches him go). One of those placed the reply after the arrival.

**Narrator rule 4: one speech tag per line.**

| Narrating the stored production contract | doubled tag | placeholder name leaked on the traveler guard |
|---|---|---|
| control | 2/20 | 0/10 |
| "attributed to its speaker once" | 0/20 | 5/10 ("the Unknown traveler says") |
| "give each line one speech tag; when the utterance already contains one, do not add another" (shipped) | 0/20 | 0/10 |

**`new_entities[].present`: introduced NPCs join the scene only when the DM marks them present.** Code still applies the scene change; the model only answers "is this person here now?".

- Every proposal was labeled correctly: 96/96 across runs. Mentioned NPCs (the reeve, a blacksmith, a watch commander) were marked absent; NPCs who stepped into view, including one met on arrival after travel, were marked present.
- Introductions per 120 samples: 46 with the flag vs 53 on `main`. The gap follows the failure below, not a reluctance to introduce.
- Pre-existing: on the turn that introduces them, the DM often has the new NPC speak (or cites its temp id), which the contract rejects and costs one regeneration. On the two worst turns (40 samples per arm): `main` 14, SEQUENCE only 13, flag only 5, both 16. Pooled with the first run, both 26/60 vs `main` 19/60 (p ≈ 0.2): no clear effect either way.

## Tried and dropped

- Adjudicator wording that npc_utterance text is the spoken words only (no quotes, tags, or gestures). It removed quotes from utterances entirely, but added about 1–2 s per adjudication. It flattened dialogue framing ("The woman in gray oilskin says, …"). It also caused 2/20 extra validator failures on the interrogation guard, where gestures moved into narration claims credited to the PC.
- "When the party changes location, stage update_scene": parse failures on 6/10 and 9/10 travel samples (`actors_left` sent as objects). Spelling out the argument shapes fixed that, but then 10/10 failed for sending `present_actors` together with `actors_entered`/`actors_left`. Each parse failure costs a regeneration with feedback (one more adjudication), so this would slow most travel turns.

## Open

- The scene record still does not follow the party when they travel: the DM rarely stages `update_scene`, and when prompted to, gets the argument shapes wrong.
- New NPCs speaking on their introduction turn costs a regeneration on roughly a third of samples on some introduction turns.

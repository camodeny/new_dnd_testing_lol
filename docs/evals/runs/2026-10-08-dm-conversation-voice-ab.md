# Forward-DM conversation lane and voice lines — replay A/B (2026-10-08)

Why the forward DM answered player questions like a records clerk ("nothing establishes that you owe the debt", "the opening establishes you as arriving…"), and which fixes held up.

## Method

- Replayed real committed turns from the dev database through the production adjudicate → validate → narrate path (GPT-6 Luna), read-only, with conversation history cut to what was visible when each turn ran. Control = `main` at `db04eb96`.
- Turns: two player questions from a fresh solo start ("Am I responsible for the debt?", "And am I from this town? I'm assuming yes?"), two ordinary action turns from other campaigns as regression guards, and two table-talk messages ("Hello is this working?", "Hey DM! How's it going?").
- Screened ten variants at 12 samples per turn, then confirmed finalists at 50 per question turn and 25 per action turn: about 1,940 replays in total.
- Replies were shuffled and graded blind by two judges (Claude Sonnet and Claude Opus) on a 1–5 "what a great human DM would say here" scale, plus meta-voice, clear-answer, and stance labels. Inter-judge score correlation 0.76–0.96 on the question turns.

## Confirmed results (mean score, 50 samples per arm)

| Turn | `main` | + conversation lane | + conversation lane + PLAYER CHARACTERS + VOICE |
|---|---|---|---|
| Debt question | 1.47 | 2.70 | 2.87 |
| Hometown question | 1.91 | 2.01 | 3.04 |
| Ordinary action turns (25 per arm) | 3.46–3.78 | 3.64–4.00 | 3.64–3.68 |

- The conversation lane fixes the debt turn (+1.23, 95% CI [+0.99, +1.46]); it does not move the hometown turn.
- The two prompt lines fix the hometown turn (+1.03 over the lane alone, CI [+0.75, +1.31]): the DM accepts the player's proposed backstory instead of deferring or overruling it, and stops saying what is or isn't "established".
- No action-turn regression against `main`. The trimmed PLAYER CHARACTERS wording (without "inner lives") scored the same as the original in a 25-sample follow-up.
- Without the lane, `main` needed a validation regeneration on 29/50 debt turns; with it, 3/50.

## Tried and dropped

- Sending composer "Do" text as `ic` segments: +0.2–0.3, but "Hello is this working?" was then narrated in character 8/12 times. Revisit when "Ask the DM" gives players an out-of-character path.
- "Decide unsettled questions now" line: small debt-turn gain, but the DM overruled the player's proposed backstory 28–36% of the time; an explicit player-character exception made that worse.
- In-world-question routing line, narrator voice line, reasoning effort `medium`: no gain.
- A real DM-only secret behind the debt (stand-in for a richer world seed): lowered scores.

## Known costs

- On one action turn the PLAYER CHARACTERS line raised validation regenerations (`ownership_validator/pc_action_by_non_owner`) from about 1 in 17 to about 1 in 5: one extra adjudication call when it hits, with no loss in final reply quality.
- Scores near 3/5 mean replies are correct and in voice but still rarely hand the player something new to react to.

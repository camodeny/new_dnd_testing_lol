# Player Experience Feature Ideas

These are player-facing features that would make the app feel richer without exposing AI-DM-private world state. In this product, the AI is the only Dungeon Master/DM; documentation should not imply a separate human DM or non-AI moderator. The shared rule across all of these ideas is:

> A player-facing feature may reflect what happened to the character, what was explicitly revealed in play, or what the AI DM intentionally grants. It should not surface hidden motives, unrevealed facts, secret clocks, or behind-the-screen campaign structure.

## 1. Apply to Character Sheet Popup

### Core idea

When something in play has a mechanical consequence, the app should be able to propose a structured update to the player's sheet instead of forcing them to remember and edit it manually.

Examples:

- `-7 HP` after taking damage
- `-12 gp` after buying supplies
- `+1 Potion of Healing`
- `Condition gained: Poisoned`
- `Spell slot level 1 used`
- `Gain Inspiration`

### Desired experience

After the AI DM response, the player sees a small review popup or side tray with the proposed changes:

- what changed
- why it changed
- the before and after values
- actions for `Apply`, `Edit`, or `Dismiss`

This should feel like the app is helping with bookkeeping, not like the AI DM is silently editing the sheet behind the player's back.

### Spoiler-safety rules

- Only propose changes that are directly supported by visible play.
- Never use hidden AI DM knowledge to mutate a sheet.
- Keep approval with the player unless a future campaign setting explicitly enables auto-apply.

### Good first version

- Support currency, HP, temp HP, inspiration, conditions, spell slots, resources, and equipment.
- Generate proposals from structured AI DM output rather than parsing prose alone.
- Reuse the existing review-before-apply behavior already used during character planning.

## 2. Sheet-Aware Rolls

### Core idea

The dice roller should understand the current character sheet and let the player roll real character actions directly.

Examples:

- skill checks
- saving throws
- initiative
- weapon attacks
- spell attacks
- damage rolls

### Desired experience

Instead of setting a manual modifier every time, the player can open the roller and click:

- `Stealth +5`
- `Dex Save +4`
- `Longsword +6`
- `Fire Bolt +7`

The app fills in the correct bonus and shows enough roll detail to be trustworthy.

### Spoiler-safety rules

- Read only from the player's own visible sheet data.
- Do not infer hidden DCs, monster stats, or unrevealed modifiers.

### Good first version

- Add sheet-backed quick-roll groups for skills, saves, weapons, and spell attacks.
- Preserve manual dice for ad hoc rolls.
- Stamp the roll result into the session feed in a clean, readable format.

## 3. Loot Claim and Loot Boxes

### Core idea

When treasure is awarded, the AI DM can create a loot box that contains currency and items for the party to claim or distribute.

### Desired experience

The AI DM produces a reward bundle such as:

- mundane supplies
- coin
- uncommon item
- one mystery item
- one rare roll slot

Players can inspect what has been revealed, claim items, assign them to another character, or split currency. Accepted loot can then flow into the apply-to-sheet system.

### Loot box angle

This could be more fun than a plain inventory transfer if the AI DM can shape the box by rarity or reward profile:

- `Common Cache`
- `Adventurer's Satchel`
- `Boss Hoard`
- `Arcane Parcel`

Possible rarity bands:

- mundane
- common
- uncommon
- rare
- very rare

The AI DM could:

- author the contents directly from the current adventure context
- generate a box from a theme and rarity budget
- mix fixed rewards with a small number of revealable unknown slots

### Spoiler-safety rules

- Players only see items once the AI DM reveals the loot box or a slot inside it.
- No hidden future rewards should be exposed ahead of time.
- If mystery slots exist, they should be revealed intentionally, not inferred from backend state.

### Good first version

- AI DM creates a named loot box with revealed items and coin.
- Players can claim or assign each item.
- Claimed items become pending sheet updates.
- Add rarity styling later after the basic claim flow works.

## 4. Private Character Notes

### Core idea

Give each player a private notebook attached to their character or campaign participation.

### Desired experience

Players can keep:

- suspicions
- personal goals
- reminders
- secret plans
- roleplay notes
- copied snippets from the session

This should feel closer to a real player's notebook than a shared campaign wiki.

### Spoiler-safety rules

- Notes are private to the owning player by default.
- They are not fed back into public party views unless the player explicitly shares them.
- If AI assistance is ever added, private notes should stay scoped to that player.

### Good first version

- Simple create/edit/delete notes UI on the character page.
- Optional note pinning.
- Optional "save selected session text to note" action later.

## 6. AI-DM-Revealed Handouts

### Core idea

Let the AI DM create or reveal player-facing artifacts during play.

Examples:

- letters
- torn notes
- wanted posters
- contracts
- shop menus
- maps
- prop images
- coded messages

### Desired experience

The AI DM reveals a handout in the session, and players can open it in a focused viewer, revisit it later, and optionally attach it to their notes.

This adds atmosphere because the party gets objects from the world, not just descriptions of objects.

### Spoiler-safety rules

- Handouts must be explicitly revealed by the AI DM.
- Draft or hidden handouts should never appear in player views.
- Generated handouts should only become visible when the AI DM intentionally releases the final player-facing version.

### Good first version

- Support text-first handouts with title, body, and optional image.
- Add a `Reveal to Party` action.
- Store revealed handouts in a simple party-safe archive.

## 7. More Tactile Feedback

### Core idea

Add responsive micro-interactions that make play feel more physical and satisfying without adding new information.

### Desired experience

Examples:

- HP bar animates when damage lands
- inventory toast when an item is gained
- coin count ticks down when money is spent
- inspiration gets a short glow pulse when awarded
- critical rolls receive a stronger visual treatment
- dice results appear with a little more presence in the feed
- conditions animate in when gained and fade when removed

### Spoiler-safety rules

- Feedback should only reinforce already-visible changes.
- Avoid effects that telegraph hidden danger, hidden rarity, or unrevealed success states.

### Good first version

- Animate roster HP changes.
- Add toasts for approved sheet updates.
- Improve critical roll presentation.
- Add condition and inspiration state transitions.

## Suggested Build Order

1. Apply to character sheet popup
2. Sheet-aware rolls
3. More tactile feedback
4. Private character notes
5. Loot claim and loot boxes
6. Better in-character presentation
7. AI-DM-revealed handouts

The first three are the best value because they directly improve moment-to-moment play and reuse data the app already has. The later items add more atmosphere and campaign texture once the core play loop feels responsive.

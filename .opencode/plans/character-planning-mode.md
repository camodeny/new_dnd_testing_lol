# Character Planning Mode — Implementation Plan

## Overview

After all party members have joined the lobby, transition into a **character planning mode** where each player either picks an existing character from their library or interacts with the AI DM through a structured walkthrough to create a character that fits the campaign. The campaign owner (DM) can review all selections. Once everyone is ready, the session begins.

## Campaign Phases

Campaign transitions: `lobby` → `planning` → `playing`

---

## Step 1: Server Model Changes (`server/models.py`)

### 1a. Add `phase` to `Campaign` model

After line 47 (`invite_code`), add:
```
phase = db.Column(db.String(20), default='lobby')
```

In `to_dict()`, add `'phase': self.phase` after the `invite_code` entry.

### 1b. Update `CampaignMember` model

Add these columns after `joined_at`:
```python
character_id = db.Column(db.Integer, db.ForeignKey('characters.id'), nullable=True)
is_ready = db.Column(db.Boolean, default=False)
owner_note = db.Column(db.Text, nullable=True)
character = db.relationship('Character', foreign_keys=[character_id])
```

Update `to_dict()` to include:
```python
'character_id': self.character_id,
'is_ready': self.is_ready,
'owner_note': self.owner_note,
```

If `self.character` exists, add a nested `character` dict with `id`, `name`, `race`, `classes`, `total_level` (use `to_dict()` or a simplified version).

### 1c. Add `PlanningConversation` and `PlanningMessage` models

After the `SessionMessage` model block, add:

```python
class PlanningConversation(db.Model):
    __tablename__ = 'planning_conversations'
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('PlanningMessage', backref='conversation', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'campaign_id': self.campaign_id,
            'user_id': self.user_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PlanningMessage(db.Model):
    __tablename__ = 'planning_messages'
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('planning_conversations.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'conversation_id': self.conversation_id,
            'role': self.role,
            'content': self.content,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
```

---

## Step 2: Character Creation System Prompt (`server/openrouter.py`)

Add a new system prompt for the character creation AI:

```python
CHARACTER_CREATION_PROMPT = (
    "You are a D&D Dungeon Master helping a player create a character for your campaign. "
    "Guide the player step by step through creating their character. "
    "Walk through these steps in order:\n"
    "1. Race — Ask about race preference, suggest fitting options for the campaign world.\n"
    "2. Class — Discuss class options that complement the party composition.\n"
    "3. Ability Scores — Help allocate stats using standard array or point buy.\n"
    "4. Background — Suggest a background that ties into the story.\n"
    "5. Skills & Proficiencies — Choose based on class and background.\n"
    "6. Equipment — Select starting gear.\n"
    "7. Personality, Appearance & Backstory — Flesh out the character's identity.\n\n"
    "Campaign context: {campaign_context}\n\n"
    "At each step, present options, ask the player what they prefer, and "
    "once they decide, give a brief summary. "
    "When all steps are complete, provide a final JSON summary of the character. "
    "Keep responses focused and concise."
)
```

Add a new function:
```python
def get_character_creation_response(messages, campaign_context=''):
    if not OPENROUTER_API_KEY:
        return None

    system_content = CHARACTER_CREATION_PROMPT.format(campaign_context=campaign_context)
    formatted = [{'role': 'system', 'content': system_content}]
    for msg in messages:
        role = 'assistant' if msg.role == 'dm' else 'user' if msg.role == 'player' else msg.role
        formatted.append({'role': role, 'content': msg.content})

    try:
        resp = requests.post(
            API_URL,
            headers={
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={'model': OPENROUTER_MODEL, 'messages': formatted},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        print(f'[openrouter] Character creation error: {e}')
        return None
```

---

## Step 3: Planning Routes Blueprint (`server/routes/planning.py`)

Create a new file with this blueprint. All endpoints require `@token_required`.

### Endpoints

#### 3a. `POST /api/campaigns/<id>/begin-planning`
- Only campaign owner can call
- Sets `campaign.phase = 'planning'`
- Creates a `PlanningConversation` for each existing member (excluding owner if they already have a character? No, create for each member on demand)
- Returns updated campaign

#### 3b. `GET /api/campaigns/<id>/planning-status`
- Returns:
  - campaign phase
  - list of members with: user info, character info (if selected), is_ready, owner_note
  - Whether current user is owner

#### 3c. `POST /api/campaigns/<id>/select-character`
- Body: `{ character_id: int }`
- Validates character belongs to current user
- Updates `CampaignMember.character_id` for the current user in this campaign
- Returns success

#### 3d. `DELETE /api/campaigns/<id>/select-character`
- Clears `CampaignMember.character_id` and sets `is_ready = False`
- Returns success

#### 3e. `POST /api/campaigns/<id>/toggle-ready`
- Toggles `CampaignMember.is_ready`
- Cannot be ready without a character selected
- Returns updated member

#### 3f. `GET /api/campaigns/<id>/planning-chat`
- Gets or creates the `PlanningConversation` for the current user + campaign
- Returns conversation with all messages

#### 3g. `POST /api/campaigns/<id>/planning-chat`
- Body: `{ content: string }`
- Gets/creates conversation for current user
- Saves user message
- Calls `get_character_creation_response(conversation.messages, campaign_context)`
- Saves AI response
- Returns both messages

#### 3h. `POST /api/campaigns/<id>/planning-chat/finalize`
- Takes the last AI message, tries to extract JSON character data
- Creates a new `Character` using `build_character_from_data`
- Updates `CampaignMember.character_id`
- Deletes the conversation (cleanup)
- Returns the character

#### 3i. `PUT /api/campaigns/<id>/members/<uid>/note`
- Only campaign owner
- Updates `CampaignMember.owner_note`
- Returns updated member

#### 3j. `POST /api/campaigns/<id>/begin-session`
- Only campaign owner
- All members must have characters assigned and be ready
- Sets `campaign.phase = 'playing'`
- Creates a `CampaignSession` (same as existing start session but also sets phase)
- Returns campaign + session

---

## Step 4: Register Blueprint (`server/app.py`)

Add import:
```python
from routes.planning import planning_bp
```

Register:
```python
app.register_blueprint(planning_bp)
```

---

## Step 5: Client API Functions (`client/src/api/client.js`)

Add:

```javascript
// Character Planning
export function beginPlanning(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/begin-planning`, { method: 'POST' })
}

export function getPlanningStatus(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/planning-status`)
}

export function selectCharacter(campaignId, characterId) {
  return apiFetch(`/campaigns/${campaignId}/select-character`, {
    method: 'POST',
    body: JSON.stringify({ character_id: characterId }),
  })
}

export function deselectCharacter(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/select-character`, { method: 'DELETE' })
}

export function toggleReady(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/toggle-ready`, { method: 'POST' })
}

export function getPlanningChat(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/planning-chat`)
}

export function sendPlanningMessage(campaignId, content) {
  return apiFetch(`/campaigns/${campaignId}/planning-chat`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  })
}

export function finalizePlanningCharacter(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/planning-chat/finalize`, { method: 'POST' })
}

export function setMemberNote(campaignId, userId, note) {
  return apiFetch(`/campaigns/${campaignId}/members/${userId}/note`, {
    method: 'PUT',
    body: JSON.stringify({ note }),
  })
}

export function beginSessionWithPlanning(campaignId) {
  return apiFetch(`/campaigns/${campaignId}/begin-session`, { method: 'POST' })
}
```

---

## Step 6: Client Components

### 6a. `PlanningStatusBar` (`client/src/components/planning/PlanningStatusBar.jsx`)

Shows a horizontal progress bar of ready members: `"3/4 Ready"`. Each member shown as a small avatar circle with a checkmark or clock icon.

Props: `members`, `currentUserId`

### 6b. `CharacterPicker` (`client/src/components/planning/CharacterPicker.jsx`)

Modal that loads the user's characters and lets them pick one:
- Fetches `getCharacters()` on mount
- Filters to characters not already assigned to this campaign
- Displays each as a card with name, race, class/level
- "Select" button per character
- "Create with DM" button as a secondary option
- Props: `campaignId`, `onSelect(characterId)`, `onStartDmCreation()`, `onClose()`

### 6c. `CharacterCreationChat` (`client/src/components/planning/CharacterCreationChat.jsx`)

Chat interface for the DM-guided character creation walkthrough:
- Shows existing messages from `getPlanningChat()`
- Text input + send button at bottom
- "Thinking" dots while waiting for DM
- "Finalize Character" button at end (player-initiated when done)
- Step progress indicator showing where in the walkthrough they are

Props: `campaignId`, `onCharacterCreated(character)`, `onBack()`

### 6d. `CharacterPlanningMode` (`client/src/components/planning/CharacterPlanningMode.jsx`)

Main planning mode component. Three states per user:
1. **Selection**: Show two options (Use Existing / Create with DM) if no character chosen yet
2. **Picking**: Show `CharacterPicker`
3. **Creating**: Show `CharacterCreationChat`
4. **Ready**: Show selected character card + "Ready" toggle

Layout:
- **Left panel**: Member list with status (who's ready, who has a character, etc.)
- **Center panel**: The current user's action area
- **Right panel** (owner only): Overview of all members + note input per member

Props: `campaign`, `currentUser`, `members`, `onComplete()`

### 6e. CampaignViewPage Updates

In the initial load function, check `campaign.phase`:
- `lobby` → show `CampaignLobby`
- `planning` → show `CharacterPlanningMode`
- `playing` → show dashboard

### 6f. CampaignLobby Updates

The "Begin Adventure" button now calls `POST /campaigns/<id>/begin-planning` and notifies parent to reload, instead of just calling `onBegin()`.

---

## Step 7: CSS Styles (`client/src/App.css`)

Append these style blocks:

### Planning Mode Styles
```css
/* Planning Mode Layout */
.planning-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.planning-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.planning-title { font-size: 24px; font-weight: 700; color: #e8e8e8; margin: 0; }

.planning-layout {
  display: grid;
  grid-template-columns: 240px 1fr 280px;
  gap: 16px;
  flex: 1;
  min-height: 0;
}

/* Member Status Panel */
.planning-member-list { display: flex; flex-direction: column; gap: 8px; }

.planning-member-card {
  background: #1f1f2e;
  border: 1px solid #2e2e42;
  border-radius: 12px;
  padding: 12px;
  transition: border-color 0.2s;
}

.planning-member-card.self { border-color: #5e5eff; }

.planning-member-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.planning-member-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}

.planning-member-name { font-size: 13px; font-weight: 600; color: #e8e8e8; }

.planning-member-status { font-size: 11px; display: flex; align-items: center; gap: 4px; }
.planning-status-ready { color: #4ade80; }
.planning-status-pending { color: #facc15; }
.planning-status-empty { color: #6b7280; }

.planning-member-char {
  font-size: 12px;
  color: #a78bfa;
  margin: 4px 0 0;
}

.planning-member-note {
  font-size: 11px;
  color: #fbbf24;
  font-style: italic;
  background: rgba(251, 191, 36, 0.08);
  padding: 6px 8px;
  border-radius: 6px;
  margin-top: 6px;
}

/* Progress Bar */
.planning-progress-bar {
  height: 6px;
  background: #2a2a3e;
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 16px;
}

.planning-progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #6c63ff, #4ade80);
  border-radius: 3px;
  transition: width 0.5s cubic-bezier(0.16, 1, 0.3, 1);
}

/* Center Panel - Action Area */
.planning-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 400px;
}

.planning-welcome {
  text-align: center;
  max-width: 400px;
}

.planning-welcome h3 { font-size: 20px; color: #e8e8e8; margin: 0 0 8px; }
.planning-welcome p { font-size: 14px; color: #9ca3af; line-height: 1.5; margin: 0 0 24px; }

.planning-actions {
  display: flex;
  gap: 12px;
  justify-content: center;
  flex-wrap: wrap;
}

.planning-action-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 24px 32px;
  background: #1f1f2e;
  border: 1px solid #2e2e42;
  border-radius: 16px;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s, transform 0.15s;
  min-width: 180px;
  color: inherit;
}

.planning-action-btn:hover {
  border-color: #5e5eff;
  background: #2a1f4e;
  transform: translateY(-2px);
}

.planning-action-btn:active {
  transform: scale(0.97);
}

.planning-action-icon {
  font-size: 32px;
  color: #a78bfa;
  line-height: 1;
}

.planning-action-label {
  font-size: 14px;
  font-weight: 600;
  color: #e8e8e8;
}

.planning-action-desc {
  font-size: 12px;
  color: #9ca3af;
}

/* Selected Character Card */
.planning-selected-char {
  background: #1f1f2e;
  border: 1px solid #5e5eff;
  border-radius: 16px;
  padding: 24px;
  text-align: center;
  max-width: 320px;
}

.planning-selected-char h3 { color: #e8e8e8; margin: 0 0 4px; }
.planning-selected-char p { color: #a78bfa; font-size: 14px; margin: 0 0 16px; }

/* Ready Toggle */
.planning-ready-btn {
  padding: 12px 32px;
  font-size: 16px;
  font-weight: 700;
  border-radius: 12px;
  border: none;
  cursor: pointer;
  transition: all 0.2s;
}

.planning-ready-btn.ready {
  background: #4ade80;
  color: #0d0d14;
}

.planning-ready-btn.not-ready {
  background: #2a2a3e;
  color: #6b7280;
}

.planning-ready-btn:hover:not(:disabled) {
  transform: translateY(-1px);
}

/* Owner Review Panel */
.planning-right-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.planning-section-label {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #b8b8c8;
  padding: 0 4px;
}

.planning-owner-note-input {
  width: 100%;
  background: #16171d;
  border: 1px solid #2e2e42;
  border-radius: 8px;
  padding: 8px 10px;
  color: #e8e8e8;
  font-size: 12px;
  resize: vertical;
  min-height: 60px;
  outline: none;
  box-sizing: border-box;
}

.planning-owner-note-input:focus {
  border-color: #5e5eff;
}

/* Start Session Button */
.planning-start-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 14px 24px;
  background: linear-gradient(135deg, #6c63ff, #a855f7);
  color: #fff;
  border: none;
  border-radius: 12px;
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  transition: opacity 0.2s, transform 0.15s;
}

.planning-start-btn:hover:not(:disabled) {
  opacity: 0.92;
  transform: translateY(-1px);
}

.planning-start-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Character Picker Modal */
.planning-picker-modal { max-width: 600px; }

.planning-picker-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.planning-picker-char {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  background: #16171d;
  border: 1px solid #2e2e42;
  border-radius: 10px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  text-align: left;
  color: inherit;
}

.planning-picker-char:hover {
  border-color: #5e5eff;
  background: #1a1a28;
}

.planning-picker-avatar {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  flex-shrink: 0;
}

.planning-picker-info { flex: 1; min-width: 0; }
.planning-picker-name { font-size: 14px; font-weight: 600; color: #e8e8e8; }
.planning-picker-meta { font-size: 12px; color: #8888a0; }

/* Character Creation Chat */
.planning-chat {
  display: flex;
  flex-direction: column;
  height: 500px;
  width: 100%;
  max-width: 600px;
  background: #1a1a28;
  border: 1px solid #2e2e42;
  border-radius: 16px;
  overflow: hidden;
}

.planning-chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.planning-chat-msg {
  max-width: 85%;
  animation: msgIn 0.2s ease;
}

.planning-chat-msg.user { align-self: flex-end; }
.planning-chat-msg.dm { align-self: flex-start; }

.planning-chat-msg-content {
  font-size: 14px;
  line-height: 1.5;
  color: #d1d5db;
  padding: 10px 14px;
  border-radius: 10px;
  background: #1f1f2e;
  border: 1px solid #2e2e42;
}

.planning-chat-msg.user .planning-chat-msg-content {
  background: #2a1f4e;
  border-color: #3a2a6e;
  color: #e8e8e8;
}

.planning-chat-msg.dm .planning-chat-msg-content {
  background: #1f1f2e;
  border-color: #3a3a2e;
  color: #f5f5dc;
  font-style: italic;
}

.planning-chat-input-area {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid #2e2e42;
  background: #1f1f2e;
}

.planning-chat-input {
  flex: 1;
  resize: none;
  font-size: 14px;
  min-height: 40px;
}

.planning-chat-send-btn {
  align-self: flex-end;
  padding: 8px 20px;
}

.planning-chat-finalize {
  padding: 0 16px 12px;
}

.planning-chat-thinking {
  font-size: 1.5rem;
  letter-spacing: 0.25em;
  color: #a78bfa;
}

/* Responsive */
@media (max-width: 900px) {
  .planning-layout {
    grid-template-columns: 1fr;
  }
  .planning-layout > * {
    display: none;
  }
  .planning-layout .planning-center {
    display: flex;
  }
}
```

---

## Step 8: Execution Order

1. Edit `server/models.py` — add phase, update CampaignMember, add Planning tables
2. Edit `server/openrouter.py` — add character creation prompt + function
3. Create `server/routes/planning.py` — all endpoints
4. Edit `server/app.py` — register blueprint
5. Edit `client/src/api/client.js` — add API functions
6. Create `client/src/components/planning/PlanningStatusBar.jsx`
7. Create `client/src/components/planning/CharacterPicker.jsx`
8. Create `client/src/components/planning/CharacterCreationChat.jsx`
9. Create `client/src/components/planning/CharacterPlanningMode.jsx`
10. Edit `client/src/pages/CampaignViewPage.jsx` — add planning phase support
11. Edit `client/src/components/dashboard/CampaignLobby.jsx` — update begin button
12. Edit `client/src/App.css` — add planning mode styles

---

## To Execute This Plan

Run each edit and file creation step in order. After all changes, restart the Flask server and Vite dev server. You may need to delete `instance/dnd.db` to pick up schema changes (SQLite schema migration).

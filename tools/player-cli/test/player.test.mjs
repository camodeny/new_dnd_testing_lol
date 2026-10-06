import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:http';
import { Player } from '../src/player.mjs';
import { main } from '../bin/dnd.mjs';

async function fixture(t, overrides = {}) {
  const stateDir = await mkdtemp(join(tmpdir(), 'dnd-cli-'));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const requests = [];
  const snapshot = {
    campaign: { id: 'campaign', owner_id: 'owner', status: 'active', revision: 1, updated_at: 'one' },
    active_thread_id: 'shared', threads: [{ id: 'shared' }], history: { messages: [], pagination: { has_more: false } },
    dm_state: { status: 'idle' }, dm_messages: [], roll_requests: [], encounter: null, surfaces: {},
  };
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    requests.push({ url: req.url, method: req.method, headers: req.headers, body: body ? JSON.parse(body) : null });
    res.setHeader('Content-Type', 'application/json');
    if (overrides.handler && await overrides.handler(req, res, requests.at(-1))) return;
    if (req.url === '/api/me') res.end(JSON.stringify({ user: { id: overrides.user ?? 'player' } }));
    else if (req.url === '/api/campaigns/campaign') res.end(JSON.stringify({ campaign: snapshot.campaign }));
    else if (req.url.startsWith('/api/campaigns/campaign/snapshot')) res.end(JSON.stringify(snapshot));
    else if (req.url === '/api/campaigns/campaign/roll-requests') res.end(JSON.stringify({ roll_requests: snapshot.roll_requests }));
    else res.end(JSON.stringify({ accepted: true }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  return { snapshot, requests, stateDir, baseUrl, player: new Player({ baseUrl, token: 'real-jwt-fixture', stateDir }) };
}

test('observation reports own obligations without assigning speakers', async t => {
  const f = await fixture(t);
  f.snapshot.roll_requests = [{ id: 'mine', status: 'pending', requested_user_id: 'player' }, { id: 'theirs', status: 'pending', requested_user_id: 'other' }];
  const seen = await f.player.observe('campaign', { thread: 'private', cursor: 'abc' });
  assert.deepEqual(seen.required_actions.map(a => a.request_id), ['mine']);
  assert.equal(seen.respond_now, undefined);
  assert.match(f.requests.at(-1).url, /thread_id=private.*cursor=abc/);
  assert.ok(f.requests.every(r => r.headers.authorization === 'Bearer real-jwt-fixture'));
});

test('campaign owner observes as an ordinary seat', async t => {
  const f = await fixture(t, { user: 'owner' });
  const seen = await f.player.observe('campaign');
  assert.equal(seen.user_id, 'owner');
  assert.ok(f.requests.some(r => r.url.includes('/snapshot')));
});

test('hidden revision advances do not wake a player; visible messages do', async t => {
  const f = await fixture(t);
  const first = await f.player.observe('campaign');
  f.snapshot.campaign.revision++;
  f.snapshot.campaign.updated_at = 'two';
  assert.equal((await f.player.observe('campaign')).change_token, first.change_token);
  f.snapshot.history.messages.push({ id: 'new', content: 'Hello' });
  const awake = await f.player.wait('campaign', { since: first.change_token, timeout: 0 });
  assert.equal(awake.reason, 'changed');
});

test('wait times out normally and detects changes during polling', async t => {
  const f = await fixture(t);
  assert.equal((await f.player.wait('campaign', { timeout: 0 })).reason, 'timeout');
  const token = (await f.player.observe('campaign')).change_token;
  const timer = setTimeout(() => { f.snapshot.dm_state = { status: 'awaiting_roll' }; }, 20);
  t.after(() => clearTimeout(timer));
  assert.equal((await f.player.wait('campaign', { since: token, timeout: 1, interval: 0.03 })).reason, 'changed');
});

test('lost submission response exposes operation ID and preserves retry payload', async t => {
  let drop = true;
  const f = await fixture(t, { handler(req, res) {
    if (req.url.endsWith('/submissions') && drop) { drop = false; res.destroy(); return true; }
  } });
  const input = { type: 'ic', text: 'I look around.', operationId: 'stable' };
  await assert.rejects(f.player.say('campaign', input), error => error.code === 'transport_error' && error.details.operation_id === 'stable');
  await f.player.say('campaign', input);
  const calls = f.requests.filter(r => r.url.endsWith('/submissions'));
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].body, calls[1].body);
  assert.deepEqual(calls[0].body.segments, [{ type: 'ic', text: 'I look around.' }]);
  await assert.rejects(f.player.say('campaign', { ...input, text: 'Different' }), { code: 'operation_conflict' });
});

test('advantage dice arithmetic and retries are stable even after fulfillment', async t => {
  const f = await fixture(t);
  f.snapshot.roll_requests = [{ id: 'roll', status: 'pending', requested_user_id: 'player', advantage_state: 'advantage' }];
  await f.player.roll('campaign', 'roll', { modifier: 3, operationId: 'dice' });
  f.snapshot.roll_requests[0].status = 'fulfilled';
  await f.player.roll('campaign', 'roll', { modifier: 3, operationId: 'dice' });
  const calls = f.requests.filter(r => r.url.endsWith('/fulfill'));
  assert.deepEqual(calls[0].body, calls[1].body);
  const payload = calls[0].body;
  assert.equal(payload.raw_rolls.length, 2);
  assert.ok(payload.raw_rolls.every(d => d >= 1 && d <= 20));
  assert.equal(payload.total, Math.max(...payload.raw_rolls) + 3);
});

test('cannot roll for another player', async t => {
  const f = await fixture(t);
  f.snapshot.roll_requests = [{ id: 'roll', status: 'pending', requested_user_id: 'other', advantage_state: 'normal' }];
  await assert.rejects(f.player.roll('campaign', 'roll', { modifier: 0 }), { code: 'invalid_roll' });
  assert.equal(f.requests.some(r => r.url.endsWith('/fulfill')), false);
});

test('HTTP rejection is preserved and mutations are not automatically retried', async t => {
  const f = await fixture(t, { handler(req, res) {
    if (req.url.endsWith('/submissions')) { res.statusCode = 409; res.setHeader('X-Current-Revision', '9'); res.end(JSON.stringify({ detail: 'Conflict' })); return true; }
  } });
  await assert.rejects(f.player.say('campaign', { type: 'ooc', text: 'Hi' }), error => error.code === 'http_error' && error.details.status === 409 && error.details.current_revision === '9' && Boolean(error.details.operation_id));
  assert.equal(f.requests.filter(r => r.url.endsWith('/submissions')).length, 1);
});

test('login stores private session, refreshes and does not expose tokens', async t => {
  let grants = [];
  const f = await fixture(t, { handler(req, res) {
    if (req.url.startsWith('/auth/v1/token')) {
      grants.push(req.url);
      res.end(JSON.stringify({ access_token: 'saved-token', refresh_token: 'refresh-secret', expires_in: grants.length === 1 ? 0 : 3600, user: { id: 'player' } }));
      return true;
    }
  } });
  const player = new Player({ baseUrl: f.baseUrl, stateDir: f.stateDir, profile: 'alice' });
  const output = await player.login({ supabaseUrl: f.baseUrl, key: 'public-key', email: 'alice@example.test', password: 'secret' });
  assert.deepEqual(output, { authenticated: true, user_id: 'player' });
  const stored = join(f.stateDir, 'alice', 'session.json');
  assert.equal((await stat(stored)).mode & 0o777, 0o600);
  const saved = JSON.parse(await readFile(stored, 'utf8'));
  assert.equal(saved.refresh_token, 'refresh-secret');
  assert.equal(saved.password, undefined);
  const fresh = new Player({ baseUrl: f.baseUrl, stateDir: f.stateDir, profile: 'alice' });
  await fresh.request('/me');
  assert.equal(grants.length, 2);
  assert.match(grants[1], /refresh_token/);
  const wrongOrigin = new Player({ baseUrl: 'https://another.example', stateDir: f.stateDir, profile: 'alice' });
  await assert.rejects(wrongOrigin.accessToken(), { code: 'session_origin_mismatch' });
});

test('CLI validates ambiguous or unsupported inputs before mutation', async t => {
  const f = await fixture(t);
  const env = { DND_BASE_URL: f.baseUrl, DND_ACCESS_TOKEN: 'jwt', DND_CAMPAIGN_ID: 'campaign', DND_STATE_DIR: f.stateDir };
  for (const args of [['say', '--ic', 'Hi', '--ooc', 'Hi'], ['observe', '--ic', 'Hi'], ['roll', 'id'], ['move', 'id', '--col', 'NaN', '--row', '2'], ['wait', '--interval', '0'], ['unknown']]) {
    await assert.rejects(main(args, env), { code: 'usage' });
  }
  assert.equal(f.requests.some(r => r.method !== 'GET'), false);
});

test('profile lock prevents overlapping commands and releases after failure', async t => {
  const f = await fixture(t);
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  let entered;
  const started = new Promise(resolve => { entered = resolve; });
  const held = f.player.exclusive(async () => { entered(); await blocked; });
  await started;
  await assert.rejects(f.player.exclusive(async () => {}), { code: 'profile_busy' });
  const bob = new Player({ baseUrl: f.baseUrl, token: 'bob-jwt', profile: 'bob', stateDir: f.stateDir });
  await bob.exclusive(async () => {});
  release();
  await held;
  await assert.rejects(f.player.exclusive(async () => { throw new Error('failure'); }), /failure/);
  await f.player.exclusive(async () => {});
});

test('move and end-turn bind observed revision/sequence; retry retains original turn', async t => {
  let sequence = 4;
  const f = await fixture(t, { handler(req, res) {
    if (req.url.endsWith('/turn-state')) { res.end(JSON.stringify({ turn: { turn_sequence: sequence } })); return true; }
  } });
  const env = { DND_BASE_URL: f.baseUrl, DND_ACCESS_TOKEN: 'jwt', DND_CAMPAIGN_ID: 'campaign', DND_STATE_DIR: f.stateDir };
  const args = ['move', 'enc', '--participant', 'pc', '--col', '8', '--row', '12', '--operation-id', 'movement'];
  await main(args, env);
  sequence = 5;
  f.snapshot.campaign.revision = 2;
  await main(args, env);
  const moves = f.requests.filter(r => r.url.endsWith('/move'));
  assert.deepEqual(moves[0].body, moves[1].body);
  assert.equal(moves[0].body.expected_revision, 1);
  assert.equal(moves[0].body.expected_turn_sequence, 4);
  assert.deepEqual(moves[0].body.to, { col: 8, row: 12 });
  await main(['end-turn', 'enc'], env);
  const end = f.requests.find(r => r.url.endsWith('/end-turn'));
  assert.equal(end.body.expected_revision, 2);
  assert.equal(end.body.expected_turn_sequence, 5);
});

test('independent seats retain separate identity and journals', async t => {
  const f = await fixture(t, { handler(req, res) {
    if (req.url === '/api/me') { res.end(JSON.stringify({ user: { id: req.headers.authorization === 'Bearer bob-jwt' ? 'bob' : 'player' } })); return true; }
  } });
  const bob = new Player({ baseUrl: f.baseUrl, token: 'bob-jwt', profile: 'bob', stateDir: f.stateDir });
  f.snapshot.roll_requests = [{ id: 'alice-roll', status: 'pending', requested_user_id: 'player' }, { id: 'bob-roll', status: 'pending', requested_user_id: 'bob' }];
  const [aliceState, bobState] = await Promise.all([f.player.observe('campaign'), bob.observe('campaign')]);
  assert.equal(aliceState.required_actions[0].request_id, 'alice-roll');
  assert.equal(bobState.required_actions[0].request_id, 'bob-roll');
  await Promise.all([f.player.say('campaign', { type: 'ic', text: 'Alice speaks', operationId: 'same-id' }), bob.say('campaign', { type: 'ic', text: 'Bob speaks', operationId: 'same-id' })]);
  assert.equal(f.requests.filter(r => r.url.endsWith('/submissions')).length, 2);
});

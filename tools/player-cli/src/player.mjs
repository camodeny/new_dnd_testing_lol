import { createHash, randomInt, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, unlink } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export class CliError extends Error {
  constructor(code, message, details = {}) { super(message); this.code = code; this.details = details; }
}
export const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const segment = value => encodeURIComponent(String(value));

export class Player {
  constructor({ baseUrl, token, profile = 'default', stateDir, fetchImpl = fetch }) {
    if (!/^[a-zA-Z0-9_-]+$/.test(profile)) throw new CliError('invalid_profile', 'Use letters, numbers, underscores or hyphens for the profile.');
    let url;
    try { url = new URL(baseUrl); } catch { throw new CliError('invalid_url', 'DND_BASE_URL must be a valid HTTP(S) origin.'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new CliError('invalid_url', 'DND_BASE_URL must be an HTTP(S) origin, without a path or credentials.');
    }
    if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
      throw new CliError('invalid_url', 'Use HTTPS except for local development.');
    }
    this.baseUrl = url.origin;
    this.token = token;
    this.fetch = fetchImpl;
    this.dir = join(stateDir ?? join(homedir(), '.config', 'dnd-player'), profile);
  }

  async json(url, { headers = {}, ...options } = {}) {
    let response;
    try {
      response = await this.fetch(url, { ...options, headers, redirect: 'error', signal: options.signal ?? AbortSignal.timeout(15000) });
    } catch {
      throw new CliError('transport_error', 'Request failed or timed out. A mutation may have committed; retry with the same operation ID.');
    }
    let data;
    try { data = await response.json(); } catch { throw new CliError('invalid_response', 'Server returned a non-JSON response.', { status: response.status }); }
    if (!response.ok) throw new CliError(response.status === 401 ? 'authentication_required' : 'http_error', `HTTP ${response.status}`, { status: response.status, detail: data.detail ?? data.error, current_revision: response.headers.get('X-Current-Revision') });
    return data;
  }

  async saveSession(session) {
    await mkdir(this.dir, { recursive: true, mode: 0o700 });
    await writeFile(join(this.dir, 'session.json'), JSON.stringify(session), { mode: 0o600 });
  }

  async exclusive(run) {
    await mkdir(this.dir, { recursive: true, mode: 0o700 });
    const lock = join(this.dir, 'run.lock');
    try { await writeFile(lock, String(process.pid), { flag: 'wx', mode: 0o600 }); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      throw new CliError('profile_busy', 'Another command holds this profile lock. After a crash, verify that process has stopped before removing run.lock.', { lock_file: lock });
    }
    try { return await run(); } finally { await unlink(lock); }
  }

  async login({ supabaseUrl, key, email, password }) {
    if (!supabaseUrl || !key || !email || !password) throw new CliError('configuration_required', 'Login requires DND_SUPABASE_URL, DND_SUPABASE_KEY, DND_EMAIL and DND_PASSWORD.');
    let url;
    try { url = new URL(supabaseUrl); } catch { throw new CliError('invalid_url', 'DND_SUPABASE_URL must be a valid HTTP(S) origin.'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new CliError('invalid_url', 'Supabase URL must be an HTTP(S) origin without credentials or a path.');
    if (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new CliError('invalid_url', 'Supabase must use HTTPS except locally.');
    const session = await this.json(`${url.origin}/auth/v1/token?grant_type=password`, {
      method: 'POST', headers: { apikey: key, 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }),
    });
    await this.saveSession({ baseUrl: this.baseUrl, supabaseUrl: url.origin, key, access_token: session.access_token, refresh_token: session.refresh_token, expires_at: Date.now() + session.expires_in * 1000 });
    this.token = session.access_token;
    return { authenticated: true, user_id: session.user.id };
  }

  async accessToken() {
    if (this.token) return this.token;
    let session;
    try { session = JSON.parse(await readFile(join(this.dir, 'session.json'), 'utf8')); }
    catch { throw new CliError('authentication_required', 'Run login for this profile or supply DND_ACCESS_TOKEN (a real Supabase JWT).'); }
    if (session.baseUrl !== this.baseUrl) throw new CliError('session_origin_mismatch', 'This profile was authenticated for another backend origin.');
    if (session.expires_at <= Date.now() + 30000) {
      const refreshed = await this.json(`${session.supabaseUrl}/auth/v1/token?grant_type=refresh_token`, {
        method: 'POST', headers: { apikey: session.key, 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: session.refresh_token }),
      });
      session = { ...session, access_token: refreshed.access_token, refresh_token: refreshed.refresh_token, expires_at: Date.now() + refreshed.expires_in * 1000 };
      await this.saveSession(session);
    }
    // Re-read expiry for every command/poll; do not cache a saved session token.
    return session.access_token;
  }

  async request(path, options = {}) {
    return this.json(`${this.baseUrl}/api${path}`, { ...options, headers: { Authorization: `Bearer ${await this.accessToken()}`, 'Content-Type': 'application/json', ...options.headers } });
  }
  path(campaign, suffix = '') { return `/campaigns/${segment(campaign)}${suffix}`; }

  async member(campaign) {
    // The campaign read checks membership. Owners are ordinary seats: every
    // human, owner included, receives the member projection (#470).
    const [identity] = await Promise.all([this.request('/me'), this.request(this.path(campaign))]);
    return identity.user;
  }

  async observe(campaign, { thread = 'main', limit = 50, cursor, signal } = {}) {
    const user = await this.member(campaign);
    const query = new URLSearchParams({ thread_id: thread, limit: String(limit) });
    if (cursor) query.set('cursor', cursor);
    const snapshot = await this.request(this.path(campaign, `/snapshot?${query}`), { signal });
    const required = snapshot.roll_requests.filter(r => r.status === 'pending' && r.requested_user_id === user.id);
    const observation = {
      user_id: user.id,
      campaign: snapshot.campaign,
      active_thread_id: snapshot.active_thread_id,
      threads: snapshot.threads,
      history: snapshot.history,
      dm_state: snapshot.dm_state,
      dm_messages: snapshot.dm_messages,
      roll_requests: snapshot.roll_requests,
      encounter: snapshot.encounter,
      surfaces: snapshot.surfaces,
      required_actions: required.map(r => ({ type: 'roll', request_id: r.id, thread_id: r.thread_id, label: r.label })),
      submission: { campaign_status_allows: snapshot.campaign.status !== 'archived', authorization: 'checked_by_server_on_submit' },
    };
    // Revision and generated timestamps can advance due to another player's
    // private activity. Never use them as wakeup signals.
    const { revision, updated_at, ...campaignState } = observation.campaign;
    observation.change_token = hash({ ...observation, campaign: campaignState });
    return observation;
  }

  async wait(campaign, { since, timeout = 60, interval = 2, ...options } = {}) {
    const deadline = Date.now() + timeout * 1000;
    let observation = await this.observe(campaign, options);
    const baseline = since ?? observation.change_token;
    while (observation.change_token === baseline && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, Math.min(interval * 1000, Math.max(0, deadline - Date.now()))));
      if (Date.now() >= deadline) break;
      observation = await this.observe(campaign, options);
    }
    return { reason: observation.change_token === baseline ? 'timeout' : 'changed', observation };
  }

  async mutate(campaign, suffix, input, prepare = () => input, operationId = randomUUID(), method = 'POST') {
    if (typeof operationId !== 'string' || !operationId.trim()) throw new CliError('invalid_input', 'Operation ID must be nonempty.');
    const user = await this.member(campaign);
    const path = this.path(campaign, suffix);
    const filename = join(this.dir, 'operations', `${hash([this.baseUrl, user.id, operationId])}.json`);
    await mkdir(join(this.dir, 'operations'), { recursive: true, mode: 0o700 });
    let record;
    try { record = JSON.parse(await readFile(filename, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    const signature = hash([path, method, input]);
    if (record && record.signature !== signature) throw new CliError('operation_conflict', 'Operation ID already belongs to different input.');
    if (!record) {
      record = { signature, payload: { ...await prepare(), operation_id: operationId } };
      try { await writeFile(filename, JSON.stringify(record), { flag: 'wx', mode: 0o600 }); }
      catch (error) {
        if (error.code !== 'EEXIST') throw error;
        record = JSON.parse(await readFile(filename, 'utf8'));
        if (record.signature !== signature) throw new CliError('operation_conflict', 'Operation ID already belongs to different input.');
      }
    }
    try {
      const result = await this.request(path, { method, headers: { 'Idempotency-Key': operationId }, body: JSON.stringify(record.payload) });
      return { operation_id: operationId, result };
    } catch (error) {
      if (error instanceof CliError) error.details.operation_id = operationId;
      throw error;
    }
  }

  async say(campaign, { thread = 'main', type, text, operationId }) {
    if (!['ic', 'ooc'].includes(type) || !text?.trim()) throw new CliError('invalid_input', 'Choose --ic or --ooc with nonempty text.');
    return this.mutate(campaign, '/submissions', { thread_id: thread, content: text, segments: [{ type, text }] }, undefined, operationId);
  }

  async roll(campaign, requestId, { modifier, operationId, visibility = 'public' }) {
    if (!Number.isInteger(modifier) || Math.abs(modifier) > 10000) throw new CliError('invalid_input', '--modifier must be an integer from the character sheet.');
    return this.mutate(campaign, `/roll-requests/${segment(requestId)}/fulfill`, { requestId, modifier, visibility }, async () => {
      const user = await this.member(campaign);
      const { roll_requests: rolls } = await this.request(this.path(campaign, '/roll-requests'));
      const request = rolls.find(r => r.id === requestId && r.requested_user_id === user.id);
      if (!request || request.status !== 'pending') throw new CliError('invalid_roll', 'No pending roll owned by this player with that ID.');
      if (request.roll_kind === 'other') throw new CliError('unsupported_roll', 'This request does not specify a standard d20 roll. Clarify with the AI DM.');
      if (request.roll_kind === 'damage') {
        // Damage dice come from code (the hit's weapon, crits already doubled); they are summed.
        const match = /^(\d+)d(\d+)([+-]\d+)?$/.exec(request.damage_dice ?? '');
        if (!match) throw new CliError('unsupported_roll', 'This damage roll has no dice code. Clarify with the AI DM.');
        const dice = Array.from({ length: Number(match[1]) }, () => randomInt(1, Number(match[2]) + 1));
        return { source: 'app', raw_rolls: dice, modifier, total: dice.reduce((sum, d) => sum + d, 0) + modifier, visibility };
      }
      if (!['normal', 'advantage', 'disadvantage'].includes(request.advantage_state)) throw new CliError('unsupported_roll', 'Unknown advantage state.');
      const dice = Array.from({ length: request.advantage_state === 'normal' ? 1 : 2 }, () => randomInt(1, 21));
      const die = request.advantage_state === 'disadvantage' ? Math.min(...dice) : Math.max(...dice);
      return { source: 'app', raw_rolls: dice, modifier, total: die + modifier, visibility };
    }, operationId);
  }
}

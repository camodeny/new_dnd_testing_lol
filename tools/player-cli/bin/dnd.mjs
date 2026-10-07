#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { Player, CliError } from '../src/player.mjs';

const help = `dnd — JSON player interface (Node 20+)

Configuration: DND_BASE_URL, DND_CAMPAIGN_ID, DND_ACCESS_TOKEN
Each player uses a separate --profile and real Supabase account.

Commands:
  login                          Save a real Supabase session for this profile
  me | campaigns | characters   Discover identity, campaigns or owned characters
  character <id>                Read an owned character sheet
  join <invite-code>            Accept an invitation
  select <character-id>         Select your lobby character
  ready | unready               Change your lobby readiness
  observe                       Read player-visible state; makes no speaker decision
  history --cursor <cursor>     Read an older snapshot history page
  wait [--since <change-token>] Wait for visible change; timeout is normal
  say --ic <text>               Submit an in-world action or dialogue
  say --ooc <text>              Submit a table/rules message
  threads                       List your visible threads
  dm                            Open your private AI-DM thread
  direct <user-id>              Open a direct player thread
  rolls                         List visible roll requests
  roll <request-id> --modifier N Roll d20 with request advantage (damage: its dice); calculate in code
  encounter                     Read active encounter
  map <encounter-id>            Read visible map
  reachable <encounter-id> --participant <id>
  move <encounter-id> --participant <id> --col N --row N
  end-turn <encounter-id>       End your current encounter turn

Options:
  --base-url <origin>           Backend or frontend API origin (no /api suffix)
  --campaign <id>               Campaign ID (or DND_CAMPAIGN_ID)
  --profile <name>              Isolated credentials/operation journal (default: default)
  --thread <id>                 Thread ID (default: main); use dm/direct results
  --operation-id <id>           Stable mutation ID; reuse to retry an uncertain result
  --limit N                    Snapshot page size, 1–100 (default: 50)
  --timeout N                  Wait duration in seconds, 0–3600 (default: 60)
  --interval N                 Poll interval in seconds, 0.2–60 (default: 2)
  --visibility public|private  Roll result visibility (default: public)
  --movement-mode <mode>       Movement mode (default: walk)

Login environment: DND_SUPABASE_URL, DND_SUPABASE_KEY (publishable/anon),
DND_EMAIL, DND_PASSWORD. Passwords/tokens are never printed.
Outputs: one JSON object; errors exit 1, usage errors exit 2.
Campaign owners cannot play through this CLI because their projections include secrets.
`;

function number(value, name, min, max, integer = false) {
  const n = Number(value);
  if (value === undefined || !Number.isFinite(n) || n < min || n > max || (integer && !Number.isInteger(n))) throw new CliError('usage', `${name} must be ${integer ? 'an integer' : 'a number'} between ${min} and ${max}.`);
  return n;
}

export async function main(args = process.argv.slice(2), env = process.env) {
  let parsed;
  try {
    parsed = parseArgs({ args, allowPositionals: true, options: {
      help: { type: 'boolean', short: 'h' },
      ...Object.fromEntries(['base-url', 'campaign', 'profile', 'thread', 'operation-id', 'limit', 'cursor', 'since', 'timeout', 'interval', 'ic', 'ooc', 'modifier', 'visibility', 'participant', 'col', 'row', 'movement-mode'].map(k => [k, { type: 'string' }])),
    } });
  } catch (error) { throw new CliError('usage', error.message); }
  const { values: v, positionals: [command, arg, ...extra] } = parsed;
  if (!command || command === 'help' || v.help) return { help };
  const commands = ['login', 'me', 'campaigns', 'characters', 'character', 'join', 'select', 'ready', 'unready', 'observe', 'history', 'wait', 'say', 'threads', 'dm', 'direct', 'rolls', 'roll', 'encounter', 'map', 'reachable', 'move', 'end-turn'];
  if (!commands.includes(command)) throw new CliError('usage', `Unknown command: ${command}`);
  const takesArgument = ['character', 'join', 'select', 'direct', 'roll', 'map', 'reachable', 'move', 'end-turn'].includes(command);
  if (extra.length || (takesArgument ? !arg : arg !== undefined)) throw new CliError('usage', takesArgument ? `${command} requires exactly one argument.` : `${command} takes no positional arguments.`);
  const allowed = {
    observe: ['thread', 'limit', 'cursor'], history: ['thread', 'limit', 'cursor'], wait: ['thread', 'limit', 'since', 'timeout', 'interval'],
    say: ['thread', 'ic', 'ooc', 'operation-id'], roll: ['modifier', 'visibility', 'operation-id'],
    reachable: ['participant', 'movement-mode'], move: ['participant', 'col', 'row', 'movement-mode', 'operation-id'],
    'end-turn': ['operation-id'], select: ['operation-id'], ready: ['operation-id'], unready: ['operation-id'],
  };
  for (const option of Object.keys(v)) {
    if (!['base-url', 'campaign', 'profile', 'help'].includes(option) && !(allowed[command] ?? []).includes(option)) throw new CliError('usage', `--${option} is not supported by ${command}.`);
  }
  const baseUrl = v['base-url'] ?? env.DND_BASE_URL;
  if (!baseUrl) throw new CliError('usage', 'Set DND_BASE_URL or --base-url to the app origin.');
  const player = new Player({ baseUrl, token: env.DND_ACCESS_TOKEN, profile: v.profile ?? 'default', stateDir: env.DND_STATE_DIR });
  const campaign = v.campaign ?? env.DND_CAMPAIGN_ID;
  const standalone = ['login', 'me', 'campaigns', 'characters', 'character', 'join'];
  if (!standalone.includes(command) && !campaign) throw new CliError('usage', 'Set DND_CAMPAIGN_ID or --campaign.');
  const p = suffix => player.path(campaign, suffix);
  const operationId = v['operation-id'];
  const observationOptions = { thread: v.thread ?? 'main', limit: number(v.limit ?? '50', '--limit', 1, 100, true), cursor: v.cursor };
  return player.exclusive(async () => {
  switch (command) {
    case 'login': return player.login({ supabaseUrl: env.DND_SUPABASE_URL, key: env.DND_SUPABASE_KEY, email: env.DND_EMAIL, password: env.DND_PASSWORD });
    case 'me': return player.request('/me');
    case 'campaigns': return player.request('/campaigns');
    case 'characters': return player.request('/characters');
    case 'character': return player.request(`/characters/${encodeURIComponent(arg)}`);
    case 'join': return player.request('/invites/accept', { method: 'POST', body: JSON.stringify({ code: arg }) });
    case 'observe': case 'history': return player.observe(campaign, observationOptions);
    case 'wait': return player.wait(campaign, { ...observationOptions, since: v.since, timeout: number(v.timeout ?? '60', '--timeout', 0, 3600), interval: number(v.interval ?? '2', '--interval', 0.2, 60) });
    case 'say':
      if ((v.ic === undefined) === (v.ooc === undefined)) throw new CliError('usage', 'Supply exactly one of --ic or --ooc.');
      return player.say(campaign, { thread: v.thread ?? 'main', type: v.ic !== undefined ? 'ic' : 'ooc', text: v.ic ?? v.ooc, operationId });
    case 'roll': {
      const visibility = v.visibility ?? 'public';
      if (!['public', 'private'].includes(visibility)) throw new CliError('usage', '--visibility must be public or private.');
      return player.roll(campaign, arg, { modifier: number(v.modifier, '--modifier', -10000, 10000, true), visibility, operationId });
    }
    case 'select': case 'ready': case 'unready': {
      const input = command === 'select' ? { character_id: arg } : { ready: command === 'ready' };
      return player.mutate(campaign, `/members/me/${command === 'select' ? 'character' : 'readiness'}`, input, async () => {
        const { campaign: state } = await player.request(p(''));
        return { ...input, expected_revision: state.revision };
      }, operationId, 'PUT');
    }
    default: {
      await player.member(campaign);
      if (command === 'threads') return player.request(p('/threads'));
      if (command === 'dm') return player.request(p('/threads/dm'), { method: 'POST' });
      if (command === 'direct') return player.request(p('/threads/direct'), { method: 'POST', body: JSON.stringify({ participant_id: arg }) });
      if (command === 'rolls') return player.request(p('/roll-requests'));
      if (command === 'encounter') return player.request(p('/encounters/active'));
      const encounterPath = `/encounters/${encodeURIComponent(arg)}`;
      if (command === 'map') return player.request(p(`${encounterPath}/map`));
      if (command === 'reachable') {
        if (!v.participant) throw new CliError('usage', '--participant is required.');
        const query = new URLSearchParams({ participant_id: v.participant, movement_mode: v['movement-mode'] ?? 'walk' });
        return player.request(p(`${encounterPath}/reachable?${query}`));
      }
      const input = command === 'move' ? {
        participant_id: v.participant,
        to: { col: number(v.col, '--col', 0, 10000, true), row: number(v.row, '--row', 0, 10000, true) },
        movement_mode: v['movement-mode'] ?? 'walk',
      } : {};
      if (command === 'move' && !v.participant) throw new CliError('usage', '--participant is required.');
      return player.mutate(campaign, `${encounterPath}/${command}`, input, async () => {
        // Server checks stale revision/turn sequence atomically; never silently
        // retry a gameplay command against a different turn.
        const [detail, state] = await Promise.all([player.request(p('')), player.request(p(`${encounterPath}/turn-state`))]);
        return { ...input, expected_revision: detail.campaign.revision, expected_turn_sequence: state.turn.turn_sequence };
      }, operationId);
    }
  }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { console.log(JSON.stringify({ ok: true, data: await main() })); }
  catch (error) {
    console.log(JSON.stringify({ ok: false, error: { code: error.code ?? 'internal_error', message: error instanceof CliError ? error.message : 'Unexpected CLI failure.', ...error.details } }));
    process.exitCode = error.code === 'usage' ? 2 : 1;
  }
}

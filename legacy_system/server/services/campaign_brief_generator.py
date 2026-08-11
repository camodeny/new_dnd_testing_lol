import random
import re
import secrets


REGIONS = (
    {
        'id': 'blackwater_port',
        'place': 'Blackwater Port',
        'region': 'a storm-lashed harbor city built on rotten pilings and old vows',
        'landmark': 'the tidewall bells',
        'factions': ('harbormasters', 'salt smugglers', 'debt priests', 'reef wardens'),
        'moods': ('salt-bitten suspense', 'dockside desperation', 'mercantile paranoia'),
        'adjectives': ('Blackwater', 'Tidebound', 'Harbor', 'Saltworn'),
    },
    {
        'id': 'emberfield',
        'place': 'Emberfield',
        'region': 'a fertile frontier plain where old battlefields still smolder below the soil',
        'landmark': 'the ashglass orchards',
        'factions': ('grain barons', 'veteran militias', 'hearth witches', 'rail agents'),
        'moods': ('frontier ambition', 'postwar unease', 'hard-bitten optimism'),
        'adjectives': ('Ember', 'Ashen', 'Harvest', 'Redplain'),
    },
    {
        'id': 'glassmere',
        'place': 'Glassmere',
        'region': 'a misty lake district where drowned villas and mirrored shrines surface at odd hours',
        'landmark': 'the reflected basilica',
        'factions': ('mirror monks', 'salvage divers', 'lake nobility', 'ferry guilds'),
        'moods': ('haunted elegance', 'ritual tension', 'quiet dread'),
        'adjectives': ('Glass', 'Mirrored', 'Drowned', 'Silvermere'),
    },
    {
        'id': 'copperhollow',
        'place': 'Copperhollow',
        'region': 'a canyon rail settlement fed by mining lifts, shrine tunnels, and boomtown grudges',
        'landmark': 'the hanging switchyard',
        'factions': ('mine syndicates', 'trackrunners', 'canyon sheriffs', 'sainted machinists'),
        'moods': ('industrial strain', 'dusty bravado', 'knife-edge politics'),
        'adjectives': ('Copper', 'Hollow', 'Canyon', 'Ironwind'),
    },
    {
        'id': 'moonfen',
        'place': 'Moonfen',
        'region': 'a lantern-lit marsh of raised causeways, singing insects, and impossible tax claims',
        'landmark': 'the drowned customs house',
        'factions': ('bog reeves', 'fen pilgrims', 'lampwrights', 'reed smugglers'),
        'moods': ('humid intrigue', 'superstitious endurance', 'swamp noir'),
        'adjectives': ('Moonfen', 'Lantern', 'Reed', 'Mire'),
    },
)

PRESSURES = (
    'a sudden levy is driving families to desperate bargains',
    'caravans keep vanishing along what should be the safest route in the region',
    'a truce between rival powers is hours from collapse',
    'something valuable has been stolen and every faction blames the wrong enemy',
    'a strange illness is spreading with political consequences faster than physical ones',
    'recent omens have made public fear easier to weaponize than reason',
    'an inheritance dispute is about to turn armed and very public',
    'someone is sabotaging the only system keeping daily life stable',
)

OPENING_SITUATIONS = (
    'The party arrives just as a public accusation interrupts a routine exchange.',
    'The first scene opens in the middle of a tense civic gathering that was supposed to stay ceremonial.',
    'The adventure begins with a practical job that becomes politically dangerous within minutes.',
    'The party is drawn together by a local emergency that no one can admit is already out of control.',
    'The opening encounter starts with a missing person report that clearly hides a second agenda.',
)

TWISTS = (
    'the obvious villain is being used as cover by a quieter faction with cleaner hands',
    'the people asking for help already made a bargain they are trying to hide',
    'the most reliable local authority is protecting one truth and burying another',
    'the crisis is real, but the timetable has been manipulated to force a bad decision',
    'two enemies who should hate each other are secretly aligned around a shared fear',
    'the first reward offered to the party is designed to make them politically owned',
)

HOOKS = (
    'The party can secure allies quickly, but only by choosing who gets embarrassed in public.',
    'Every early success changes who feels safe speaking honestly to the party.',
    'The first major clue is easy to find and difficult to survive politically.',
    'The problem looks local until the party follows who profits from keeping it local.',
    'The opening job becomes a test of whether the party values stability, truth, or leverage more.',
)

TITLE_OBJECTS = (
    'Debt', 'Bell', 'Crown', 'Oath', 'Lantern', 'Harvest', 'Mirror', 'Vault',
    'Bridge', 'Trial', 'Choir', 'Engine', 'Relic', 'Tide', 'Charter', 'Mask',
)

TITLE_PATTERNS = (
    'The {object} of {place}',
    '{place} Under {object}',
    'The {adjective} {object}',
    '{object} at {place}',
    'The {object} Below {place}',
)

DIFFICULTIES = ('Easy', 'Medium', 'Hard', 'Deadly')
LOOT_MODES = ('frequent_gamble', 'rare_quality')


def _slugify_seed_text(value):
    cleaned = re.sub(r'[^a-z0-9]+', '-', str(value).strip().lower()).strip('-')
    return cleaned[:64] or 'campaign'


def generate_seed(provided_seed=None):
    if provided_seed:
        return _slugify_seed_text(provided_seed)
    parts = [
        secrets.choice(('ashen', 'gilded', 'hollow', 'lantern', 'salt', 'verdant', 'iron', 'moon')),
        secrets.choice(('accord', 'bell', 'bridge', 'charter', 'choir', 'engine', 'harvest', 'mask')),
        str(secrets.randbelow(9000) + 1000),
    ]
    return '-'.join(parts)


def _pick(rng, values):
    return values[rng.randrange(len(values))]


def _weighted_pick(rng, values):
    total = sum(weight for _, weight in values)
    threshold = rng.uniform(0, total)
    running = 0
    for value, weight in values:
        running += weight
        if threshold <= running:
            return value
    return values[-1][0]


def _title_for(rng, region):
    pattern = _pick(rng, TITLE_PATTERNS)
    return pattern.format(
        object=_pick(rng, TITLE_OBJECTS),
        place=region['place'],
        adjective=_pick(rng, region['adjectives']),
    )


def _description_for(region, pressure, opening, twist, hook):
    return ' '.join((
        f'Set in {region["region"]}, {pressure}.',
        opening,
        f'Behind the first crisis, {twist}.',
        hook,
    ))


def _difficulty_for(rng):
    return _weighted_pick(rng, (
        ('Easy', 1),
        ('Medium', 4),
        ('Hard', 3),
        ('Deadly', 1),
    ))


def _required_players_for(rng):
    return _weighted_pick(rng, (
        (1, 1),
        (2, 2),
        (3, 4),
        (4, 4),
        (5, 2),
        (6, 1),
    ))


def generate_campaign_brief(seed=None, overrides=None):
    seed_value = generate_seed(seed)
    rng = random.Random(seed_value)
    overrides = overrides or {}

    region = _pick(rng, REGIONS)
    pressure = _pick(rng, PRESSURES)
    opening = _pick(rng, OPENING_SITUATIONS)
    twist = _pick(rng, TWISTS)
    hook = _pick(rng, HOOKS)
    title = _title_for(rng, region)
    difficulty = overrides.get('difficulty') or _difficulty_for(rng)
    required_players = overrides.get('required_players') or _required_players_for(rng)
    loot_mode = overrides.get('loot_mode') if overrides.get('loot_mode') in LOOT_MODES else _pick(rng, LOOT_MODES)
    description = _description_for(region, pressure, opening, twist, hook)

    generator_meta = {
        'version': 'v1',
        'region_id': region['id'],
        'region_name': region['place'],
        'landmark': region['landmark'],
        'factions': list(region['factions']),
        'mood': _pick(rng, region['moods']),
        'pressure': pressure,
        'opening_situation': opening,
        'twist': twist,
        'party_hook': hook,
    }

    settings = {
        'generator': {
            'type': 'seed_pack',
            'version': generator_meta['version'],
            'seed': seed_value,
        },
        'campaign_brief': generator_meta,
    }

    return {
        'seed': seed_value,
        'name': title,
        'description': description,
        'difficulty': difficulty,
        'required_players': max(1, min(int(required_players), 10)),
        'loot_mode': loot_mode,
        'settings': settings,
        'generator_meta': generator_meta,
    }

import json
import random
from datetime import datetime
from models import db, LootBox, Character, SheetProposal

LOOT_RARITIES = ('common', 'uncommon', 'rare', 'very_rare')

LOOT_MODE_PROMPTS = {
    'frequent_gamble': (
        'The campaign is in "Frequent Gamble" loot mode. '
        'Generate loot that feels exciting but grounded — mostly common and uncommon items '
        'with occasional rare surprises. Items should be thematic and fun, not overpowered.'
    ),
    'rare_quality': (
        'The campaign is in "Rare Quality" loot mode. '
        'This is a special loot drop — make it count. '
        'Generated items should be at least uncommon quality, with generous chances of rare or very rare items. '
        'Each item should feel meaningful and memorable.'
    ),
}


def _safe_character_name(character):
    return character.name or f'PC {character.id}'


def _build_loot_generation_context(campaign, characters):
    loot_mode = 'frequent_gamble'
    settings = {}
    if campaign.settings:
        try:
            settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else campaign.settings
        except (TypeError, ValueError):
            settings = {}
    loot_mode = settings.get('loot_mode', 'frequent_gamble')
    mode_prompt = LOOT_MODE_PROMPTS.get(loot_mode, LOOT_MODE_PROMPTS['frequent_gamble'])

    char_summaries = []
    for character in characters:
        classes = ', '.join(
            f'{c.class_name} {c.level}' for c in (character.classes or [])
        ) or f'Level {character.total_level}'
        char_summaries.append({
            'id': character.id,
            'name': _safe_character_name(character),
            'race': character.race or 'Unknown',
            'classes': classes,
            'total_level': character.total_level or 1,
        })

    return {
        'mode_prompt': mode_prompt,
        'loot_mode': loot_mode,
        'characters': char_summaries,
        'items_per_player': random.randint(10, 15),
    }


def _build_loot_generation_messages(generation_context):
    context_parts = [generation_context['mode_prompt']]
    context_parts.append(
        f'Party has {len(generation_context["characters"])} characters. '
        f'Generate exactly {generation_context["items_per_player"]} items per character.'
    )
    context_parts.append('Characters:')
    for char_info in generation_context['characters']:
        context_parts.append(
            f'  - {char_info["name"]} ({char_info["race"]}, {char_info["classes"]})'
        )

    user_content = '''
Generate a D&D loot box with per-character item pools.

For each character, generate items that:
- Fit their class, level, and identity
- Feel earned and thematic for their story
- Are fun, not just +X stat boosts
- Include at least one item that could change how they play

Return only valid JSON matching this exact shape:
{
  "pools": {
    "CHARACTER_ID": [
      {"name": "Item Name", "description": "What it does (1-2 sentences)", "rarity": "common|uncommon|rare|very_rare"}
    ]
  },
  "currency": {"gp": 0, "sp": 0, "cp": 0, "ep": 0, "pp": 0}
}

Currency should be appropriate for the party level and loot mode.
'''
    context_parts.append(user_content.strip())

    return [
        {
            'role': 'system',
            'content': (
                'You generate thematic D&D loot for individual player characters. '
                'Return only valid JSON with no commentary or markdown fences.'
            ),
        },
        {
            'role': 'user',
            'content': '\n'.join(context_parts),
        },
    ]


def _generate_loot_content(campaign, characters):
    from openrouter import get_loot_generation_response

    generation_context = _build_loot_generation_context(campaign, characters)
    error_result = {
        'pools': {str(c.id): [{'name': 'Mysterious Trinket', 'description': 'A small curiosity of unknown origin.', 'rarity': 'common'}] for c in characters},
        'currency': {'gp': 10, 'sp': 0, 'cp': 0, 'ep': 0, 'pp': 0},
    }

    if not characters:
        return error_result

    try:
        result = get_loot_generation_response(generation_context)
        if not isinstance(result, dict):
            return error_result
        pools = result.get('pools')
        if not isinstance(pools, dict):
            return error_result
        cleaned = {}
        for c in characters:
            key = str(c.id)
            pool = pools.get(key, [])
            if isinstance(pool, list) and len(pool) >= 1:
                cleaned[key] = pool
            else:
                cleaned[key] = [
                    {'name': 'Mysterious Trinket', 'description': 'A small curiosity of unknown origin.', 'rarity': 'common'}
                ]
        currency = result.get('currency', {})
        if not isinstance(currency, dict):
            currency = {'gp': 10, 'sp': 0, 'cp': 0, 'ep': 0, 'pp': 0}
        return {'pools': cleaned, 'currency': currency}
    except Exception:
        return error_result


def generate_loot_box(campaign, session, current_user, name, description):
    characters = Character.query.filter_by(campaign_id=campaign.id).order_by(Character.party_order).all()
    content = _generate_loot_content(campaign, characters)

    loot_box = LootBox(
        campaign_id=campaign.id,
        session_id=session.id if session else None,
        name=name,
        description=description,
        items_json=json.dumps(content['pools']),
        currency_json=json.dumps(content['currency']),
        status='unopened',
        created_by_session_tool=True,
    )
    db.session.add(loot_box)
    db.session.flush()

    return loot_box


def _character_pool(loot_box, character_id):
    items = json.loads(loot_box.items_json) if isinstance(loot_box.items_json, str) else (loot_box.items_json or {})
    pool = items.get(str(character_id), [])
    if isinstance(pool, list) and len(pool) > 0:
        return pool
    return []


def _currency_per_player(currency, player_count):
    if player_count <= 0:
        return {}
    result = {}
    for denom in ('cp', 'sp', 'ep', 'gp', 'pp'):
        total = max(0, int(currency.get(denom, 0)))
        result[denom] = total // player_count
    return result


def _make_item_proposal(session, character, item, dm_user_id):
    return {
        'field': f'equipment:{item["name"]}',
        'operation': 'add',
        'value': {'name': item['name']},
        'before': {'count': 0},
        'after': {'count': 1},
        'label': f'Equipment: {item["name"]}',
    }


def _make_currency_changes(currency_split):
    changes = []
    for denom in ('cp', 'sp', 'ep', 'gp', 'pp'):
        amount = currency_split.get(denom, 0)
        if amount > 0:
            changes.append({
                'field': denom,
                'operation': 'add',
                'value': amount,
                'before': 0,
                'after': amount,
                'label': denom.upper(),
            })
    return changes


def open_loot_box(loot_box, session, dm_user_id):
    draws = json.loads(loot_box.draw_results_json) if loot_box.draw_results_json else {}
    characters = Character.query.filter_by(campaign_id=loot_box.campaign_id).order_by(Character.party_order).all()
    currency = json.loads(loot_box.currency_json) if isinstance(loot_box.currency_json, str) else (loot_box.currency_json or {})
    player_count = max(len(characters), 1)
    currency_split = _currency_per_player(currency, player_count)

    proposals = []

    for character in characters:
        pool = _character_pool(loot_box, character.id)
        if not pool:
            continue

        item = random.choice(pool)
        item_changes = [_make_item_proposal(session, character, item, dm_user_id)]
        currency_changes = _make_currency_changes(currency_split)

        all_changes = item_changes + currency_changes

        reason_parts = [f'Loot from {loot_box.name}']
        if currency_changes:
            coin_parts = [f'{c["value"]} {c["label"]}' for c in currency_changes]
            reason_parts.append(f'({", ".join(coin_parts)})')

        proposal = SheetProposal(
            session_id=session.id,
            character_id=character.id,
            dm_user_id=dm_user_id,
            reason=' | '.join(reason_parts),
            changes=all_changes,
            status='pending',
        )
        db.session.add(proposal)
        db.session.flush()

        draws[str(character.id)] = {
            'item_index': pool.index(item),
            'item_name': item['name'],
            'item_description': item.get('description', ''),
            'item_rarity': item.get('rarity', 'common'),
            'proposal_id': proposal.id,
        }
        proposals.append(proposal)

    loot_box.draw_results_json = json.dumps(draws)
    loot_box.status = 'opened'
    loot_box.opened_at = datetime.utcnow()
    db.session.commit()

    return proposals


def get_campaign_stash(campaign_id, current_user=None, is_dm=False):
    boxes = LootBox.query.filter_by(campaign_id=campaign_id).order_by(LootBox.created_at.desc()).all()
    return [box.to_dict(current_user=current_user, is_dm=is_dm) for box in boxes]

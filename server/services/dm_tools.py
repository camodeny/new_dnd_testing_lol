import json
import random
import re
from time_utils import utcnow
from models import (
    db,
    CampaignClock,
    CampaignMember,
    CampaignMonster,
    CampaignSession,
    Character,
    CharacterCondition,
    EncounterMap,
    EncounterMapPlacement,
    NPCActor,
    SessionMessage,
    SheetProposal,
    WorldEvent,
)
from services.audit_service import log_audit_event
from services.character_service import character_full_dict
from services.embedding_service import (
    find_duplicate_graph_item,
    search_memory_embeddings,
    search_memory_embeddings_batch,
    search_weight,
    upsert_memory_embedding,
)
from services.encounter_movement import reachable_cells
from services.encounter_map_service import create_encounter_map as do_create_encounter_map, latest_encounter_map
from services.lootbox_service import generate_loot_box as do_generate_loot_box
from services.planning_service import summary_dict_for_read
from services.shop_generation_service import clean_shop_items, generate_scene_shops, upsert_shop
from services.world_service import clean_id, clean_text, get_campaign_world, json_dumps, json_loads
from services.memory_resolver_schemas import (
    SOURCE_CONTRACT_COMPILED_V2,
    determine_evidence_status,
    make_evidence_source,
    validate_diagnostics,
)
from openrouter import get_character_sheet_answer


VALID_VISIBILITIES = {'public', 'party_known', 'dm_private'}
ACTIVE_CLOCK_STATUSES = {'active', 'ticking', 'pending', 'completion_pending'}
VALID_MEMORY_CERTAINTIES = {'confirmed', 'suspected', 'inferred', 'false', 'retconned'}
DEFERRED_NARRATIVE_TOOL_NAMES = {
    'record_world_event',
    'update_current_scene',
    'reveal_fact',
    'propose_sheet_update',
}
RETRIEVAL_LANE_ORDER = ('entities', 'scene_events', 'clocks_promises', 'prior_facts')
PRIVATE_VISIBILITY_TERMS = {
    'dm_private',
    'private',
    'secret',
    'hidden',
    'unrevealed',
    'spoiler',
    'offscreen',
    'off-screen',
}
SYMMETRIC_RELATION_TYPES = {
    'allied_with',
    'associated_with',
    'connected_to',
    'companion_of',
    'friends_with',
    'sibling_of',
    'traveling_with',
}


def estimate_tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 4) if text else 0


def _iter_patch_scalars(value):
    if isinstance(value, dict):
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_patch_scalars(item)
        return
    if value not in (None, ''):
        yield value


def _first_patch_scalar(value):
    for item in _iter_patch_scalars(value):
        return item
    return None


def _coerce_patch_text(value, max_length=1200, default=''):
    cleaned = clean_text(_first_patch_scalar(value), max_length)
    return cleaned or default


def _coerce_patch_id_list(value, max_items=8):
    values = value if isinstance(value, (list, tuple, set)) else [value]
    cleaned = []
    seen = set()
    for raw in _iter_patch_scalars(values):
        item_id = clean_id(raw, '')
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        cleaned.append(item_id)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _coerce_patch_int(value, default=None, minimum=None, maximum=None):
    scalar = _first_patch_scalar(value)
    if scalar in (None, ''):
        return default
    try:
        number = int(scalar)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _coerce_patch_visibility(value, default=None):
    visibility = _coerce_patch_text(value, 30)
    return visibility if visibility in VALID_VISIBILITIES else default


def _sync_party_known_location(world_state, current_scene):
    if not isinstance(world_state, dict):
        return
    current_scene = current_scene if isinstance(current_scene, dict) else {}
    location_id = clean_id(current_scene.get('location_id'), '')
    if not location_id:
        return
    party = world_state.get('party') if isinstance(world_state.get('party'), dict) else {}
    party['known_location_id'] = location_id
    world_state['party'] = party


def _normalize_clock_completion_criteria(value):
    """Return a bounded, stable list of criteria required before a full clock can resolve."""
    if not isinstance(value, list):
        return []
    criteria = []
    seen_ids = set()
    for index, raw_item in enumerate(value[:8], start=1):
        if isinstance(raw_item, dict):
            criterion_id = clean_id(raw_item.get('id'), f'criterion_{index}')
            description = _coerce_patch_text(raw_item.get('description') or raw_item.get('text'), 420)
        else:
            criterion_id = f'criterion_{index}'
            description = _coerce_patch_text(raw_item, 420)
        if not criterion_id or not description or criterion_id in seen_ids:
            continue
        seen_ids.add(criterion_id)
        criteria.append({'id': criterion_id, 'description': description})
    return criteria


def _visibility_text_for_item(item, fields):
    if not isinstance(item, dict):
        return ''
    values = []
    for field in fields:
        value = item.get(field)
        if isinstance(value, list):
            values.extend(str(part) for part in value if part not in (None, ''))
        elif value not in (None, ''):
            values.append(str(value))
    return ' '.join(values)


def _visibility_words(text):
    return {
        word
        for word in re.findall(r"[a-z0-9']+", clean_text(text, 4000).lower())
        if len(word) >= 4
    }


def _is_supported_by_visible_exchange(item_text, visible_text):
    item_words = _visibility_words(item_text)
    if not item_words:
        return False
    visible_words = _visibility_words(visible_text)
    if not visible_words:
        return False
    overlap = item_words & visible_words
    if len(overlap) >= 4 and len(overlap) / max(len(item_words), 1) >= 0.45:
        return True
    clean_item = clean_text(item_text, 700).lower()
    clean_visible = clean_text(visible_text, 4000).lower()
    return len(clean_item) >= 24 and clean_item in clean_visible


def _contains_hidden_policy_signal(item_text):
    item_words = _visibility_words(item_text)
    return bool(item_words & PRIVATE_VISIBILITY_TERMS)


def _contains_unrevealed_private_term(campaign, item_text, visible_text):
    if not campaign:
        return False
    lowered_item = clean_text(item_text, 4000).lower()
    lowered_visible = clean_text(visible_text, 4000).lower()
    if not lowered_item:
        return False
    for term in _private_output_terms(campaign):
        lowered_term = clean_text(term, 240).lower()
        if len(lowered_term) < 4:
            continue
        if lowered_term in lowered_item and lowered_term not in lowered_visible:
            return True
    return False


def _visibility_policy_text(audit_context):
    return ' '.join(
        clean_text(audit_context.get(key), 4000)
        for key in ('latest_player_message', 'latest_dm_message', 'player_message', 'dm_message')
        if audit_context.get(key)
    )


def _leak_guard_has_private_content(campaign, value, visible_text):
    """True when party-visible text carries unrevealed private terms or a
    hidden-policy signal that is not already supported by the visible exchange."""
    if not value:
        return False
    text = clean_text(value, 4000)
    if not text:
        return False
    if _contains_unrevealed_private_term(campaign, text, visible_text):
        return True
    if visible_text and _contains_hidden_policy_signal(text) and not _is_supported_by_visible_exchange(text, visible_text):
        return True
    return False


def _leak_guard_memory_patch(campaign, patch, visible_text):
    """Final write-boundary leak prevention for memory writes.

    Party-visible (public/party_known) items must never carry unrevealed
    private terms or unsupported hidden-policy signals. An identity field that
    leaks demotes the whole item to dm_private; secondary text fields are
    redacted in place. NPC public columns are redacted independently so a
    visible name cannot promote a private dossier summary. Private dossier
    fields (voice, background, wants, fears, secrets, relationships,
    recent_offscreen_activity) always stay inside the DM-private dossier.

    Returns a telemetry dict with reason codes and counts only -- never the
    secret content itself.
    """
    telemetry = {
        'entities_demoted': 0,
        'entities_redacted': 0,
        'relations_demoted': 0,
        'relations_redacted': 0,
        'facts_demoted': 0,
        'events_demoted': 0,
        'events_redacted': 0,
        'npcs_demoted': 0,
        'npc_public_redacted': 0,
        'clocks_demoted': 0,
        'summary_redacted': 0,
        'anchor_items_redacted': 0,
        'reasons': [],
    }

    def _flag(reason):
        telemetry['reasons'].append(reason)

    for item in patch.get('upsert_graph_entities', []) if isinstance(patch.get('upsert_graph_entities'), list) else []:
        if not isinstance(item, dict) or item.get('visibility') not in {'public', 'party_known'}:
            continue
        if _leak_guard_has_private_content(campaign, item.get('name'), visible_text):
            item['visibility'] = 'dm_private'
            telemetry['entities_demoted'] += 1
            _flag('entity_name_private')
            continue
        if _leak_guard_has_private_content(
            campaign,
            _visibility_text_for_item(item, ('summary', 'tags')),
            visible_text,
        ):
            for field in ('summary', 'tags'):
                if field in item:
                    item.pop(field, None)
            telemetry['entities_redacted'] += 1
            _flag('entity_summary_private')

    for item in patch.get('upsert_graph_relations', []) if isinstance(patch.get('upsert_graph_relations'), list) else []:
        if not isinstance(item, dict) or item.get('visibility') not in {'public', 'party_known'}:
            continue
        if _leak_guard_has_private_content(
            campaign,
            _visibility_text_for_item(item, ('summary', 'type')),
            visible_text,
        ):
            item['visibility'] = 'dm_private'
            telemetry['relations_demoted'] += 1
            _flag('relation_private')

    for item in patch.get('upsert_graph_facts', []) if isinstance(patch.get('upsert_graph_facts'), list) else []:
        if not isinstance(item, dict) or item.get('visibility') not in {'public', 'party_known'}:
            continue
        if _leak_guard_has_private_content(campaign, item.get('text'), visible_text):
            item['visibility'] = 'dm_private'
            telemetry['facts_demoted'] += 1
            _flag('fact_private')

    for item in patch.get('update_npc_actors', []) if isinstance(patch.get('update_npc_actors'), list) else []:
        if not isinstance(item, dict):
            continue
        identity_private = _leak_guard_has_private_content(campaign, item.get('name'), visible_text)
        if identity_private:
            item.pop('name', None)
            telemetry['npc_public_redacted'] += 1
            _flag('npc_name_private')
        if _leak_guard_has_private_content(
            campaign,
            _visibility_text_for_item(item, ('role', 'public_summary')),
            visible_text,
        ):
            for field in ('role', 'public_summary'):
                if field in item:
                    item.pop(field, None)
            telemetry['npc_public_redacted'] += 1
            _flag('npc_public_fields_private')
        if identity_private:
            item['visibility'] = 'dm_private'
            telemetry['npcs_demoted'] += 1
            _flag('npc_demoted_dm_private')
        else:
            item['visibility'] = _coerce_patch_visibility(item.get('visibility')) or 'dm_private'

    for item in patch.get('record_events', []) if isinstance(patch.get('record_events'), list) else []:
        if not isinstance(item, dict) or item.get('visibility') not in {'public', 'party_known'}:
            continue
        if _leak_guard_has_private_content(
            campaign,
            _visibility_text_for_item(item, ('summary', 'event_type')),
            visible_text,
        ):
            item['visibility'] = 'dm_private'
            telemetry['events_demoted'] += 1
            _flag('event_private')

    for key in ('create_clocks', 'retire_clocks'):
        for item in patch.get(key, []) if isinstance(patch.get(key), list) else []:
            if not isinstance(item, dict) or item.get('visibility') not in {'public', 'party_known'}:
                continue
            if _leak_guard_has_private_content(
                campaign,
                _visibility_text_for_item(item, ('name', 'summary', 'trigger', 'on_complete')),
                visible_text,
            ):
                item['visibility'] = 'dm_private'
                telemetry['clocks_demoted'] += 1
                _flag('clock_private')

    # ── Session-facing free text: running summary and memory anchors ───
    # CampaignSession.to_dict() serves running_summary and normalized
    # memory_anchors to every campaign member, so unrevealed private terms
    # must be redacted here at the write boundary too.
    summary, summary_changed = _redact_free_text_private_terms(
        campaign,
        patch.get('running_summary'),
        visible_text,
    )
    if summary_changed:
        patch['running_summary'] = summary
        telemetry['summary_redacted'] += 1
        _flag('running_summary_private')

    anchors = patch.get('memory_anchors')
    if isinstance(anchors, dict):
        for key in ('current_goal', 'current_scene'):
            value, changed = _redact_free_text_private_terms(campaign, anchors.get(key), visible_text)
            if changed:
                anchors[key] = value
                telemetry['anchor_items_redacted'] += 1
                _flag('memory_anchor_private')
        for key in ('open_clues', 'unresolved_questions', 'npc_observations', 'recent_offers_promises'):
            values = anchors.get(key)
            if not isinstance(values, list):
                continue
            kept = []
            for value in values:
                if _leak_guard_has_private_content(campaign, value, visible_text):
                    telemetry['anchor_items_redacted'] += 1
                    _flag('memory_anchor_private')
                    continue
                kept.append(value)
            anchors[key] = kept

    return telemetry


def _redact_free_text_private_terms(campaign, text, visible_text):
    """Drop sentences that carry unrevealed private terms from free text.

    Used for session-facing free text (running summary, anchor scalars) where
    a whole-record demotion is not meaningful. Returns (redacted_or_none,
    changed) and never emits the private term itself.
    """
    text = clean_text(text, 4000)
    if not text:
        return None, False
    if not _leak_guard_has_private_content(campaign, text, visible_text):
        return text, False
    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = [
        sentence
        for sentence in sentences
        if not _leak_guard_has_private_content(campaign, sentence, visible_text)
    ]
    cleaned = ' '.join(kept).strip()
    return (cleaned or None), True


def redact_session_summary_private_terms(campaign, summary, player_message, dm_message):
    """Apply the leak guard to a running summary produced by the post-clock
    summary finalizer, which writes session.running_summary directly (bypassing
    the memory patch write boundary). Returns (redacted_or_none, changed)."""
    visible_text = ' '.join(
        clean_text(value, 2400)
        for value in (player_message, dm_message)
        if value
    ).strip()
    return _redact_free_text_private_terms(campaign, summary, visible_text)


def _selected_member(campaign_id, user_id):
    return CampaignMember.query.filter_by(campaign_id=campaign_id, user_id=user_id).first()


def _compact_character(character):
    if not character:
        return None
    data = character.to_dict()
    return {
        'id': data.get('id'),
        'user_id': data.get('user_id'),
        'name': data.get('name'),
        'race': data.get('race'),
        'subrace': data.get('subrace'),
        'background': data.get('background'),
        'total_level': data.get('total_level'),
        'ability_scores': data.get('ability_scores'),
        'combat': data.get('combat'),
        'general': data.get('general'),
        'spellcasting': data.get('spellcasting'),
        'current_location': getattr(character, 'current_location', None),
    }


def _current_character(campaign, current_user):
    member = _selected_member(campaign.id, current_user.id)
    if member and member.selected_character_id:
        return db.session.get(Character, member.selected_character_id)
    return Character.query.filter_by(campaign_id=campaign.id, user_id=current_user.id).first()


def _protected_player_characters(members):
    protected = []
    for member in members:
        character = db.session.get(Character, member.selected_character_id) if member.selected_character_id else None
        if character:
            protected.append({
                'id': character.id,
                'name': character.name,
                'user_id': member.user_id,
                'username': member.user.username if member.user else None,
            })
    return protected


def _world_json(campaign):
    world = get_campaign_world(campaign.id)
    if not world:
        return None, {}, {}, {}
    return (
        world,
        json_loads(world.knowledge_graph, {'entities': [], 'relations': [], 'facts': []}),
        json_loads(world.world_state, {}),
        json_loads(world.dm_private, {}),
    )


def _active_clocks(campaign, limit=8):
    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
    active = [clock for clock in clocks if (clock.status or 'active') in ACTIVE_CLOCK_STATUSES]
    return (active or clocks)[:limit]


def _established_public_facts(campaign, limit=8):
    _world, graph, _world_state, _dm_private = _world_json(campaign)
    facts = []
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if not isinstance(fact, dict):
            continue
        visibility = clean_text(fact.get('visibility'), 30) or 'dm_private'
        if visibility not in {'public', 'party_known'}:
            continue
        text = clean_text(fact.get('text'), 220)
        if not text:
            continue
        facts.append({
            'id': clean_id(fact.get('id'), f'fact_{len(facts) + 1}'),
            'text': text,
            'certainty': clean_text(fact.get('certainty'), 40) or 'confirmed',
            'visibility': visibility,
            'entity_ids': _coerce_patch_id_list(fact.get('entity_ids'), max_items=4),
        })
        if len(facts) >= limit:
            break
    return facts


def _recent_public_world_events(campaign, limit=6):
    events = (
        WorldEvent.query.filter_by(campaign_id=campaign.id)
        .order_by(WorldEvent.id.desc())
        .limit(max(limit * 3, limit))
        .all()
    )
    public_events = []
    for event in events:
        visibility = clean_text(event.visibility, 30) or 'dm_private'
        if visibility not in {'public', 'party_known'}:
            continue
        summary = clean_text(event.summary, 220)
        if not summary:
            continue
        public_events.append({
            'id': event.id,
            'event_type': clean_text(event.event_type, 80),
            'summary': summary,
            'visibility': visibility,
        })
        if len(public_events) >= limit:
            break
    return list(reversed(public_events))


def _open_public_threads(campaign, limit=6):
    _world, _graph, world_state, _dm_private = _world_json(campaign)
    open_threads = world_state.get('open_threads', []) if isinstance(world_state, dict) else []
    if not isinstance(open_threads, list):
        open_threads = [open_threads]
    return [
        clean_text(thread, 180)
        for thread in open_threads[:limit]
        if clean_text(thread, 180)
    ]


def _private_output_terms(campaign):
    _world, graph, _world_state, dm_private = _world_json(campaign)
    terms = set()

    def add(value):
        text = clean_text(value, 240)
        if len(text) >= 4:
            terms.add(text)

    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if entity.get('visibility') == 'dm_private':
            add(entity.get('name'))
    for relation in graph.get('relations', []) if isinstance(graph, dict) else []:
        if relation.get('visibility') == 'dm_private':
            add(relation.get('summary'))
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if fact.get('visibility') == 'dm_private':
            add(fact.get('text'))

    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        if (clock.visibility or 'dm_private') == 'dm_private':
            add(clock.name)

    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        dossier = json_loads(npc.dossier, {})
        for secret in dossier.get('secrets', []) if isinstance(dossier, dict) else []:
            add(secret)

    if isinstance(dm_private, dict):
        for key in ('true_inciting_incident', 'villain_plan'):
            add(dm_private.get(key))
        for value in dm_private.get('hidden_factions', []) if isinstance(dm_private.get('hidden_factions'), list) else []:
            add(value)
        for value in dm_private.get('npc_secrets', []) if isinstance(dm_private.get('npc_secrets'), list) else []:
            add(value)

    return sorted(terms, key=lambda value: value.lower())


def _latest_player_content_from_messages(messages):
    for message in reversed(messages or []):
        role = getattr(message, 'role', None)
        if role == 'player':
            return getattr(message, 'content', '') or ''
    return ''


def _public_npc_reference(npc):
    summary = clean_text(npc.public_summary, 160)
    if summary:
        match = re.match(r'^(?:an?|the)\s+([^.,;]+)', summary, flags=re.IGNORECASE)
        if match:
            reference = match.group(1).strip()
        else:
            reference = summary.split('.')[0].split(',')[0].strip()
        reference = re.sub(r'\bwho\b.*$', '', reference, flags=re.IGNORECASE).strip()
        if reference:
            return reference[:80].strip()

    actor_id = clean_text(npc.actor_id, 100)
    if actor_id:
        return actor_id.replace('_', ' ').strip()
    return 'unrevealed NPC'


def _visible_naming_constraints(campaign, recent_messages):
    private_terms = {term.lower() for term in _private_output_terms(campaign)}
    latest_player_content = _latest_player_content_from_messages(recent_messages)
    constraints = []
    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all():
        name = clean_text(npc.name, 120)
        if not name or name.lower() not in private_terms:
            continue
        if re.search(re.escape(name), latest_player_content, flags=re.IGNORECASE):
            continue
        constraints.append({
            'avoid_visible_name': name,
            'use_public_reference': _public_npc_reference(npc),
            'applies_to': 'visible narration and <npc target="..."> until the name is revealed by play',
        })
    return constraints


def _private_spoiler_items(campaign):
    _world, graph, _world_state, dm_private = _world_json(campaign)
    items = []
    seen = set()

    def add(item_id, kind, text):
        clean = clean_text(text, 700)
        if not clean:
            return
        dedupe_key = clean.lower()
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        items.append({
            'id': clean_id(item_id, f'private_item_{len(items) + 1}'),
            'kind': kind,
            'text': clean,
        })

    for entity in graph.get('entities', []) if isinstance(graph, dict) else []:
        if entity.get('visibility') == 'dm_private':
            add(entity.get('id'), 'entity', ' - '.join(
                part for part in [entity.get('name'), entity.get('summary')] if part
            ))
    for relation in graph.get('relations', []) if isinstance(graph, dict) else []:
        if relation.get('visibility') == 'dm_private':
            add(relation.get('id'), 'relation', relation.get('summary'))
    for fact in graph.get('facts', []) if isinstance(graph, dict) else []:
        if fact.get('visibility') == 'dm_private':
            add(fact.get('id'), 'fact', fact.get('text'))

    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        if (clock.visibility or 'dm_private') == 'dm_private':
            add(
                f'clock_{clock.clock_id}',
                'clock',
                ' - '.join(part for part in [clock.name, clock.summary, clock.trigger, clock.on_complete] if part),
            )

    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        dossier = json_loads(npc.dossier, {})
        for index, secret in enumerate(dossier.get('secrets', []) if isinstance(dossier, dict) else []):
            add(f'npc_secret_{npc.actor_id}_{index + 1}', 'npc_secret', secret)

    if isinstance(dm_private, dict):
        for key in ('true_inciting_incident', 'villain_plan'):
            add(f'dm_private_{key}', key, dm_private.get(key))
        for index, value in enumerate(dm_private.get('hidden_factions', []) if isinstance(dm_private.get('hidden_factions'), list) else []):
            add(f'dm_private_hidden_faction_{index + 1}', 'hidden_faction', value)
        for index, value in enumerate(dm_private.get('npc_secrets', []) if isinstance(dm_private.get('npc_secrets'), list) else []):
            add(f'dm_private_npc_secret_{index + 1}', 'npc_secret', value)

    # Include recent DM-private world events so operational clues from hidden state
    # are available to the spoiler checker as unrevealed private items.
    private_events = (
        WorldEvent.query
        .filter_by(campaign_id=campaign.id, visibility='dm_private')
        .order_by(WorldEvent.created_at.desc())
        .limit(24)
        .all()
    )
    for event in reversed(private_events):
        payload = json_loads(event.payload, {})
        payload_scene = payload.get('scene_patch', {}) if isinstance(payload, dict) else {}
        payload_current_scene = payload.get('current_scene', {}) if isinstance(payload, dict) else {}
        scene_tension = payload_scene.get('immediate_tension') if isinstance(payload_scene, dict) else None
        current_tension = payload_current_scene.get('immediate_tension') if isinstance(payload_current_scene, dict) else None
        text = ' - '.join(
            part for part in [
                event.summary,
                scene_tension,
                current_tension,
            ] if part
        )
        add(f'world_event_{event.id}', 'world_event', text)

    return items


def _campaign_loot_mode_policy(campaign):
    import json
    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    loot_mode = settings.get('loot_mode', 'frequent_gamble')

    if loot_mode == 'rare_quality':
        return (
            'Loot policy: The party is in "Rare Quality" mode. '
            'Use the generate_loot_box tool sparingly — only after major boss fights, '
            'significant story milestones, or truly exceptional achievements. '
            'When you do generate loot, the items should feel meaningful and memorable. '
            'Make each drop count.'
        )
    return (
        'Loot policy: The party is in "Frequent Gamble" mode. '
        'Use the generate_loot_box tool often — after combat encounters, exploration '
        'discoveries, social victories, or any notable achievement. '
        'The fun is in the frequency — keep the rewards coming regularly.'
    )


def _campaign_loot_mode(campaign):
    import json
    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    return settings.get('loot_mode', 'frequent_gamble')


def _compact_encounter_map(encounter_map):
    if not encounter_map:
        return None
    data = encounter_map.to_dict(include_private=True)
    return {
        'id': data.get('id'),
        'title': data.get('title'),
        'grid': data.get('grid'),
        'vtt_setup': data.get('vtt_setup'),
        'placements': data.get('placements', []),
        'encounter_state': data.get('encounter_state'),
        'setup_status': data.get('setup_status'),
        'setup_error': data.get('setup_error'),
    }


def _campaign_settings(campaign):
    try:
        return json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        return {}


def _combat_coordinate_context(campaign, encounter_map):
    if not encounter_map:
        return None

    settings = _campaign_settings(campaign)
    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    state_active = state.get('active', False) if isinstance(state, dict) else False
    if not settings.get('encounter_active') and not state_active:
        return None

    grid = EncounterMap._json_value(encounter_map.grid_json, {})
    turn_order = state.get('turn_order', []) if isinstance(state, dict) else []
    turn_by_placement_id = {
        item.get('placement_id'): item
        for item in turn_order
        if isinstance(item, dict) and item.get('placement_id') is not None
    }
    active_turn_index = state.get('active_turn_index') if isinstance(state, dict) else None
    active_placement_id = None
    if isinstance(active_turn_index, int) and 0 <= active_turn_index < len(turn_order):
        active_item = turn_order[active_turn_index]
        if isinstance(active_item, dict):
            active_placement_id = active_item.get('placement_id')

    combatants = []
    for placement in sorted(encounter_map.placements, key=lambda item: (item.grid_row, item.grid_col, item.id)):
        turn_item = turn_by_placement_id.get(placement.id, {})
        combatants.append({
            'placement_id': placement.id,
            'actor_type': placement.actor_type,
            'combatant_type': 'enemy' if placement.actor_type == 'monster' else placement.actor_type,
            'actor_id': placement.actor_id,
            'label': placement.label,
            'coordinates': {
                'col': placement.grid_col,
                'row': placement.grid_row,
            },
            'initiative': turn_item.get('initiative') if isinstance(turn_item, dict) else None,
            'is_active_turn': placement.id == active_placement_id,
        })

    return {
        'active': True,
        'encounter_map_id': encounter_map.id,
        'encounter_map_title': encounter_map.title,
        'grid': {
            'columns': grid.get('columns') if isinstance(grid, dict) else None,
            'rows': grid.get('rows') if isinstance(grid, dict) else None,
        },
        'round': state.get('round') if isinstance(state, dict) else None,
        'active_turn_index': active_turn_index,
        'combatants': combatants,
        'instruction': (
            'Use these exact grid coordinates as the current combat positions for every player character, '
            'NPC, and enemy before adjudicating the latest message.'
        ),
    }


def build_session_hot_context(campaign, session, current_user, recent_messages_override=None):
    character = _current_character(campaign, current_user)
    world, _graph, world_state, _private = _world_json(campaign)
    recent_messages = recent_messages_override if recent_messages_override is not None else (
        session.messages[-8:] if session and session.messages else []
    )
    active_clocks = _active_clocks(campaign)
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
    protected_player_characters = _protected_player_characters(members)
    loot_mode = _campaign_loot_mode(campaign)
    encounter_map = latest_encounter_map(campaign.id)

    context = {
        'strategy': 'compact_hot_context_with_dm_tools',
        'full_world_graph_included': False,
        'campaign': {
            'id': campaign.id,
            'name': campaign.name,
            'description': campaign.description,
            'difficulty': campaign.difficulty,
            'seed': campaign.seed,
            'loot_mode': loot_mode,
        },
        'session': {
            'id': session.id,
            'running_summary': session.running_summary or '',
            'message_count': len(session.messages or []),
        },
        'current_user': current_user.to_dict(),
        'current_character': _compact_character(character),
        'current_player_character': (
            {
                'id': character.id,
                'name': character.name,
                'user_id': character.user_id,
            }
            if character else None
        ),
        'protected_player_characters': protected_player_characters,
        'party': [
            {
                'user_id': member.user_id,
                'username': member.user.username if member.user else None,
                'selected_character_id': member.selected_character_id,
                'selected_character': next((
                    pc for pc in protected_player_characters
                    if pc['id'] == member.selected_character_id
                ), None),
                'ready': bool(member.character_ready_at and member.selected_character_id),
            }
            for member in members
        ],
        'current_scene': current_scene,
        'current_encounter_map': _compact_encounter_map(encounter_map),
        'combat_coordinates': _combat_coordinate_context(campaign, encounter_map),
        'active_clocks': [clock.to_dict(include_private=True) for clock in active_clocks],
        'established_public_facts': _established_public_facts(campaign),
        'recent_public_world_events': _recent_public_world_events(campaign),
        'open_public_threads': _open_public_threads(campaign),
        'visible_naming_constraints': _visible_naming_constraints(campaign, recent_messages),
        'private_output_terms': _private_output_terms(campaign),
        'private_spoiler_items': _private_spoiler_items(campaign),
        'recent_messages': [message.to_dict() for message in recent_messages],
        'tool_policy': (
            'Treat hot-context memory as authoritative state for lore, continuity, NPC motivations, active pressure, '
            'and world consequences. Use tools for character-sheet answers, campaign memory, NPC dossiers, clocks, '
            'scene state, and durable state writes instead of guessing. If a question or action touches remembered '
            'facts, unresolved clocks, recent world events, or stored NPC agendas, inspect and follow that state '
            'before improvising. Do not claim to update world state unless a write tool succeeds. Never reveal '
            'DM-private tool results unless they have become visible through play. private_output_terms are '
            'reasoning-only strings that must not appear in visible narration unless they are first revealed through play. '
            'Treat specific player-supplied recollections, accusations, guesses, bluff details, and theories as claims, '
            'not confirmed truth, unless they are corroborated by the visible scene, established public facts, a '
            'successful check, or another grounded source. NPCs may react to a claim without validating it. '
            'When a new interpretation would reframe an existing public lead or clue, preserve the old lead unless the '
            'fiction clearly earns the update; if uncertain, speak conditionally instead of replacing prior truth. '
            'Use create_encounter_map when the party enters a tactical area where spatial positioning matters, '
            'or when a player explicitly asks for a map; include vtt_setup_notes when the DM has intended '
            'friendly starts, enemy starts, obstacles, objectives, or terrain calls for the playable setup JSON. '
            'After a map exists and combat positioning matters, use place_encounter_map_actors to place '
            'players, NPCs, and monsters on grid coordinates, and use move_encounter_actor to move NPCs and monsters tactically '
            'using real pathfinding and movement costs instead of resetting placements. '
            'When combat or initiative starts, use toggle_encounter_mode(active=True) to start Encounter Mode. '
            'This will return an instruction prompt. You must then immediately call create_encounter_map to generate '
            'a brand new combat map, and then place_encounter_map_actors to place everyone onto it. Do not re-use old maps. '
            'During combat, track turns and state with tools instead of narration guesses: use get_encounter_overview() or get_combatant_state() for compact reads, '
            'list_reachable_positions() before tactical movement, move_encounter_actor() for terrain-aware movement, next_combat_turn() to advance turns, '
            'set_combat_turn() to skip or correct turns, update_combatant_actions() to deduct actions/movement, roll_dice() for deterministic DM-side rolls, '
            'apply_damage(), apply_healing(), grant_temp_hp(), set_combatant_hp(), set_combatant_initiative(), update_combatant_conditions(), and remove_encounter_actor() '
            'to mutate combat state directly. '
            'When combat_coordinates is present, treat it as the latest exact grid location list for every PC, NPC, and enemy after the latest message. '
            'Do not generate maps for every ordinary scene. '
            + _campaign_loot_mode_policy(campaign)
        ),
    }
    return context


SESSION_DM_PRIMARY_PROMPT_EXCLUDED_CONTEXT_KEYS = {
    'private_output_terms',
    'private_spoiler_items',
    'recent_messages',
}


def _transcript_source_refs(messages):
    refs = []
    for message in messages or []:
        if isinstance(message, dict):
            message_id = message.get('id')
            session_id = message.get('session_id')
            role = message.get('role')
        else:
            message_id = getattr(message, 'id', None)
            session_id = getattr(message, 'session_id', None)
            role = getattr(message, 'role', None)
        if message_id is None:
            continue
        refs.append({
            'source_id': f'session_message:{message_id}',
            'message_id': message_id,
            'session_id': session_id,
            'role': role,
        })
    return refs


def context_manifest(context, tools):
    section_tokens = {
        key: estimate_tokens(value)
        for key, value in context.items()
        if key not in {'recent_messages'}
    }
    section_tokens['recent_messages'] = estimate_tokens(context.get('recent_messages', []))
    primary_prompt_section_tokens = {
        key: token_count
        for key, token_count in section_tokens.items()
        if key not in SESSION_DM_PRIMARY_PROMPT_EXCLUDED_CONTEXT_KEYS
    }
    transcript_source_refs = _transcript_source_refs(context.get('recent_messages', []))
    return {
        'strategy': context.get('strategy'),
        'full_world_graph_included': False,
        'fed_sections': [
            key for key in context.keys()
            if key not in SESSION_DM_PRIMARY_PROMPT_EXCLUDED_CONTEXT_KEYS
        ],
        'internal_only_sections': [
            key for key in context.keys()
            if key in SESSION_DM_PRIMARY_PROMPT_EXCLUDED_CONTEXT_KEYS
        ],
        'available_tools': [tool['function']['name'] for tool in tools],
        'estimated_tokens_by_section': section_tokens,
        'estimated_total_tokens': sum(section_tokens.values()),
        'estimated_primary_prompt_tokens_by_section': primary_prompt_section_tokens,
        'estimated_primary_prompt_context_tokens': sum(primary_prompt_section_tokens.values()),
        'estimated_duplicate_transcript_tokens_removed': section_tokens['recent_messages'],
        'recent_message_source_ids': [ref['message_id'] for ref in transcript_source_refs],
        'recent_message_source_refs': transcript_source_refs,
    }


def _memory_text(value, limit=1600):
    text = str(value or '')
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '...'


def _memory_hot_context_summary(hot_context):
    hot_context = hot_context or {}

    def compact_character_summary(character):
        if not isinstance(character, dict):
            return None
        classes = []
        for entry in character.get('classes') or []:
            if not isinstance(entry, dict):
                continue
            classes.append({
                'class_name': entry.get('class_name'),
                'level': entry.get('level'),
                'subclass': entry.get('subclass'),
            })
        return {
            'id': character.get('id'),
            'user_id': character.get('user_id'),
            'name': character.get('name'),
            'race': character.get('race'),
            'subrace': character.get('subrace'),
            'background': character.get('background'),
            'total_level': character.get('total_level'),
            'classes': classes,
            'current_location': character.get('current_location'),
        }

    def compact_clock(clock):
        return {
            'clock_id': clock.get('clock_id') or clock.get('id'),
            'name': clock.get('name'),
            'filled': clock.get('filled'),
            'segments': clock.get('segments'),
            'status': clock.get('status'),
            'visibility': clock.get('visibility'),
            'summary': _memory_text(clock.get('summary'), 220),
        }

    def compact_message(message):
        return {
            'role': message.get('role'),
            'user_id': message.get('user_id'),
            'content': _memory_text(message.get('content'), 450),
        }

    return {
        'campaign': hot_context.get('campaign'),
        'session': hot_context.get('session'),
        'current_user': {
            'id': (hot_context.get('current_user') or {}).get('id'),
            'username': (hot_context.get('current_user') or {}).get('username'),
        },
        'current_character': compact_character_summary(hot_context.get('current_character')),
        'current_player_character': compact_character_summary(hot_context.get('current_player_character')),
        'protected_player_characters': [
            compact_character_summary(character)
            for character in (hot_context.get('protected_player_characters') or [])
            if isinstance(character, dict)
        ],
        'current_scene': hot_context.get('current_scene') or {},
        'current_encounter_map': hot_context.get('current_encounter_map'),
        'active_clocks': [
            compact_clock(clock)
            for clock in (hot_context.get('active_clocks') or [])[:8]
            if isinstance(clock, dict)
        ],
        'visible_naming_constraints': hot_context.get('visible_naming_constraints') or [],
        'recent_messages': [
            compact_message(message)
            for message in (hot_context.get('recent_messages') or [])[-4:]
            if isinstance(message, dict)
        ],
    }


def _compact_memory_search_value(kind, value):
    if kind == 'world_state' and isinstance(value, dict):
        current_scene = value.get('current_scene') if isinstance(value.get('current_scene'), dict) else {}
        return {
            'current_arc': value.get('current_arc'),
            'current_scene': {
                'location_id': current_scene.get('location_id'),
                'location_name': current_scene.get('location_name'),
                'time_of_day': current_scene.get('time_of_day'),
                'active_npc_ids': current_scene.get('active_npc_ids'),
                'immediate_tension': _memory_text(current_scene.get('immediate_tension'), 220),
            },
        }
    if kind == 'dm_private' and isinstance(value, dict):
        return {
            'true_inciting_incident': _memory_text(value.get('true_inciting_incident'), 220),
            'villain_plan': _memory_text(value.get('villain_plan'), 220),
            'hidden_pressures': [
                _memory_text(item, 120)
                for item in (value.get('hidden_pressures') or [])[:3]
            ],
        }
    if kind == 'planning_summary' and isinstance(value, dict):
        return {
            'summary': _memory_text(value.get('summary') or value.get('planning_summary'), 220),
            'party_goals': [
                _memory_text(item, 120)
                for item in (value.get('party_goals') or [])[:3]
            ],
        }
    if kind == 'clock' and isinstance(value, dict):
        return {
            'name': value.get('name'),
            'summary': _memory_text(value.get('summary'), 220),
            'filled': value.get('filled'),
            'segments': value.get('segments'),
            'status': value.get('status'),
            'visibility': value.get('visibility'),
        }
    if kind == 'world_event' and isinstance(value, dict):
        return {
            'event_type': value.get('event_type'),
            'summary': _memory_text(value.get('summary'), 220),
            'visibility': value.get('visibility'),
        }
    if kind == 'npc_actor' and isinstance(value, dict):
        return {
            'name': value.get('name'),
            'role': value.get('role'),
            'public_summary': _memory_text(value.get('public_summary'), 220),
            'wants': [
                _memory_text(item, 100)
                for item in (value.get('wants') or [])[:3]
            ],
            'fears': [
                _memory_text(item, 100)
                for item in (value.get('fears') or [])[:3]
            ],
        }
    if isinstance(value, dict):
        compact = {}
        for key in (
            'name',
            'type',
            'summary',
            'text',
            'visibility',
            'certainty',
            'importance',
            'source_id',
            'target_id',
            'entity_ids',
        ):
            if key not in value:
                continue
            compact[key] = value.get(key)
        if 'summary' in compact:
            compact['summary'] = _memory_text(compact['summary'], 220)
        if 'text' in compact:
            compact['text'] = _memory_text(compact['text'], 220)
        return compact
    return _memory_text(value, 220)


def _compact_memory_search_result(result):
    if not isinstance(result, dict):
        return result
    matches = []
    for item in (result.get('matches') or [])[:4]:
        if not isinstance(item, dict):
            continue
        matches.append({
            'kind': item.get('kind'),
            'item_id': item.get('item_id'),
            'score': round(float(item.get('score') or 0.0), 3),
            'memory': _compact_memory_search_value(item.get('kind'), item.get('value')),
        })
    return {
        'query': result.get('query'),
        'matches': matches,
    }


def build_session_memory_context(campaign, session, current_user, player_message, dm_message, hot_context):
    memory_hot_context = _memory_hot_context_summary(hot_context)
    search_terms = ' '.join(
        text for text in [
            player_message,
            dm_message,
            json.dumps(memory_hot_context.get('current_scene', {}), ensure_ascii=False),
        ]
        if text
    )
    return {
        'campaign_id': campaign.id,
        'session_id': session.id,
        'current_user': {
            'id': current_user.id,
            'username': current_user.username,
        },
        'prior_running_summary': session.running_summary or '',
        'prior_memory_anchors': session.to_dict().get('memory_anchors'),
        'latest_player_message': player_message,
        'latest_dm_message': dm_message,
        'latest_dm_response_parts': [],
        'hot_context': memory_hot_context,
        'relevant_memory': _compact_memory_search_result(
            _tool_search_campaign_memory(
                campaign,
                current_user,
                {'query': search_terms[:240], 'limit': 5},
            )
        ),
        'active_clock_count': len(_active_clocks(campaign, limit=50)),
        'all_active_clocks_completed': all(
            (clock.status or 'active') not in ACTIVE_CLOCK_STATUSES or (clock.filled or 0) >= (clock.segments or 4)
            for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all()
        ),
    }


def build_session_summary_finalize_context(
    campaign,
    session,
    player_message,
    dm_message,
    player_message_id=None,
    dm_message_id=None,
):
    """Assemble the narrow context for finalizing the running summary against
    committed post-clock state: previous summary, current turn, committed scene,
    committed public facts, and committed active/resolved clocks. The summary is
    treated as a derived narrative projection of this authoritative state."""
    world, graph, world_state, _private = _world_json(campaign)
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}

    active_clocks = []
    resolved_clocks = []
    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all():
        visibility = clock.visibility or 'dm_private'
        if visibility not in {'public', 'party_known'}:
            # The running summary is party-facing; unrevealed dm_private clocks
            # must never be named in it.
            continue
        compact = {
            'clock_id': clock.clock_id,
            'name': clock.name,
            'segments': clock.segments,
            'filled': clock.filled,
            'status': clock.status,
            'visibility': visibility,
            'summary': clean_text(clock.summary, 220),
        }
        if (clock.status or 'active') in ACTIVE_CLOCK_STATUSES:
            active_clocks.append(compact)
        elif (clock.status or '') in {'resolved', 'completed', 'superseded'}:
            resolved_clocks.append(compact)

    return {
        'prior_running_summary': session.running_summary or '' if session else '',
        'latest_player_message': player_message,
        'latest_dm_message': dm_message,
        'current_scene': current_scene,
        'committed_facts': _established_public_facts(campaign, limit=6),
        'active_clocks': active_clocks[:8],
        'resolved_clocks': resolved_clocks[-8:],
    }


def build_session_clock_context(
    campaign,
    session,
    current_user,
    player_message,
    dm_message,
    current_scene_before,
    current_scene_after,
    player_message_id=None,
    dm_message_id=None,
):
    recent_events = WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.created_at.desc()).limit(6).all()
    active_clocks = []
    pending_completion_evidence = []
    allowed_evidence_sources = []
    allowed_evidence_keys = set()
    current_player_row = _resolve_clock_transcript_message(campaign, player_message_id)
    turn_started_at = current_player_row.created_at if current_player_row is not None else None

    def allow_source(source_type, source_id, chronology='historical_or_restated', occurred_at=None):
        if source_id in (None, ''):
            return
        source = {
            'source_type': source_type,
            'source_id': str(source_id),
            'chronology': chronology,
        }
        if occurred_at is not None:
            source['occurred_at'] = occurred_at.isoformat()
        key = _clock_evidence_key(source)
        if key is not None and key not in allowed_evidence_keys:
            allowed_evidence_keys.add(key)
            allowed_evidence_sources.append(source)

    allow_source('transcript_message', player_message_id, 'current_turn')
    allow_source('transcript_message', dm_message_id, 'current_turn')
    for event in recent_events:
        event_is_current = bool(
            turn_started_at is not None
            and event.created_at is not None
            and event.created_at >= turn_started_at
        )
        allow_source(
            'world_event',
            event.id,
            'current_turn' if event_is_current else 'historical_or_restated',
            event.created_at,
        )

    clocks = CampaignClock.query.filter_by(campaign_id=campaign.id).order_by(CampaignClock.id.asc()).all()
    for clock in [clock for clock in clocks if (clock.status or 'active') in ACTIVE_CLOCK_STATUSES][:50]:
        completion_state = clock.completion_state if isinstance(clock.completion_state, dict) else {}
        active_clocks.append({
            'clock_id': clock.clock_id,
            'name': clock.name,
            'segments': clock.segments,
            'filled': clock.filled,
            'pressure_type': clock.pressure_type,
            'visibility': clock.visibility,
            'summary': clock.summary,
            'trigger': clock.trigger,
            'trigger_clauses': _clock_trigger_clauses(clock),
            'on_complete': clock.on_complete,
            'completion_criteria': clock.completion_criteria or [],
            'completion_state': completion_state,
            'status': clock.status,
        })
        criteria_state = completion_state.get('criteria') if isinstance(completion_state.get('criteria'), dict) else {}
        for criterion_id, verdict in criteria_state.items():
            if not isinstance(verdict, dict):
                continue
            for source in verdict.get('evidence_sources') if isinstance(verdict.get('evidence_sources'), list) else []:
                key = _clock_evidence_key(source)
                if key is None:
                    continue
                source_type, source_id = key
                evidence_item = {
                    'clock_id': clock.clock_id,
                    'criterion_id': criterion_id,
                    'source_type': source_type,
                    'source_id': source_id,
                }
                if source_type == 'transcript_message':
                    message = _resolve_clock_transcript_message(campaign, source_id)
                    if message is None:
                        continue
                    evidence_item.update({'role': message.role, 'content': message.content})
                elif source_type == 'world_event':
                    event = _resolve_clock_world_event(campaign, source_id)
                    if event is None:
                        continue
                    evidence_item.update({'event': event.to_dict(include_private=True)})
                else:
                    continue
                allow_source(source_type, source_id)
                pending_completion_evidence.append(evidence_item)
    return {
        'campaign_id': campaign.id,
        'session_id': session.id,
        'current_user': {
            'id': current_user.id,
            'username': current_user.username,
        },
        'latest_player_message': {
            'source_type': 'transcript_message',
            'source_id': str(player_message_id) if player_message_id is not None else None,
            'content': player_message,
        },
        'latest_dm_message': {
            'source_type': 'transcript_message',
            'source_id': str(dm_message_id) if dm_message_id is not None else None,
            'content': dm_message,
        },
        'current_scene_before': current_scene_before if isinstance(current_scene_before, dict) else {},
        'current_scene_after': current_scene_after if isinstance(current_scene_after, dict) else {},
        'active_clocks': active_clocks,
        'recent_events': [event.to_dict(include_private=True) for event in reversed(recent_events)],
        'pending_completion_evidence': pending_completion_evidence,
        'allowed_evidence_sources': allowed_evidence_sources,
    }


SHEET_SCALAR_FIELDS = {
    'max_hp': {'type': 'int', 'min': 0},
    'current_hp': {'type': 'int', 'min': 0},
    'temp_hp': {'type': 'int', 'min': 0},
    'armor_class': {'type': 'int', 'min': 0},
    'initiative_bonus': {'type': 'int'},
    'speed': {'type': 'int', 'min': 0},
    'death_save_successes': {'type': 'int', 'min': 0, 'max': 3},
    'death_save_failures': {'type': 'int', 'min': 0, 'max': 3},
    'inspiration': {'type': 'bool'},
    'proficiency_bonus': {'type': 'int', 'min': 0},
    'passive_perception': {'type': 'int', 'min': 0},
    'exhaustion_level': {'type': 'int', 'min': 0, 'max': 6},
    'experience_points': {'type': 'int', 'min': 0},
    'cp': {'type': 'int', 'min': 0},
    'sp': {'type': 'int', 'min': 0},
    'ep': {'type': 'int', 'min': 0},
    'gp': {'type': 'int', 'min': 0},
    'pp': {'type': 'int', 'min': 0},
    'spell_slots_used_1': {'type': 'int', 'min': 0},
    'spell_slots_used_2': {'type': 'int', 'min': 0},
    'spell_slots_used_3': {'type': 'int', 'min': 0},
    'spell_slots_used_4': {'type': 'int', 'min': 0},
    'spell_slots_used_5': {'type': 'int', 'min': 0},
    'spell_slots_used_6': {'type': 'int', 'min': 0},
    'spell_slots_used_7': {'type': 'int', 'min': 0},
    'spell_slots_used_8': {'type': 'int', 'min': 0},
    'spell_slots_used_9': {'type': 'int', 'min': 0},
}

SHEET_LIST_OPERATIONS = ('condition', 'equipment')

FIELD_LABELS = {
    'max_hp': 'Max HP',
    'current_hp': 'Current HP',
    'temp_hp': 'Temp HP',
    'armor_class': 'Armor Class',
    'initiative_bonus': 'Initiative Bonus',
    'speed': 'Speed',
    'death_save_successes': 'Death Save Successes',
    'death_save_failures': 'Death Save Failures',
    'inspiration': 'Inspiration',
    'proficiency_bonus': 'Proficiency Bonus',
    'passive_perception': 'Passive Perception',
    'exhaustion_level': 'Exhaustion Level',
    'experience_points': 'Experience Points',
    'cp': 'CP',
    'sp': 'SP',
    'ep': 'EP',
    'gp': 'GP',
    'pp': 'PP',
    'spell_slots_used_1': 'Level 1 Spell Slots Used',
    'spell_slots_used_2': 'Level 2 Spell Slots Used',
    'spell_slots_used_3': 'Level 3 Spell Slots Used',
    'spell_slots_used_4': 'Level 4 Spell Slots Used',
    'spell_slots_used_5': 'Level 5 Spell Slots Used',
    'spell_slots_used_6': 'Level 6 Spell Slots Used',
    'spell_slots_used_7': 'Level 7 Spell Slots Used',
    'spell_slots_used_8': 'Level 8 Spell Slots Used',
    'spell_slots_used_9': 'Level 9 Spell Slots Used',
}


def _sheet_field_label(field):
    clean = field.split(':', 1)[0]
    return FIELD_LABELS.get(clean, clean.replace('_', ' ').title())


def _current_list_item_count(character, relation_name, item_name):
    items = getattr(character, relation_name, [])
    return sum(
        1 for item in items
        if (getattr(item, 'condition_name', None) or getattr(item, 'name', None) or '').lower() == item_name.lower()
    )


def _compute_change(character, change):
    field = change.get('field', '')
    operation = change.get('operation', '')
    raw_value = change.get('value', 0)

    if ':' in field:
        prefix, item_name = field.split(':', 1)
        prefix = prefix.strip().lower()
        if prefix not in SHEET_LIST_OPERATIONS:
            return None
        item_name = item_name.strip()
        label_prefix = prefix.capitalize()
        count = _current_list_item_count(character, f'{prefix}s' if prefix != 'equipment' else prefix, item_name)
        if operation == 'add' or (operation == 'set' and raw_value):
            return {
                'field': field,
                'operation': operation,
                'value': {'name': item_name},
                'before': {'count': count},
                'after': {'count': count + 1},
                'label': f'{label_prefix}: {item_name}',
            }
        elif operation in ('subtract', 'remove') or (operation == 'set' and not raw_value):
            return {
                'field': field,
                'operation': operation,
                'value': {'name': item_name},
                'before': {'count': count},
                'after': {'count': max(0, count - 1)},
                'label': f'{label_prefix}: {item_name}',
            }
        return None

    config = SHEET_SCALAR_FIELDS.get(field)
    if not config:
        return None

    try:
        current = getattr(character, field, 0)
    except (TypeError, AttributeError):
        return None

    if config['type'] == 'bool':
        current = bool(current)
        new = bool(raw_value)
        if operation == 'set':
            new = bool(raw_value)
        elif operation == 'add':
            new = True
        elif operation == 'subtract':
            new = False
        else:
            return None
    else:
        try:
            current = int(current) if current is not None else 0
            delta = int(raw_value)
        except (TypeError, ValueError):
            return None

        if operation == 'set':
            new = delta
        elif operation == 'add':
            new = current + delta
        elif operation == 'subtract':
            new = current - delta
        else:
            return None

        min_val = config.get('min')
        max_val = config.get('max')
        if min_val is not None:
            new = max(min_val, new)
        if max_val is not None:
            new = min(max_val, new)

    return {
        'field': field,
        'operation': operation,
        'value': raw_value,
        'before': current,
        'after': new,
        'label': _sheet_field_label(field),
    }


DM_TOOL_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'ask_character_sheet',
            'description': 'Ask a focused question about the current player, party, or a specific character sheet and receive only the concise answer needed.',
            'parameters': {
                'type': 'object',
                'required': ['question'],
                'properties': {
                    'scope': {
                        'type': 'string',
                        'enum': ['current_player', 'party', 'character_id'],
                        'default': 'current_player',
                    },
                    'character_id': {'type': 'integer'},
                    'question': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_current_scene',
            'description': 'Fetch current scene, active clocks, active NPCs, and recent world events.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'include_private': {'type': 'boolean', 'default': True},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_campaign_memory',
            'description': 'Search campaign memory across graph entities, relations, facts, NPC actors, clocks, events, and planning summary.',
            'parameters': {
                'type': 'object',
                'required': ['query'],
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'default': 8, 'minimum': 1, 'maximum': 20},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'record_world_event',
            'description': 'Record a durable world event after something meaningful happens in play.',
            'parameters': {
                'type': 'object',
                'required': ['event_type', 'summary'],
                'properties': {
                    'event_type': {'type': 'string'},
                    'summary': {'type': 'string'},
                    'payload': {'type': 'object'},
                    'visibility': {'type': 'string', 'enum': ['public', 'party_known', 'dm_private'], 'default': 'dm_private'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_current_scene',
            'description': 'Update current scene fields in world state and record the change.',
            'parameters': {
                'type': 'object',
                'required': ['scene_patch'],
                'properties': {
                    'scene_patch': {'type': 'object'},
                    'reason': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'reveal_fact',
            'description': 'Change a graph entity, relation, or fact visibility after it becomes known through play.',
            'parameters': {
                'type': 'object',
                'required': ['item_type', 'item_id', 'visibility'],
                'properties': {
                    'item_type': {'type': 'string', 'enum': ['entity', 'relation', 'fact']},
                    'item_id': {'type': 'string'},
                    'visibility': {'type': 'string', 'enum': ['public', 'party_known', 'dm_private']},
                    'reason': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'propose_sheet_update',
            'description': 'Propose a mechanical update to a character sheet based on visible events in play. Use ask_character_sheet first to check current values. The player must approve the change unless the character is DM-controlled.',
            'parameters': {
                'type': 'object',
                'required': ['character_id', 'reason', 'changes'],
                'properties': {
                    'character_id': {'type': 'integer', 'description': 'ID of the character to update.'},
                    'reason': {'type': 'string', 'description': 'Brief player-visible explanation of why this change is happening.'},
                    'changes': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'required': ['field', 'operation', 'value'],
                            'properties': {
                                'field': {
                                    'type': 'string',
                                    'description': 'Scalar field (current_hp, gp, inspiration, spell_slots_used_1, exhaustion_level, etc.) or list field (condition:Poisoned, equipment:Potion of Healing).',
                                },
                                'operation': {
                                    'type': 'string',
                                    'enum': ['add', 'subtract', 'set'],
                                    'description': 'add, subtract, or set the value.',
                                },
                                'value': {
                                    'type': 'number',
                                    'description': 'Numeric value to add/subtract/set. For boolean fields use 1 (true) or 0 (false).',
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_loot_box',
            'description': 'Generate a loot box for the party after a notable achievement like a combat victory, discovery, or reward scene. Call this when the party earns treasure through their actions. The generated loot box will appear in the campaign stash for players to inspect and open.',
            'parameters': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': 'Thematic name for this loot box, e.g. "Goblin Chieftain\'s Hoard" or "Crypt of the Forgotten Knight".',
                    },
                    'description': {
                        'type': 'string',
                        'description': 'Flavor text describing where this loot came from and what it looks like. Will be visible to players.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'roll_dice',
            'description': 'Roll dice deterministically for DM-controlled mechanics such as monster attacks, damage, saves, recharge, or random tables. Supports additive expressions like 1d20+4, 2d6+3, and keep-highest/lowest forms like 2d20kh1.',
            'parameters': {
                'type': 'object',
                'required': ['expression'],
                'properties': {
                    'expression': {
                        'type': 'string',
                        'description': 'Dice expression such as 1d20+4, 2d10, 4d6kh3, or 1d8+1d6+2.',
                    },
                    'reason': {
                        'type': 'string',
                        'description': 'Optional short note about why this roll is happening.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_encounter_map',
            'description': 'Generate a party-visible gridded D&D battle map for a tactical encounter area, then create structured VTT setup JSON with friendly/enemy spawn boxes, terrain labels, obstacles, and tactical notes. Use this when spatial positioning matters or when players ask for a map.',
            'parameters': {
                'type': 'object',
                'required': ['title', 'map_prompt'],
                'properties': {
                    'title': {
                        'type': 'string',
                        'description': 'Short player-visible title for the map, e.g. "Ruined Chapel Ambush".',
                    },
                    'map_prompt': {
                        'type': 'string',
                        'description': 'Concrete visual description of the battle map layout and important zones.',
                    },
                    'terrain': {
                        'type': 'string',
                        'description': 'Optional terrain notes such as forest, cavern, city street, ship deck, or dungeon room.',
                    },
                    'tactical_features': {
                        'type': 'string',
                        'description': 'Optional tactical details such as cover, choke points, elevation, hazards, doors, water, bridges, or obstacles.',
                    },
                    'vtt_setup_notes': {
                        'type': 'string',
                        'description': 'Optional DM-only setup instructions for the VTT JSON, such as where friendly PCs should start, where enemies should spawn, which obstacles or terrain matter, and any intended tactical objective. This guides metadata only and is not shown as map text.',
                    },
                    'mood': {
                        'type': 'string',
                        'description': 'Optional mood and lighting notes.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'place_encounter_map_actors',
            'description': 'Place or move players, NPCs, and monsters on the current encounter map grid. Use this after a map exists and combat or tactical positioning matters. Monster ids are created automatically if missing. If a placement is on blocked or hazardous terrain, the tool returns placement_warnings and saves nothing unless allow_illegal_placements is explicitly true.',
            'parameters': {
                'type': 'object',
                'required': ['placements'],
                'properties': {
                    'encounter_map_id': {
                        'type': 'integer',
                        'description': 'Optional explicit encounter map id. Defaults to the latest campaign encounter map.',
                    },
                    'clear_existing': {
                        'type': 'boolean',
                        'default': False,
                        'description': 'When true, remove existing placements on this map before applying the supplied placements.',
                    },
                    'allow_illegal_placements': {
                        'type': 'boolean',
                        'default': False,
                        'description': 'Set true only after reviewing placement_warnings from a previous call and intentionally placing actors on blocked or hazardous map cells.',
                    },
                    'placements': {
                        'type': 'array',
                        'minItems': 1,
                        'items': {
                            'type': 'object',
                            'required': ['actor_type', 'actor_id', 'col', 'row'],
                            'properties': {
                                'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                                'actor_id': {
                                    'type': 'string',
                                    'description': 'Player user id or selected character id, NPC actor_id, or monster_id.',
                                },
                                'col': {'type': 'integer', 'minimum': 0},
                                'row': {'type': 'integer', 'minimum': 0},
                                'label': {'type': 'string'},
                                'monster_name': {
                                    'type': 'string',
                                    'description': 'Optional name to use when actor_type is monster and the monster id is new.',
                                },
                                'stat_block': {
                                    'type': 'object',
                                    'description': 'Optional structured monster stats for newly created monster ids.',
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'move_encounter_actor',
            'description': 'Move an already-placed encounter actor across the current map using terrain-aware pathfinding. Use this for tactical enemy or NPC movement after actors are already on the board. During combat, this consumes movement_remaining and can enforce turn order.',
            'parameters': {
                'type': 'object',
                'required': ['col', 'row'],
                'properties': {
                    'encounter_map_id': {
                        'type': 'integer',
                        'description': 'Optional explicit encounter map id. Defaults to the latest campaign encounter map.',
                    },
                    'placement_id': {
                        'type': 'integer',
                        'description': 'Preferred explicit placement id for the actor to move.',
                    },
                    'actor_type': {
                        'type': 'string',
                        'enum': ['player', 'npc', 'monster'],
                        'description': 'Actor type when placement_id is not provided.',
                    },
                    'actor_id': {
                        'type': 'string',
                        'description': 'User id or selected character id for players, NPC actor_id, or monster_id when placement_id is not provided.',
                    },
                    'col': {
                        'type': 'integer',
                        'minimum': 0,
                        'description': 'Destination grid column.',
                    },
                    'row': {
                        'type': 'integer',
                        'minimum': 0,
                        'description': 'Destination grid row.',
                    },
                    'ignore_turn_order': {
                        'type': 'boolean',
                        'default': False,
                        'description': 'Set true to move a combatant outside their active turn during combat. Use sparingly for forced movement, reactions, or repositioning corrections.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_encounter_overview',
            'description': 'Read the current encounter round, active turn, and compact summaries for every combatant without dumping the full encounter state into the model context.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'encounter_map_id': {
                        'type': 'integer',
                        'description': 'Optional explicit encounter map id. Defaults to the latest campaign encounter map.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_combatant_state',
            'description': 'Read a compact combat summary for one combatant, including HP, temp HP, conditions, actions, position, and whether it is their turn.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'encounter_map_id': {
                        'type': 'integer',
                        'description': 'Optional explicit encounter map id. Defaults to the latest campaign encounter map.',
                    },
                    'placement_id': {
                        'type': 'integer',
                        'description': 'Preferred explicit placement id for the combatant.',
                    },
                    'actor_type': {
                        'type': 'string',
                        'enum': ['player', 'npc', 'monster'],
                        'description': 'Actor type when placement_id is not provided.',
                    },
                    'actor_id': {
                        'type': 'string',
                        'description': 'User id or selected character id for players, NPC actor_id, or monster_id when placement_id is not provided.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_reachable_positions',
            'description': 'Return the legal grid cells a combatant can currently reach with its remaining movement, using map terrain and current movement budget.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'encounter_map_id': {
                        'type': 'integer',
                        'description': 'Optional explicit encounter map id. Defaults to the latest campaign encounter map.',
                    },
                    'placement_id': {
                        'type': 'integer',
                        'description': 'Preferred explicit placement id for the combatant.',
                    },
                    'actor_type': {
                        'type': 'string',
                        'enum': ['player', 'npc', 'monster'],
                        'description': 'Actor type when placement_id is not provided.',
                    },
                    'actor_id': {
                        'type': 'string',
                        'description': 'User id or selected character id for players, NPC actor_id, or monster_id when placement_id is not provided.',
                    },
                    'max_cells': {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': 500,
                        'description': 'Maximum number of reachable cells to return.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'toggle_encounter_mode',
            'description': 'Start or stop Encounter Mode (combat tracker). Starting it will return instructions to generate a new map and place actors. Stopping it will archive the current map.',
            'parameters': {
                'type': 'object',
                'required': ['active'],
                'properties': {
                    'active': {
                        'type': 'boolean',
                        'description': 'True to start Encounter Mode, False to stop/clear it.'
                    }
                }
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'next_combat_turn',
            'description': 'Advance combat tracker to the next combatant, resetting their actions/movement and potentially advancing the round counter.',
            'parameters': {
                'type': 'object',
                'properties': {}
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_combat_turn',
            'description': 'Directly jump to a specific combatant in the turn order list by index.',
            'parameters': {
                'type': 'object',
                'required': ['active_turn_index'],
                'properties': {
                    'active_turn_index': {
                        'type': 'integer',
                        'description': 'Index in the turn order list to jump to.'
                    }
                }
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_combatant_actions',
            'description': 'Manually update a combatant action resources (action, bonus_action, reaction, movement_remaining).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'placement_id': {
                        'type': 'integer',
                        'description': 'Find combatant by placement ID.'
                    },
                    'actor_type': {
                        'type': 'string',
                        'enum': ['player', 'npc', 'monster']
                    },
                    'actor_id': {
                        'type': 'string'
                    },
                    'actions': {
                        'type': 'object',
                        'description': 'Map of action flags/movement to update.',
                        'properties': {
                            'action': {'type': 'boolean'},
                            'bonus_action': {'type': 'boolean'},
                            'reaction': {'type': 'boolean'},
                            'movement_remaining': {'type': 'integer'}
                        }
                    }
                }
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_combatant_hp',
            'description': 'Set exact HP values for a combatant and sync them to persistent state. Use this for corrections, imported results, or explicit overwrites.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'max_hp': {'type': 'integer', 'minimum': 0},
                    'current_hp': {'type': 'integer', 'minimum': 0},
                    'temp_hp': {'type': 'integer', 'minimum': 0},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'apply_damage',
            'description': 'Apply damage to a combatant deterministically, consuming temp HP first by default and syncing the updated HP to persistent state.',
            'parameters': {
                'type': 'object',
                'required': ['amount'],
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'amount': {'type': 'integer', 'minimum': 0},
                    'damage_type': {'type': 'string'},
                    'ignore_temp_hp': {'type': 'boolean', 'default': False},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'apply_healing',
            'description': 'Apply healing to a combatant deterministically, capping at max HP and syncing the updated HP to persistent state.',
            'parameters': {
                'type': 'object',
                'required': ['amount'],
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'amount': {'type': 'integer', 'minimum': 0},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'grant_temp_hp',
            'description': 'Grant or adjust temporary HP for a combatant. Use mode=max for normal 5e temp-HP replacement, replace for hard set, or add for exceptional stacking cases.',
            'parameters': {
                'type': 'object',
                'required': ['amount'],
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'amount': {'type': 'integer', 'minimum': 0},
                    'mode': {'type': 'string', 'enum': ['max', 'replace', 'add']},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_combatant_initiative',
            'description': 'Set a combatant initiative value directly and re-sort turn order using initiative, then initiative_bonus, then placement order.',
            'parameters': {
                'type': 'object',
                'required': ['initiative'],
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'initiative': {'type': 'integer'},
                    'initiative_bonus': {'type': 'integer'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_combatant_conditions',
            'description': 'Set, add, or remove combat conditions on a combatant and sync them to persistent state for players, NPCs, and monsters.',
            'parameters': {
                'type': 'object',
                'required': ['conditions'],
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                    'mode': {'type': 'string', 'enum': ['set', 'add', 'remove']},
                    'conditions': {
                        'type': 'array',
                        'items': {
                            'oneOf': [
                                {'type': 'string'},
                                {
                                    'type': 'object',
                                    'properties': {
                                        'name': {'type': 'string'},
                                        'source': {'type': 'string'},
                                        'duration': {'type': 'string'},
                                        'note': {'type': 'string'},
                                    },
                                },
                            ],
                        },
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remove_encounter_actor',
            'description': 'Remove a placed actor from the current encounter map and current turn order. Use this for defeated monsters, despawns, or cleanup corrections.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'encounter_map_id': {'type': 'integer'},
                    'placement_id': {'type': 'integer'},
                    'actor_type': {'type': 'string', 'enum': ['player', 'npc', 'monster']},
                    'actor_id': {'type': 'string'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_shop_list',
            'description': 'Create or update all local merchants for the current scene. Provide concise shop summaries only; a separate shop generator will expand each merchant into a priced item menu and save the shop records.',
            'parameters': {
                'type': 'object',
                'required': ['shops'],
                'properties': {
                    'scene_note': {
                        'type': 'string',
                        'description': 'Optional brief note about the market, street, district, or trade scene these shops belong to.'
                    },
                    'shops': {
                        'type': 'array',
                        'description': 'Concise list of merchants or storefronts available in this current scene.',
                        'minItems': 1,
                        'maxItems': 12,
                        'items': {
                            'type': 'object',
                            'required': ['name', 'description', 'specialties'],
                            'properties': {
                                'name': {
                                    'type': 'string',
                                    'description': 'Thematic merchant or shop name, e.g. "Grom\'s Armory" or "The Whispering Elixir".'
                                },
                                'description': {
                                    'type': 'string',
                                    'description': 'Concise flavor text describing the merchant, storefront, and atmosphere.'
                                },
                                'specialties': {
                                    'type': 'array',
                                    'description': 'What this merchant sells, such as weapons, armor, potions, maps, mounts, rations, tools, books, or luxury goods.',
                                    'items': {'type': 'string'}
                                },
                                'price_level': {
                                    'type': 'string',
                                    'enum': ['budget', 'standard', 'premium', 'luxury'],
                                    'description': 'General price band for this merchant.'
                                },
                                'item_count': {
                                    'type': 'integer',
                                    'minimum': 1,
                                    'maximum': 12,
                                    'description': 'Approximate number of menu items the shop generator should create.'
                                }
                            }
                        }
                    }
                }
            }
        }
    }
]


def get_dm_tool_definitions(campaign):
    if not campaign:
        return DM_TOOL_DEFINITIONS

    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}

    encounter_active = bool(settings.get('encounter_active', False))

    if encounter_active:
        return DM_TOOL_DEFINITIONS

    exclude_names = {
        'create_encounter_map',
        'place_encounter_map_actors',
        'move_encounter_actor',
        'get_encounter_overview',
        'get_combatant_state',
        'list_reachable_positions',
        'next_combat_turn',
        'set_combat_turn',
        'update_combatant_actions',
        'set_combatant_hp',
        'apply_damage',
        'apply_healing',
        'grant_temp_hp',
        'set_combatant_initiative',
        'update_combatant_conditions',
        'remove_encounter_actor',
    }
    return [tool for tool in DM_TOOL_DEFINITIONS if tool['function']['name'] not in exclude_names]



def _tool_ask_character_sheet(campaign, current_user, args, audit_context=None):
    scope = args.get('scope') or 'current_player'
    question = clean_text(args.get('question'), 600)
    if not question:
        return {
            'scope': scope,
            'answer': 'A character-sheet question is required.',
            'character_ids': [],
            'missing': True,
        }

    if scope == 'party':
        members = CampaignMember.query.filter_by(campaign_id=campaign.id).order_by(CampaignMember.id.asc()).all()
        character_sheets = []
        for member in members:
            character = db.session.get(Character, member.selected_character_id) if member.selected_character_id else None
            if character:
                character_sheets.append({
                    'user_id': member.user_id,
                    'username': member.user.username if member.user else None,
                    'character': character_full_dict(character),
                })
    else:
        if scope == 'character_id':
            character = Character.query.filter_by(id=args.get('character_id'), campaign_id=campaign.id).first()
        else:
            character = _current_character(campaign, current_user)
        character_sheets = [{
            'user_id': character.user_id,
            'username': current_user.username if current_user and character.user_id == current_user.id else None,
            'character': character_full_dict(character),
        }] if character else []

    result = get_character_sheet_answer(question, scope, character_sheets, audit_context=audit_context)
    return {'scope': scope, **result}


def _tool_propose_sheet_update(campaign, current_user, args, session, audit_context=None):
    character_id = args.get('character_id')
    reason = clean_text(args.get('reason', ''), 500)
    raw_changes = args.get('changes', [])

    if not character_id or not reason or not raw_changes:
        return {'error': 'character_id, reason, and changes are required.'}

    character = Character.query.filter_by(id=character_id, campaign_id=campaign.id).first()
    if not character:
        return {'error': 'Character not found in this campaign.'}

    computed_changes = []
    for change in raw_changes:
        result = _compute_change(character, change)
        if result:
            computed_changes.append(result)

    if not computed_changes:
        return {'error': 'No valid changes could be computed from the provided data.'}

    proposal = SheetProposal(
        session_id=session.id,
        character_id=character_id,
        dm_user_id=current_user.id,
        reason=reason,
        changes=computed_changes,
        status='pending',
    )
    db.session.add(proposal)
    db.session.commit()

    return {
        'proposal_id': proposal.id,
        'character_id': character_id,
        'reason': reason,
        'changes': computed_changes,
        'status': 'pending',
    }


def _prepare_sheet_proposal(campaign, current_user, args, session):
    character_id = args.get('character_id')
    reason = clean_text(args.get('reason', ''), 500)
    raw_changes = args.get('changes', [])
    if not character_id or not reason or not raw_changes:
        return {'error': 'character_id, reason, and changes are required.'}
    character = Character.query.filter_by(id=character_id, campaign_id=campaign.id).first()
    if not character:
        return {'error': 'Character not found.'}
    changes = [result for change in raw_changes if (result := _compute_change(character, change))]
    if not changes:
        return {'error': 'No valid changes could be computed from the provided data.'}
    return {
        'character_id': character_id,
        'reason': reason,
        'changes': changes,
        'status': 'pending',
    }


def _tool_get_current_scene(campaign, _current_user, args):
    include_private = args.get('include_private', True) is not False
    world, _graph, world_state, _private = _world_json(campaign)
    active_npc_ids = []
    current_scene = {}
    if isinstance(world_state, dict):
        current_scene = world_state.get('current_scene', {}) or {}
        active_npc_ids = current_scene.get('active_npc_ids', []) if isinstance(current_scene, dict) else []
    npcs = NPCActor.query.filter_by(campaign_id=campaign.id).order_by(NPCActor.id.asc()).all()
    if active_npc_ids:
        active_npcs = [
            npc for npc in npcs
            if npc.actor_id in active_npc_ids or str(npc.id) in {str(item) for item in active_npc_ids}
        ]
    else:
        active_npcs = npcs[:4]
    events = WorldEvent.query.filter_by(campaign_id=campaign.id).order_by(WorldEvent.created_at.desc()).limit(8).all()
    return {
        'has_world': bool(world),
        'current_scene': current_scene,
        'active_clocks': [clock.to_dict(include_private=include_private) for clock in _active_clocks(campaign)],
        'active_npcs': [npc.to_dict(include_private=include_private) for npc in active_npcs],
        'recent_events': [event.to_dict(include_private=include_private) for event in reversed(events)],
    }


def _match_score(query_terms, value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return sum(1 for term in query_terms if term and term in text)


def _candidate_key(item):
    return (str(item.get('kind')), str(item.get('item_id')))


def _campaign_memory_candidates(campaign):
    _world, graph, world_state, dm_private = _world_json(campaign)
    candidates = []

    for plural_kind, item_type in (
        ('entities', 'entity'),
        ('relations', 'relation'),
        ('facts', 'fact'),
    ):
        for item in graph.get(plural_kind, []) if isinstance(graph, dict) else []:
            candidates.append({'kind': item_type, 'item_id': item.get('id'), 'value': item})
    for npc in NPCActor.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'npc_actor', 'item_id': npc.actor_id, 'value': npc.to_dict(include_private=True)})
    for clock in CampaignClock.query.filter_by(campaign_id=campaign.id).all():
        candidates.append({'kind': 'clock', 'item_id': clock.clock_id, 'value': clock.to_dict(include_private=True)})
    for event in (
        WorldEvent.query
        .filter_by(campaign_id=campaign.id)
        .order_by(WorldEvent.created_at.desc(), WorldEvent.id.desc())
        .limit(30)
        .all()
    ):
        candidates.append({'kind': 'world_event', 'item_id': str(event.id), 'value': event.to_dict(include_private=True)})
    candidates.append({'kind': 'world_state', 'item_id': 'current', 'value': world_state})
    candidates.append({'kind': 'dm_private', 'item_id': 'current', 'value': dm_private})
    candidates.append({'kind': 'planning_summary', 'item_id': 'current', 'value': summary_dict_for_read(campaign.id, include_private=True)})
    return candidates


def _rank_campaign_memory_candidates(query, candidates, semantic_scores):
    terms = [term for term in query.replace('_', ' ').split() if len(term) > 2]
    weight = search_weight()
    scored = []
    for item in candidates:
        keyword_score = _match_score(terms, item['value'])
        semantic_score = semantic_scores.get(_candidate_key(item), 0.0)
        score = keyword_score + (semantic_score * weight)
        if score:
            scored.append({
                **item,
                'score': score,
                'keyword_score': keyword_score,
                'embedding_score': semantic_score,
            })
    return sorted(scored, key=lambda item: item['score'], reverse=True)


def _tool_search_campaign_memory(campaign, _current_user, args, audit_context=None):
    query = clean_text(args.get('query'), 240).lower()
    limit = min(max(int(args.get('limit') or 8), 1), 20)
    candidates = _campaign_memory_candidates(campaign)

    semantic = search_memory_embeddings(campaign, query, candidates, limit, audit_context=audit_context)
    semantic_scores = semantic.get('scores') if semantic.get('ok') else {}
    scored = _rank_campaign_memory_candidates(query, candidates, semantic_scores)
    return {'query': query, 'matches': scored[:limit]}


def _retrieval_visibility(item):
    value = item.get('value') if isinstance(item, dict) else {}
    return clean_text(value.get('visibility') if isinstance(value, dict) else '', 40) or 'dm_private'


def _retrieval_certainty(item):
    value = item.get('value') if isinstance(item, dict) else {}
    return clean_text(value.get('certainty') if isinstance(value, dict) else '', 40) or 'confirmed'


def build_session_retrieval_packet(campaign, current_user, hot_context, preflight_decision, audit_context=None):
    """Collect a small, lane-diverse evidence packet before visible DM generation."""
    preflight_decision = preflight_decision or {}
    mode = str(preflight_decision.get('dm_reply_mode') or '').strip().lower()
    confidence = str(preflight_decision.get('confidence') or '').strip().lower()
    safe_modes = {'silent', 'ooc_only', 'mechanics_only', 'clarification_only'}
    if mode in safe_modes or (mode == 'simple_narrative' and confidence == 'high'):
        return None

    recent_messages = (hot_context or {}).get('recent_messages') or []
    player_text = ''
    for message in reversed(recent_messages):
        if message.get('role') == 'player':
            player_text = clean_text(message.get('content'), 240)
            break
    raw_queries = preflight_decision.get('retrieval_queries') if isinstance(preflight_decision.get('retrieval_queries'), dict) else {}
    defaults = {
        'entities': f'Named people, places, objects, and factions related to {player_text}',
        'scene_events': f'Current scene and recent events relevant to {player_text}',
        'clocks_promises': f'Active clocks, promises, and unresolved pressure relevant to {player_text}',
        'prior_facts': f'Prior campaign facts relevant to {player_text}',
    }
    queries = {
        lane: clean_text(raw_queries.get(lane), 240) or defaults[lane]
        for lane in RETRIEVAL_LANE_ORDER
    }
    candidates = _campaign_memory_candidates(campaign)
    semantic = search_memory_embeddings_batch(
        campaign,
        queries,
        candidates,
        audit_context=audit_context,
    )
    scores_by_query = semantic.get('scores_by_query') if semantic.get('ok') else {}
    lane_rankings = {
        lane: _rank_campaign_memory_candidates(queries[lane].lower(), candidates, scores_by_query.get(lane, {}))
        for lane in RETRIEVAL_LANE_ORDER
    }

    choices = []
    for lane_index, lane in enumerate(RETRIEVAL_LANE_ORDER):
        for item in lane_rankings[lane]:
            choices.append((item.get('score', 0.0), -lane_index, lane, item))
    choices.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)

    selected = {lane: [] for lane in RETRIEVAL_LANE_ORDER}
    seen = set()
    for _score, _lane_priority, lane, item in choices:
        key = _candidate_key(item)
        if key in seen or len(selected[lane]) >= 2:
            continue
        seen.add(key)
        visibility = _retrieval_visibility(item)
        selected[lane].append({
            'kind': item.get('kind'),
            'item_id': item.get('item_id'),
            'score': round(float(item.get('score') or 0), 4),
            'visibility': visibility,
            'certainty': _retrieval_certainty(item),
            'internal_only': visibility == 'dm_private',
            'memory': _compact_memory_search_value(item.get('kind'), item.get('value')),
        })

    packet = {
        'queries': queries,
        'lanes': [
            {'lane': lane, 'matches': selected[lane]}
            for lane in RETRIEVAL_LANE_ORDER
        ],
        'semantic_available': bool(semantic.get('ok')),
    }
    if campaign and audit_context:
        log_audit_event(
            campaign.id,
            'session_retrieval_packet',
            'Built focused pre-generation retrieval evidence packet.',
            packet,
            source='session_context',
            actor='session_retrieval',
            trace_id=audit_context.get('trace_id'),
            parent_trace_id=audit_context.get('parent_trace_id'),
            trace_label=audit_context.get('trace_label'),
            audit_role='tools',
            commit=False,
        )
    return packet


def _record_event(campaign, event_type, summary, payload=None, visibility='dm_private'):
    event = WorldEvent(
        campaign_id=campaign.id,
        event_type=clean_id(event_type, 'world_event')[:80],
        summary=clean_text(summary, 1200) or 'World event recorded.',
        payload=json_dumps(payload or {}),
        visibility=visibility if visibility in VALID_VISIBILITIES else 'dm_private',
    )
    db.session.add(event)
    db.session.flush()
    upsert_memory_embedding(campaign, 'world_event', str(event.id), event.to_dict(include_private=True))
    return event


def _tool_record_world_event(campaign, _current_user, args):
    event = _record_event(
        campaign,
        args.get('event_type'),
        args.get('summary'),
        payload=args.get('payload') if isinstance(args.get('payload'), dict) else {},
        visibility=args.get('visibility') or 'dm_private',
    )
    return {'event': event.to_dict(include_private=True), 'affected_ids': {'world_event_ids': [event.id]}}


def _tool_update_current_scene(campaign, _current_user, args):
    world, _graph, world_state, _private = _world_json(campaign)
    if not world:
        return {'error': 'No world package exists.'}
    scene_patch = args.get('scene_patch') if isinstance(args.get('scene_patch'), dict) else {}
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    
    from services.scene_location_resolver import resolve_scene_location_patch
    loc_patch = resolve_scene_location_patch(scene_patch, campaign, current_scene)
    
    # Enforce resolver or omission
    clean_scene_patch = {k: v for k, v in scene_patch.items() if k not in ('location_id', 'location_name')}
    loc_status = loc_patch.get("status", "unresolved") if isinstance(loc_patch, dict) else "unresolved"
    if loc_status in ("canonical", "direct", "new"):
        clean_scene_patch.update({"location_id": loc_patch["location_id"], "location_name": loc_patch["location_name"]})
        
    current_scene.update(clean_scene_patch)
    world_state['current_scene'] = current_scene
    _sync_party_known_location(world_state, current_scene)
    world.world_state = json_dumps(world_state)
    world.updated_at = utcnow()
    upsert_memory_embedding(campaign, 'world_state', 'current', world_state)
    event = _record_event(
        campaign,
        'scene_updated',
        args.get('reason') or 'Current scene updated.',
        {'scene_patch': scene_patch, 'current_scene': current_scene},
    )
    return {'current_scene': current_scene, 'affected_ids': {'world_id': world.id, 'world_event_ids': [event.id]}}


def _tool_advance_clock(campaign, _current_user, args):
    clock_id = clean_id(args.get('clock_id'), '')
    clock = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    if not clock:
        return {'error': f'Clock not found: {clock_id}'}
    try:
        delta = int(args.get('delta') or 0)
    except (TypeError, ValueError):
        delta = 0
    old_filled = clock.filled or 0
    clock.filled = min(max(old_filled + delta, 0), clock.segments or 4)
    completion_criteria = _normalize_clock_completion_criteria(clock.completion_criteria)
    if clock.filled >= (clock.segments or 4) and completion_criteria:
        # A full progress bar is not a completed investigation when its declared
        # completion conditions still require explicit evidence.
        clock.status = 'completion_pending'
    elif args.get('status'):
        clock.status = clean_text(args.get('status'), 30)
    elif clock.filled >= (clock.segments or 4):
        clock.status = 'completed'
    elif (clock.status or 'active') in {'completed', 'completion_pending'} and clock.filled < (clock.segments or 4):
        clock.status = 'active'
    clock.updated_at = utcnow()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    evidence = args.get('evidence') if isinstance(args.get('evidence'), list) else []
    evidence_sources = []
    for ev in evidence:
        if isinstance(ev, dict) and ev.get('source_type'):
            evidence_sources.append(dict(ev))
        elif ev:
            evidence_sources.append({'source_type': 'resolver_output', 'source_id': str(ev)})
    from services.audit_service import log_audit_event as _log_audit
    from openrouter import _get_cached_build_sha
    upstream = args.get('provenance') if isinstance(args.get('provenance'), dict) else {}
    provenance = {
        'tool_name': upstream.get('tool_name') or 'session_dm_advance_clock',
        'pipeline_stage': 'applied',
        'evidence_sources': upstream.get('evidence_sources') or evidence_sources or None,
        'evidence_status': upstream.get('evidence_status') or (determine_evidence_status(evidence_sources) if evidence_sources else 'insufficiently_supported'),
        'resolution_confidence': upstream.get('resolution_confidence'),
        'ambiguity_status': upstream.get('ambiguity_status'),
        'delta': delta,
        'source_player_message_id': upstream.get('source_player_message_id'),
        'source_dm_message_id': upstream.get('source_dm_message_id'),
        'trace_id': upstream.get('trace_id'),
        'build_sha': upstream.get('build_sha') or _get_cached_build_sha(),
    }
    if upstream.get('clock_trigger_rule_id'):
        provenance['clock_trigger_rule_id'] = upstream['clock_trigger_rule_id']
    for field in ('trigger_clause_id', 'chronology_verdict', 'new_evidence_ids', 'trigger_verdict'):
        if upstream.get(field) is not None:
            provenance[field] = upstream[field]
    event = _record_event(
        campaign,
        'clock_advanced',
        args.get('reason') or f'{clock.name} changed by {delta}.',
        {
            'clock_id': clock.clock_id,
            'from': old_filled,
            'to': clock.filled,
            'delta': delta,
            'status': clock.status,
            'evidence': [clean_text(item, 240) for item in evidence if clean_text(item, 240)],
            'provenance': provenance,
        },
        visibility=clock.visibility or 'dm_private',
    )
    _log_audit(
        campaign.id,
        'clock_advanced',
        f'Clock "{clock.name}" advanced by {delta}',
        {
            'clock_id': clock.clock_id,
            'from': old_filled,
            'to': clock.filled,
            'delta': delta,
            'status': clock.status,
            'provenance': provenance,
        },
        source='tools',
        actor='session_dm',
        audit_role='tools',
    )
    return {'clock': clock.to_dict(include_private=True), 'affected_ids': {'clock_ids': [clock.id], 'world_event_ids': [event.id]}}


def _tool_reveal_fact(campaign, _current_user, args):
    world, graph, _world_state, _private = _world_json(campaign)
    if not world:
        return {'error': 'No world package exists.'}
    item_type = args.get('item_type')
    plural = {'entity': 'entities', 'relation': 'relations', 'fact': 'facts'}.get(item_type)
    visibility = args.get('visibility') if args.get('visibility') in VALID_VISIBILITIES else 'party_known'
    item_id = clean_id(args.get('item_id'), '')
    if not plural:
        return {'error': 'Invalid item_type.'}
    target = None
    for item in graph.get(plural, []):
        if item.get('id') == item_id:
            target = item
            break
    if not target:
        return {'error': f'{item_type} not found: {item_id}'}
    old_visibility = target.get('visibility')
    target['visibility'] = visibility
    world.knowledge_graph = json_dumps(graph)
    world.updated_at = utcnow()
    upsert_memory_embedding(campaign, item_type, item_id, target)
    event = _record_event(
        campaign,
        'fact_revealed',
        args.get('reason') or f'{item_type} {item_id} visibility changed to {visibility}.',
        {'item_type': item_type, 'item_id': item_id, 'from': old_visibility, 'to': visibility},
        visibility=visibility,
    )
    return {'item': target, 'affected_ids': {'world_id': world.id, 'world_event_ids': [event.id]}}


def _stage_deferred_narrative_action(campaign, session, current_user, name, args, action_buffer):
    """Validate a narrative mutation and return a preview without touching durable state."""
    args = args if isinstance(args, dict) else {}
    action_id = f'pending_action_{len(action_buffer["actions"]) + 1}'
    preview = None
    if name == 'record_world_event':
        summary = clean_text(args.get('summary'), 1200)
        if not summary:
            return {'error': 'A world-event summary is required.'}
        preview = {
            'event': {
                'id': action_id,
                'event_type': clean_id(args.get('event_type'), 'world_event')[:80],
                'summary': summary,
                'visibility': args.get('visibility') if args.get('visibility') in VALID_VISIBILITIES else 'dm_private',
                'pending': True,
            },
        }
    elif name == 'update_current_scene':
        world, _graph, world_state, _private = _world_json(campaign)
        if not world:
            return {'error': 'No world package exists.'}
        scene_patch = args.get('scene_patch') if isinstance(args.get('scene_patch'), dict) else {}
        preview_state = json_loads(json_dumps(world_state), {})
        current_scene = preview_state.get('current_scene', {}) if isinstance(preview_state, dict) else {}
        from services.scene_location_resolver import resolve_scene_location_patch
        loc_patch = resolve_scene_location_patch(scene_patch, campaign, current_scene)
        clean_scene_patch = {key: value for key, value in scene_patch.items() if key not in ('location_id', 'location_name')}
        loc_status = loc_patch.get("status", "unresolved") if isinstance(loc_patch, dict) else "unresolved"
        if loc_status in ("canonical", "direct", "new"):
            clean_scene_patch.update({"location_id": loc_patch["location_id"], "location_name": loc_patch["location_name"]})
        current_scene.update(clean_scene_patch)
        preview_state['current_scene'] = current_scene
        _sync_party_known_location(preview_state, current_scene)
        preview = {'current_scene': current_scene, 'pending': True}
    elif name == 'reveal_fact':
        world, graph, _world_state, _private = _world_json(campaign)
        if not world:
            return {'error': 'No world package exists.'}
        item_type = args.get('item_type')
        plural = {'entity': 'entities', 'relation': 'relations', 'fact': 'facts'}.get(item_type)
        item_id = clean_id(args.get('item_id'), '')
        visibility = args.get('visibility') if args.get('visibility') in VALID_VISIBILITIES else 'party_known'
        target = next((item for item in graph.get(plural, []) if item.get('id') == item_id), None) if plural else None
        if not target:
            return {'error': f'{item_type or "item"} not found: {item_id}'}
        preview = {
            'item': {
                'id': item_id,
                'item_type': item_type,
                'from_visibility': target.get('visibility'),
                'visibility': visibility,
                'pending': True,
            },
        }
    elif name == 'propose_sheet_update':
        proposal = _prepare_sheet_proposal(campaign, current_user, args, session)
        if proposal.get('error'):
            return proposal
        preview = {'proposal': {**proposal, 'proposal_id': action_id, 'pending': True}}
    else:
        return {'error': f'Unsupported deferred narrative tool: {name}'}

    action = {'id': action_id, 'name': name, 'args': dict(args), 'preview': preview}
    action_buffer['actions'].append(action)
    return {**preview, 'pending_action_id': action_id}


def apply_deferred_narrative_action(campaign, session, current_user, action):
    """Apply a previously staged action inside the accepted-turn transaction."""
    name = action.get('name')
    args = action.get('args') if isinstance(action.get('args'), dict) else {}
    if name == 'record_world_event':
        return _tool_record_world_event(campaign, current_user, args), None
    if name == 'update_current_scene':
        return _tool_update_current_scene(campaign, current_user, args), None
    if name == 'reveal_fact':
        return _tool_reveal_fact(campaign, current_user, args), None
    if name == 'propose_sheet_update':
        prepared = _prepare_sheet_proposal(campaign, current_user, args, session)
        if prepared.get('error'):
            return prepared, None
        proposal = SheetProposal(
            session_id=session.id,
            character_id=prepared['character_id'],
            dm_user_id=current_user.id,
            reason=prepared['reason'],
            changes=prepared['changes'],
            status='pending',
        )
        db.session.add(proposal)
        return {
            'proposal_id': None,
            'character_id': proposal.character_id,
            'reason': proposal.reason,
            'changes': proposal.changes,
            'status': proposal.status,
        }, proposal
    return {'error': f'Unsupported deferred narrative tool: {name}'}, None


def _tool_generate_loot_box(campaign, current_user, args, session=None, audit_context=None):
    from models import SessionMessage

    name = clean_text(args.get('name', ''), 200)
    description = clean_text(args.get('description', ''), 1000)
    if not name:
        return {'error': 'A loot box name is required.'}

    try:
        loot_box = do_generate_loot_box(campaign, session, current_user, name, description)
    except Exception as err:
        return {'error': f'Failed to generate loot box: {repr(err)}'}

    if session:
        announcement = (
            f'A loot box has appeared: **{loot_box.name}**. '
            f'The party can inspect and open it from the campaign stash.'
        )
        announcement_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=announcement,
        )
        db.session.add(announcement_msg)
        db.session.flush()

    event = _record_event(
        campaign,
        'loot_box_generated',
        f'Loot box "{loot_box.name}" generated.',
        {'loot_box_id': loot_box.id, 'name': loot_box.name, 'description': loot_box.description},
        visibility='party_known',
    )

    return {
        'loot_box': {
            'id': loot_box.id,
            'name': loot_box.name,
            'description': loot_box.description,
            'status': loot_box.status,
        },
        'affected_ids': {'loot_box_ids': [loot_box.id], 'world_event_ids': [event.id]},
    }


def _tool_create_encounter_map(campaign, current_user, args, session=None, audit_context=None):
    title = clean_text(args.get('title', ''), 200)
    map_prompt = clean_text(args.get('map_prompt', ''), 2000)
    if not title:
        return {'error': 'A map title is required.'}
    if not map_prompt:
        return {'error': 'A map prompt is required.'}

    try:
        encounter_map = do_create_encounter_map(
            campaign,
            session,
            title,
            map_prompt,
            terrain=args.get('terrain', ''),
            tactical_features=args.get('tactical_features', ''),
            mood=args.get('mood', ''),
            vtt_setup_notes=args.get('vtt_setup_notes', ''),
            audit_context=audit_context,
        )
    except Exception as err:
        return {'error': f'Failed to generate encounter map: {repr(err)}'}

    event = _record_event(
        campaign,
        'encounter_map_generated',
        f'Encounter map "{encounter_map.title}" generated.',
        {'encounter_map_id': encounter_map.id, 'title': encounter_map.title},
        visibility='party_known',
    )
    return {
        'encounter_map': encounter_map.to_dict(include_private=True),
        'affected_ids': {'encounter_map_ids': [encounter_map.id], 'world_event_ids': [event.id]},
    }


def _encounter_map_grid_dimensions(encounter_map):
    grid = EncounterMap._json_value(encounter_map.grid_json, {})
    if not isinstance(grid, dict):
        return None, None
    columns = grid.get('columns')
    rows = grid.get('rows')
    return columns if isinstance(columns, int) else None, rows if isinstance(rows, int) else None


def _coerce_speed_feet(value, default=30):
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        match = re.search(r'(\d+)', value)
        if match:
            return max(0, int(match.group(1)))
    return max(0, int(default))


def _movement_budget_feet_for_placement(campaign, placement, turn_combatant=None):
    if isinstance(turn_combatant, dict):
        actions = turn_combatant.get('actions') if isinstance(turn_combatant.get('actions'), dict) else {}
        if 'movement_remaining' in actions:
            return _coerce_speed_feet(actions.get('movement_remaining'), default=0)
        return _coerce_speed_feet(turn_combatant.get('speed'), default=30)

    if placement.actor_type == 'player':
        member = CampaignMember.query.filter_by(
            campaign_id=campaign.id,
            user_id=int(placement.actor_id),
        ).first() if str(placement.actor_id).isdigit() else None
        character = member.selected_character if member and member.selected_character else None
        if not character:
            character = Character.query.filter_by(
                campaign_id=campaign.id,
                user_id=int(placement.actor_id),
            ).first() if str(placement.actor_id).isdigit() else None
        return _coerce_speed_feet(character.speed if character else 30, default=30)

    if placement.actor_type == 'monster':
        monster = CampaignMonster.query.filter_by(
            campaign_id=campaign.id,
            monster_id=placement.actor_id,
        ).first()
        stat_block = json_loads(monster.stat_block, {}) if monster and monster.stat_block else {}
        return _coerce_speed_feet(stat_block.get('speed'), default=30)

    if placement.actor_type == 'npc':
        npc = NPCActor.query.filter_by(
            campaign_id=campaign.id,
            actor_id=placement.actor_id,
        ).first()
        dossier = json_loads(npc.dossier, {}) if npc and npc.dossier else {}
        return _coerce_speed_feet(dossier.get('speed'), default=30)

    return 30


def _resolve_map_player(campaign, actor_id):
    try:
        numeric_id = int(actor_id)
    except (TypeError, ValueError):
        return None, 'Player actor_id must be a user id or selected character id.'

    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=numeric_id).first()
    if not member:
        member = CampaignMember.query.filter_by(campaign_id=campaign.id, selected_character_id=numeric_id).first()
    if not member:
        return None, 'No campaign player exists with that user id or selected character id.'

    character = member.selected_character
    label = character.name if character else (member.user.username if member.user else f'Player {member.user_id}')
    return {'actor_id': str(member.user_id), 'label': label}, None


def _character_for_player_actor(campaign, actor_id):
    try:
        numeric_id = int(actor_id)
    except (TypeError, ValueError):
        return None

    member = CampaignMember.query.filter_by(campaign_id=campaign.id, user_id=numeric_id).first()
    if member and member.selected_character:
        return member.selected_character
    return Character.query.filter_by(campaign_id=campaign.id, user_id=numeric_id).first()


def _player_character_for_placement(campaign, placement):
    if not placement or placement.actor_type != 'player':
        return None
    return _character_for_player_actor(campaign, placement.actor_id)


def _resolve_map_npc(campaign, actor_id):
    actor = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=actor_id).first()
    if not actor and str(actor_id).isdigit():
        actor = NPCActor.query.filter_by(campaign_id=campaign.id, id=int(actor_id)).first()
    if not actor:
        return None, 'No campaign NPC exists with that id.'
    return {'actor_id': actor.actor_id, 'label': actor.name}, None


def _resolve_map_monster(campaign, placement):
    monster_id = clean_id(placement.get('actor_id'), '')
    if not monster_id:
        return None, 'Monster actor_id is required.'
    monster = CampaignMonster.query.filter_by(campaign_id=campaign.id, monster_id=monster_id).first()
    if not monster:
        monster = CampaignMonster(
            campaign_id=campaign.id,
            monster_id=monster_id,
            name=clean_text(placement.get('monster_name') or placement.get('label'), 200) or monster_id.replace('_', ' ').title(),
            stat_block=json_dumps(placement.get('stat_block') if isinstance(placement.get('stat_block'), dict) else {}),
        )
        db.session.add(monster)
        db.session.flush()
    return {'actor_id': monster.monster_id, 'label': monster.name, 'monster_id': monster.id}, None


def _resolve_map_actor(campaign, placement):
    actor_type = clean_text(placement.get('actor_type'), 20).lower()
    actor_id = clean_text(placement.get('actor_id'), 100)
    if actor_type == 'player':
        return _resolve_map_player(campaign, actor_id)
    if actor_type == 'npc':
        return _resolve_map_npc(campaign, actor_id)
    if actor_type == 'monster':
        return _resolve_map_monster(campaign, placement)
    return None, 'actor_type must be player, npc, or monster.'


def _normalize_condition_entries(items):
    normalized = []
    seen = set()
    for raw in items or []:
        if isinstance(raw, str):
            entry = {
                'name': clean_text(raw, 80),
                'source': '',
                'duration': '',
                'note': '',
            }
        elif isinstance(raw, dict):
            entry = {
                'name': clean_text(raw.get('name') or raw.get('condition') or raw.get('condition_name'), 80),
                'source': clean_text(raw.get('source'), 120),
                'duration': clean_text(raw.get('duration') or raw.get('duration_remaining'), 120),
                'note': clean_text(raw.get('note') or raw.get('description'), 240),
            }
        else:
            continue

        if not entry['name']:
            continue
        key = entry['name'].lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(entry)
    return normalized


def _combatant_conditions(combatant):
    conditions = combatant.get('conditions') if isinstance(combatant, dict) else []
    return _normalize_condition_entries(conditions)


def _combatant_action_state(combatant):
    actions = combatant.get('actions') if isinstance(combatant, dict) and isinstance(combatant.get('actions'), dict) else {}
    return {
        'action': bool(actions.get('action', True)),
        'bonus_action': bool(actions.get('bonus_action', True)),
        'reaction': bool(actions.get('reaction', True)),
        'movement_remaining': _coerce_speed_feet(actions.get('movement_remaining'), default=0),
    }


def _combatant_public_summary(combatant):
    if not isinstance(combatant, dict):
        return {}
    return {
        'placement_id': combatant.get('placement_id'),
        'actor_type': combatant.get('actor_type'),
        'actor_id': combatant.get('actor_id'),
        'label': combatant.get('label'),
        'initiative': combatant.get('initiative'),
        'initiative_bonus': combatant.get('initiative_bonus'),
        'max_hp': max(0, int(combatant.get('max_hp') or 0)),
        'current_hp': max(0, int(combatant.get('current_hp') or 0)),
        'temp_hp': max(0, int(combatant.get('temp_hp') or 0)),
        'armor_class': max(0, int(combatant.get('armor_class') or 0)),
        'speed': _coerce_speed_feet(combatant.get('speed'), default=30),
        'conditions': _combatant_conditions(combatant),
        'actions': _combatant_action_state(combatant),
    }


def _combatant_snapshot(encounter_map, combatant):
    snapshot = _combatant_public_summary(combatant)
    placement_id = snapshot.get('placement_id')
    placement = db.session.get(EncounterMapPlacement, placement_id) if placement_id else None
    if not placement and encounter_map and placement_id:
        placement = EncounterMapPlacement.query.filter_by(
            encounter_map_id=encounter_map.id,
            id=placement_id,
        ).first()
    snapshot['placement'] = placement.to_dict() if placement else None
    return snapshot


def _get_encounter_map_for_tool(campaign, encounter_map_id=None):
    encounter_map = db.session.get(EncounterMap, encounter_map_id) if encounter_map_id else latest_encounter_map(campaign.id)
    if not encounter_map or encounter_map.campaign_id != campaign.id:
        return None, None, 'No encounter map is available for this campaign.'
    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    return encounter_map, state if isinstance(state, dict) else {}, None


def _find_combatant(turn_order, placement_id):
    for index, combatant in enumerate(turn_order):
        if isinstance(combatant, dict) and combatant.get('placement_id') == placement_id:
            return combatant, index
    return None, None


def _resolve_combatant_target(campaign, args, require_active=True):
    encounter_map, state, error = _get_encounter_map_for_tool(campaign, args.get('encounter_map_id'))
    if error:
        return None, None, None, None, None, error

    if require_active and not state.get('active'):
        return encounter_map, state, None, None, None, 'Combat is not active.'

    placement = None
    placement_id = args.get('placement_id')
    if placement_id is not None:
        try:
            placement = EncounterMapPlacement.query.filter_by(
                encounter_map_id=encounter_map.id,
                id=int(placement_id),
            ).first()
        except (TypeError, ValueError):
            return encounter_map, state, None, None, None, 'placement_id must be an integer.'
    else:
        actor_type = clean_text(args.get('actor_type'), 20).lower()
        actor_id = args.get('actor_id')
        if not actor_type or actor_id in (None, ''):
            return encounter_map, state, None, None, None, 'Provide placement_id or actor_type plus actor_id.'
        if actor_type == 'monster':
            resolved = {'actor_id': clean_id(actor_id, '')}
            resolve_error = None if resolved['actor_id'] else 'Monster actor_id is required.'
        else:
            resolved, resolve_error = _resolve_map_actor(
                campaign,
                {'actor_type': actor_type, 'actor_id': actor_id},
            )
            if resolve_error:
                return encounter_map, state, None, None, None, resolve_error
        placement = EncounterMapPlacement.query.filter_by(
            encounter_map_id=encounter_map.id,
            actor_type=actor_type,
            actor_id=resolved['actor_id'],
        ).first()

    if not placement:
        return encounter_map, state, None, None, None, 'Encounter actor placement not found on the current map.'

    turn_order = state.get('turn_order', []) if isinstance(state.get('turn_order'), list) else []
    combatant, combatant_index = _find_combatant(turn_order, placement.id) if turn_order else (None, None)
    if require_active and not combatant:
        return encounter_map, state, placement, None, None, 'Combatant not found in turn order.'
    return encounter_map, state, placement, combatant, combatant_index, None


def _sync_player_conditions(character, conditions):
    desired = {item['name'].lower(): item for item in _normalize_condition_entries(conditions)}
    existing = {
        (condition.condition_name or '').lower(): condition
        for condition in CharacterCondition.query.filter_by(character_id=character.id).all()
    }

    for key, condition in existing.items():
        if key not in desired:
            db.session.delete(condition)

    for key, item in desired.items():
        row = existing.get(key)
        if not row:
            row = CharacterCondition(character_id=character.id, condition_name=item['name'])
            db.session.add(row)
        row.condition_name = item['name']
        row.source = item['source'] or None
        row.duration_remaining = item['duration'] or None
        row.description = item['note'] or None


def _sync_combatant_storage(campaign, placement, combatant, sync_conditions=False):
    if not placement or not isinstance(combatant, dict):
        return

    combatant['max_hp'] = max(0, int(combatant.get('max_hp') or 0))
    combatant['current_hp'] = max(0, min(int(combatant.get('current_hp') or 0), combatant['max_hp']))
    combatant['temp_hp'] = max(0, int(combatant.get('temp_hp') or 0))

    if placement.actor_type == 'player':
        character = _player_character_for_placement(campaign, placement)
        if not character:
            return
        character.max_hp = combatant['max_hp']
        character.current_hp = combatant['current_hp']
        character.temp_hp = combatant['temp_hp']
        if sync_conditions:
            _sync_player_conditions(character, combatant.get('conditions') or [])
        return

    if placement.actor_type == 'monster':
        monster = CampaignMonster.query.filter_by(
            campaign_id=campaign.id,
            monster_id=placement.actor_id,
        ).first()
        if not monster:
            return
        stat_block = json_loads(monster.stat_block, {})
        stat_block['max_hp'] = combatant['max_hp']
        stat_block['current_hp'] = combatant['current_hp']
        stat_block['temp_hp'] = combatant['temp_hp']
        if sync_conditions:
            stat_block['conditions'] = _combatant_conditions(combatant)
        monster.stat_block = json.dumps(stat_block)
        return

    if placement.actor_type == 'npc':
        npc = NPCActor.query.filter_by(
            campaign_id=campaign.id,
            actor_id=placement.actor_id,
        ).first()
        if not npc:
            return
        dossier = json_loads(npc.dossier, {})
        dossier['max_hp'] = combatant['max_hp']
        dossier['current_hp'] = combatant['current_hp']
        dossier['temp_hp'] = combatant['temp_hp']
        if sync_conditions:
            dossier['conditions'] = _combatant_conditions(combatant)
        npc.dossier = json.dumps(dossier)


def _persist_encounter_state(encounter_map, state):
    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()


def _dice_term_tokenize(expression):
    text = (expression or '').strip().lower().replace(' ', '')
    if not text:
        return []
    if text[0] not in '+-':
        text = f'+{text}'
    parts = re.findall(r'[+-][^+-]+', text)
    return parts if ''.join(parts) == text else []


def _roll_dice_expression(expression):
    parts = _dice_term_tokenize(expression)
    if not parts:
        raise ValueError('Unsupported dice expression.')

    total = 0
    breakdown = []

    for part in parts:
        sign = -1 if part[0] == '-' else 1
        token = part[1:]
        dice_match = re.fullmatch(r'(\d*)d(\d+)(kh|kl)?(\d+)?', token)
        if dice_match:
            count = int(dice_match.group(1) or 1)
            sides = int(dice_match.group(2))
            keep_mode = dice_match.group(3)
            keep_count = int(dice_match.group(4) or 1)
            if count < 1 or count > 100 or sides < 2 or sides > 1000:
                raise ValueError('Dice expression is out of allowed bounds.')
            rolls = [random.randint(1, sides) for _ in range(count)]
            if keep_mode:
                keep_count = max(1, min(keep_count, count))
                ordered = sorted(rolls, reverse=(keep_mode == 'kh'))
                kept = ordered[:keep_count]
            else:
                kept = list(rolls)
            subtotal = sum(kept) * sign
            total += subtotal
            breakdown.append({
                'kind': 'dice',
                'sign': sign,
                'count': count,
                'sides': sides,
                'rolls': rolls,
                'kept': kept,
                'keep': f'{keep_mode}{keep_count}' if keep_mode else None,
                'subtotal': subtotal,
            })
            continue

        if re.fullmatch(r'\d+', token):
            value = int(token) * sign
            total += value
            breakdown.append({
                'kind': 'constant',
                'sign': sign,
                'value': abs(value),
                'subtotal': value,
            })
            continue

        raise ValueError('Unsupported dice expression.')

    return {
        'expression': expression,
        'total': total,
        'terms': breakdown,
    }


def _point_in_polygon(points, x, y):
    if len(points) < 3:
        return False

    inside = False
    previous = points[-1]
    for current in points:
        current_x = current.get('col')
        current_y = current.get('row')
        previous_x = previous.get('col')
        previous_y = previous.get('row')
        if not all(isinstance(value, (int, float)) for value in (current_x, current_y, previous_x, previous_y)):
            previous = current
            continue
        if (current_y > y) != (previous_y > y):
            x_intersection = (previous_x - current_x) * (y - current_y) / (previous_y - current_y) + current_x
            if x < x_intersection:
                inside = not inside
        previous = current
    return inside


def _rect_contains_cell(rect, grid_col, grid_row):
    rect = rect if isinstance(rect, dict) else {}
    try:
        col = int(rect.get('col'))
        row = int(rect.get('row'))
        width = int(rect.get('width'))
        height = int(rect.get('height'))
    except (TypeError, ValueError):
        return False
    return col <= grid_col < col + width and row <= grid_row < row + height


def _map_area_contains_cell(area, grid_col, grid_row):
    polygon = area.get('polygon') if isinstance(area.get('polygon'), list) else []
    if polygon:
        return _point_in_polygon(polygon, grid_col + 0.5, grid_row + 0.5)
    return _rect_contains_cell(area.get('rect'), grid_col, grid_row)


def _illegal_area_reason(area):
    kind = clean_text(area.get('kind'), 40).lower()
    movement_effect = clean_text(area.get('movement_effect'), 60).lower()
    if kind in {'blocked', 'wall'} or movement_effect == 'blocks_movement':
        return 'blocks movement'
    if kind == 'hazard':
        return 'is hazardous terrain'
    return ''


def _placement_illegal_warnings(encounter_map, placement, index, grid_col, grid_row):
    setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {})
    if not isinstance(setup, dict):
        return []

    label = clean_text(placement.get('label'), 200) or clean_text(placement.get('monster_name'), 200) or clean_text(placement.get('actor_id'), 100)
    warnings = []
    for group in ('terrain_zones', 'obstacles'):
        areas = setup.get(group) if isinstance(setup.get(group), list) else []
        for area in areas:
            if not isinstance(area, dict) or not _map_area_contains_cell(area, grid_col, grid_row):
                continue
            reason = _illegal_area_reason(area)
            if not reason:
                continue
            area_label = clean_text(area.get('label'), 200) or 'Unnamed map area'
            description = clean_text(area.get('description'), 300)
            warning = {
                'index': index,
                'actor_type': clean_text(placement.get('actor_type'), 20).lower(),
                'actor_id': clean_text(placement.get('actor_id'), 100),
                'label': label,
                'col': grid_col,
                'row': grid_row,
                'area_label': area_label,
                'area_type': clean_text(area.get('kind'), 40) or group,
                'reason': f'{area_label} {reason}.',
            }
            if description:
                warning['description'] = description
                warning['reason'] = f'{warning["reason"]} {description}'
            warnings.append(warning)
    return warnings


def _tool_place_encounter_map_actors(campaign, current_user, args, session=None, audit_context=None):
    _ = current_user, session, audit_context
    encounter_map_id = args.get('encounter_map_id')
    encounter_map = db.session.get(EncounterMap, encounter_map_id) if encounter_map_id else latest_encounter_map(campaign.id)
    if not encounter_map or encounter_map.campaign_id != campaign.id:
        return {'error': 'No encounter map is available for this campaign.'}

    placements = args.get('placements') if isinstance(args.get('placements'), list) else []
    if not placements:
        return {'error': 'At least one placement is required.'}

    columns, rows = _encounter_map_grid_dimensions(encounter_map)
    errors = []
    validated_placements = []
    placement_warnings = []
    saved = []
    monster_ids = []

    for index, raw_placement in enumerate(placements):
        placement = raw_placement if isinstance(raw_placement, dict) else {}
        try:
            grid_col = int(placement.get('col'))
            grid_row = int(placement.get('row'))
        except (TypeError, ValueError):
            errors.append({'index': index, 'error': 'Placement requires integer col and row values.'})
            continue

        if grid_col < 0 or grid_row < 0:
            errors.append({'index': index, 'error': 'Placement col and row must be 0 or greater.'})
            continue
        if columns is not None and grid_col >= columns:
            errors.append({'index': index, 'error': f'Placement col must be less than map columns ({columns}).'})
            continue
        if rows is not None and grid_row >= rows:
            errors.append({'index': index, 'error': f'Placement row must be less than map rows ({rows}).'})
            continue

        placement_warnings.extend(_placement_illegal_warnings(encounter_map, placement, index, grid_col, grid_row))
        validated_placements.append((index, placement, grid_col, grid_row))

    if placement_warnings and not args.get('allow_illegal_placements'):
        return {
            'warning': 'One or more placements are on blocked or hazardous map cells. No placements were saved. Review placement_warnings, then choose legal cells or call again with allow_illegal_placements=true if this is intentional.',
            'encounter_map_id': encounter_map.id,
            'placement_warnings': placement_warnings,
            'placement_errors': errors,
        }

    if args.get('clear_existing'):
        EncounterMapPlacement.query.filter_by(encounter_map_id=encounter_map.id).delete(synchronize_session=False)

    for index, placement, grid_col, grid_row in validated_placements:
        actor_type = clean_text(placement.get('actor_type'), 20).lower()
        resolved, error = _resolve_map_actor(campaign, placement)
        if error:
            errors.append({'index': index, 'error': error})
            continue

        if resolved.get('monster_id'):
            monster_ids.append(resolved['monster_id'])

        existing = EncounterMapPlacement.query.filter_by(
            encounter_map_id=encounter_map.id,
            actor_type=actor_type,
            actor_id=resolved['actor_id'],
        ).first()
        row = existing or EncounterMapPlacement(
            encounter_map_id=encounter_map.id,
            actor_type=actor_type,
            actor_id=resolved['actor_id'],
            label=resolved['label'],
        )
        if not existing:
            db.session.add(row)
        row.grid_col = grid_col
        row.grid_row = grid_row
        row.label = clean_text(placement.get('label'), 200) or resolved['label']
        db.session.flush()
        saved.append(row.to_dict())

    if not saved and errors:
        return {'error': 'No placements were saved.', 'placement_errors': errors, 'placement_warnings': placement_warnings}

    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if settings.get('encounter_active') and not state:
        from routes.encounter_maps import build_initial_encounter_state, check_and_start_turns
        state = build_initial_encounter_state(encounter_map, campaign)
        check_and_start_turns(state)
        encounter_map.encounter_state_json = json.dumps(state)

    event = _record_event(
        campaign,
        'encounter_map_actors_placed',
        f'{len(saved)} actor placement{"s" if len(saved) != 1 else ""} updated on "{encounter_map.title}".',
        {'encounter_map_id': encounter_map.id, 'placements': saved, 'errors': errors, 'warnings': placement_warnings},
        visibility='party_known',
    )
    return {
        'encounter_map': encounter_map.to_dict(include_private=True),
        'placements': saved,
        'placement_warnings': placement_warnings,
        'placement_errors': errors,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement['id'] for placement in saved],
            'monster_ids': sorted(set(monster_ids)),
            'world_event_ids': [event.id],
        },
    }


def _tool_move_encounter_actor(campaign, current_user, args):
    _ = current_user
    encounter_map_id = args.get('encounter_map_id')
    encounter_map = db.session.get(EncounterMap, encounter_map_id) if encounter_map_id else latest_encounter_map(campaign.id)
    if not encounter_map or encounter_map.campaign_id != campaign.id:
        return {'error': 'No encounter map is available for this campaign.'}

    try:
        grid_col = int(args.get('col'))
        grid_row = int(args.get('row'))
    except (TypeError, ValueError):
        return {'error': 'Actor move requires integer col and row values.'}

    columns, rows = _encounter_map_grid_dimensions(encounter_map)
    if grid_col < 0 or grid_row < 0:
        return {'error': 'Actor move col and row must be 0 or greater.'}
    if columns is not None and grid_col >= columns:
        return {'error': f'Actor move col must be less than map columns ({columns}).'}
    if rows is not None and grid_row >= rows:
        return {'error': f'Actor move row must be less than map rows ({rows}).'}

    placement = None
    placement_id = args.get('placement_id')
    if placement_id is not None:
        try:
            placement = EncounterMapPlacement.query.filter_by(
                encounter_map_id=encounter_map.id,
                id=int(placement_id),
            ).first()
        except (TypeError, ValueError):
            return {'error': 'placement_id must be an integer.'}
    else:
        actor_lookup = {
            'actor_type': args.get('actor_type'),
            'actor_id': args.get('actor_id'),
        }
        resolved_actor, error = _resolve_map_actor(campaign, actor_lookup)
        if error:
            return {'error': error}
        placement = EncounterMapPlacement.query.filter_by(
            encounter_map_id=encounter_map.id,
            actor_type=clean_text(args.get('actor_type'), 20).lower(),
            actor_id=resolved_actor['actor_id'],
        ).first()

    if not placement:
        return {'error': 'Encounter actor placement not found on the current map.'}

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    is_encounter_active = state.get('active', False) if isinstance(state, dict) else False
    turn_order = state.get('turn_order', []) if isinstance(state, dict) else []
    target_combatant = next(
        (
            combatant for combatant in turn_order
            if isinstance(combatant, dict) and combatant.get('placement_id') == placement.id
        ),
        None,
    )

    if is_encounter_active and not args.get('ignore_turn_order'):
        active_turn_index = state.get('active_turn_index')
        if active_turn_index is None:
            return {'error': 'Combat is active, but initiative rolling is not yet complete.'}
        if not isinstance(active_turn_index, int) or active_turn_index < 0 or active_turn_index >= len(turn_order):
            return {'error': 'Invalid combat turn state.'}
        active_combatant = turn_order[active_turn_index]
        if active_combatant.get('placement_id') != placement.id:
            return {'error': f"It is not {placement.label}'s turn. It is currently {active_combatant.get('label')}'s turn."}

    available_speed = _movement_budget_feet_for_placement(campaign, placement, turn_combatant=target_combatant if is_encounter_active else None)
    max_squares = available_speed // 5
    setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {})
    if isinstance(columns, int) and isinstance(rows, int):
        reachable = reachable_cells(
            setup,
            columns,
            rows,
            placement.grid_col,
            placement.grid_row,
            max_squares,
        )
        movement_cost = reachable.get((grid_col, grid_row))
    else:
        reachable = {}
        movement_cost = max(abs(grid_col - placement.grid_col), abs(grid_row - placement.grid_row))

    if movement_cost is None:
        return {
            'error': 'That destination is not reachable with the actor\'s current movement and the map terrain.',
            'movement': {
                'speed': available_speed,
                'max_squares': max_squares,
                'attempted_squares': max(abs(grid_col - placement.grid_col), abs(grid_row - placement.grid_row)),
                'reachable_squares': len(reachable),
            },
        }

    if movement_cost == 0:
        return {
            'encounter_map': encounter_map.to_dict(include_private=True),
            'placement': placement.to_dict(),
            'movement': {
                'speed': available_speed,
                'max_squares': max_squares,
                'moved_squares': 0,
                'from': {'col': placement.grid_col, 'row': placement.grid_row},
                'to': {'col': placement.grid_col, 'row': placement.grid_row},
            },
            'affected_ids': {
                'encounter_map_ids': [encounter_map.id],
                'placement_ids': [placement.id],
            },
        }

    from_col = placement.grid_col
    from_row = placement.grid_row
    placement.grid_col = grid_col
    placement.grid_row = grid_row

    if is_encounter_active and target_combatant:
        target_combatant.setdefault('actions', {})
        target_combatant['actions']['movement_remaining'] = max(0, available_speed - (movement_cost * 5))
        encounter_map.encounter_state_json = json.dumps(state)

    event = _record_event(
        campaign,
        'encounter_actor_moved',
        f'{placement.label} moved on "{encounter_map.title}".',
        {
            'encounter_map_id': encounter_map.id,
            'placement_id': placement.id,
            'actor_type': placement.actor_type,
            'actor_id': placement.actor_id,
            'label': placement.label,
            'from': {'col': from_col, 'row': from_row},
            'to': {'col': grid_col, 'row': grid_row},
            'movement_cost': movement_cost,
            'movement_remaining': (
                target_combatant.get('actions', {}).get('movement_remaining')
                if isinstance(target_combatant, dict)
                else None
            ),
        },
        visibility='party_known',
    )
    db.session.commit()

    return {
        'encounter_map': encounter_map.to_dict(include_private=True),
        'placement': placement.to_dict(),
        'movement': {
            'speed': available_speed,
            'max_squares': max_squares,
            'moved_squares': movement_cost,
            'from': {'col': from_col, 'row': from_row},
            'to': {'col': grid_col, 'row': grid_row},
            'movement_remaining': (
                target_combatant.get('actions', {}).get('movement_remaining')
                if isinstance(target_combatant, dict)
                else None
            ),
        },
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
            'world_event_ids': [event.id],
        },
    }


def _tool_toggle_encounter_mode(campaign, current_user, args):
    _ = current_user
    active = bool(args.get('active', False))

    try:
        settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else (campaign.settings or {})
    except (TypeError, ValueError):
        settings = {}
    settings['encounter_active'] = active
    campaign.settings = json.dumps(settings)

    encounter_map = latest_encounter_map(campaign.id)
    encounter_state = {}
    if encounter_map:
        from routes.encounter_maps import build_initial_encounter_state, check_and_start_turns
        if active:
            encounter_state = build_initial_encounter_state(encounter_map, campaign)
            check_and_start_turns(encounter_state)
            encounter_map.encounter_state_json = json.dumps(encounter_state)
        else:
            state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
            if state:
                state['active'] = False
                state['active_turn_index'] = None
                encounter_map.encounter_state_json = json.dumps(state)
            else:
                encounter_map.encounter_state_json = None
            encounter_state = state or {}

            # Archive the map when combat is ended
            encounter_map.is_archived = True

    db.session.commit()

    message = f"Encounter mode {'started' if active else 'stopped'}."
    if active:
        message += (
            " You MUST now run the create_encounter_map tool to generate a brand new map "
            "for this combat encounter, and then run the place_encounter_map_actors tool "
            "to place all players, NPCs, and monsters onto the newly generated map. Do not re-use old maps. "
            "Use this exact create_encounter_map argument shape: "
            '{"title":"Short player-visible map title","map_prompt":"Concrete top-down battle map layout and important zones",'
            '"terrain":"Optional terrain/environment notes","tactical_features":"Optional cover, choke points, elevation, hazards, doors, obstacles, objectives",'
            '"vtt_setup_notes":"Optional setup notes for friendly starts, enemy starts, obstacles, objectives, and terrain calls",'
            '"mood":"Optional mood and lighting notes"}. '
            "Do not use name, description, width, height, grid_size, environment_type, lighting_conditions, or terrain_features. "
            "After the map is created, use place_encounter_map_actors with this argument shape: "
            '{"encounter_map_id":123,"clear_existing":true,"placements":[{"actor_type":"player|npc|monster","actor_id":"user id, selected character id, NPC actor_id, or monster_id","col":0,"row":0,"label":"Optional label","monster_name":"Optional monster name","stat_block":{}}]}.'
        )

    return {
        'message': message,
        'encounter_state': encounter_state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id] if encounter_map else [],
        }
    }


def _tool_next_combat_turn(campaign, current_user, args):
    _ = current_user, args
    encounter_map = latest_encounter_map(campaign.id)
    if not encounter_map:
        return {'error': 'No encounter map is active for this campaign.'}

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if not state or not state.get('active'):
        return {'error': 'Combat is not active.'}

    active_turn_index = state.get('active_turn_index')
    if active_turn_index is None:
        return {'error': 'Initiative is not fully rolled yet.'}

    turn_order = state.get('turn_order', [])
    if not turn_order:
        return {'error': 'Turn order is empty.'}

    next_index = (active_turn_index + 1) % len(turn_order)
    if next_index == 0:
        state['round'] = state.get('round', 1) + 1

    state['active_turn_index'] = next_index

    next_combatant = turn_order[next_index]
    speed = next_combatant.get('speed', 30)
    next_combatant['actions'] = {
        'action': True,
        'bonus_action': True,
        'reaction': True,
        'movement_remaining': speed
    }

    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()

    return {
        'message': f"Turn advanced to {next_combatant.get('label')}.",
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
        }
    }


def _tool_set_combat_turn(campaign, current_user, args):
    _ = current_user
    try:
        new_index = int(args.get('active_turn_index'))
    except (TypeError, ValueError):
        return {'error': 'active_turn_index must be an integer.'}

    encounter_map = latest_encounter_map(campaign.id)
    if not encounter_map:
        return {'error': 'No encounter map is active for this campaign.'}

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if not state or not state.get('active'):
        return {'error': 'Combat is not active.'}

    turn_order = state.get('turn_order', [])
    if not turn_order:
        return {'error': 'Turn order is empty.'}

    if new_index < 0 or new_index >= len(turn_order):
        return {'error': f'Turn index must be between 0 and {len(turn_order) - 1}.'}

    state['active_turn_index'] = new_index

    new_combatant = turn_order[new_index]
    speed = new_combatant.get('speed', 30)
    new_combatant['actions'] = {
        'action': True,
        'bonus_action': True,
        'reaction': True,
        'movement_remaining': speed
    }

    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()

    return {
        'message': f"Turn set to {new_combatant.get('label')}.",
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
        }
    }


def _tool_update_combatant_actions(campaign, current_user, args):
    _ = current_user
    actor_type = args.get('actor_type')
    actor_id = args.get('actor_id')
    placement_id = args.get('placement_id')
    actions_updates = args.get('actions', {})

    encounter_map = latest_encounter_map(campaign.id)
    if not encounter_map:
        return {'error': 'No encounter map is active for this campaign.'}

    state = EncounterMap._json_value(encounter_map.encounter_state_json, {})
    if not state or not state.get('active'):
        return {'error': 'Combat is not active.'}

    turn_order = state.get('turn_order', [])
    target = None
    for combatant in turn_order:
        if placement_id is not None and combatant.get('placement_id') == placement_id:
            target = combatant
            break
        if actor_type is not None and actor_id is not None:
            if combatant.get('actor_type') == actor_type and str(combatant.get('actor_id')) == str(actor_id):
                target = combatant
                break

    if not target:
        return {'error': 'Combatant not found in turn order.'}

    if 'actions' not in target:
        target['actions'] = {}

    for key in ('action', 'bonus_action', 'reaction'):
        if key in actions_updates:
            target['actions'][key] = bool(actions_updates[key])
    if 'movement_remaining' in actions_updates:
        try:
            target['actions']['movement_remaining'] = max(0, int(actions_updates['movement_remaining']))
        except (TypeError, ValueError):
            pass

    encounter_map.encounter_state_json = json.dumps(state)
    db.session.commit()

    return {
        'message': f"Actions updated for {target.get('label')}.",
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
        }
    }


def _tool_roll_dice(campaign, current_user, args):
    _ = campaign, current_user
    expression = clean_text(args.get('expression') or args.get('dice'), 120)
    if not expression:
        return {'error': 'expression is required.'}

    try:
        result = _roll_dice_expression(expression)
    except ValueError as err:
        return {'error': str(err)}

    reason = clean_text(args.get('reason'), 240)
    return {
        'expression': expression,
        'reason': reason,
        'total': result['total'],
        'terms': result['terms'],
    }


def _tool_get_encounter_overview(campaign, current_user, args):
    _ = current_user
    encounter_map, state, error = _get_encounter_map_for_tool(campaign, args.get('encounter_map_id'))
    if error:
        return {'error': error}
    if not state.get('active'):
        return {'error': 'Combat is not active.'}

    turn_order = state.get('turn_order', []) if isinstance(state.get('turn_order'), list) else []
    active_turn_index = state.get('active_turn_index')
    active_combatant = None
    if isinstance(active_turn_index, int) and 0 <= active_turn_index < len(turn_order):
        active_combatant = turn_order[active_turn_index]

    return {
        'encounter_map_id': encounter_map.id,
        'title': encounter_map.title,
        'round': int(state.get('round') or 1),
        'active_turn_index': active_turn_index,
        'active_combatant': _combatant_snapshot(encounter_map, active_combatant) if active_combatant else None,
        'turn_order': [_combatant_snapshot(encounter_map, combatant) for combatant in turn_order],
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [combatant.get('placement_id') for combatant in turn_order if isinstance(combatant, dict)],
        },
    }


def _tool_get_combatant_state(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    turn_order = state.get('turn_order', []) if isinstance(state.get('turn_order'), list) else []
    active_turn_index = state.get('active_turn_index')
    active_combatant = None
    if isinstance(active_turn_index, int) and 0 <= active_turn_index < len(turn_order):
        active_combatant = turn_order[active_turn_index]

    return {
        'encounter_map_id': encounter_map.id,
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'combatant_index': combatant_index,
        'is_active_turn': bool(active_combatant and active_combatant.get('placement_id') == placement.id),
        'active_turn_index': active_turn_index,
        'active_combatant': _combatant_snapshot(encounter_map, active_combatant) if active_combatant else None,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_list_reachable_positions(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=False,
    )
    if error:
        return {'error': error}

    columns, rows = _encounter_map_grid_dimensions(encounter_map)
    if columns is None or rows is None:
        return {'error': 'Encounter map grid dimensions are not available.'}

    available_speed = _movement_budget_feet_for_placement(campaign, placement, turn_combatant=combatant)
    max_squares = available_speed // 5
    setup = EncounterMap._json_value(encounter_map.vtt_setup_json, {})
    reachable = reachable_cells(
        setup,
        columns,
        rows,
        placement.grid_col,
        placement.grid_row,
        max_squares,
    )
    max_cells = max(1, min(int(args.get('max_cells') or 250), 500))
    cells = [
        {'col': col, 'row': row, 'cost': cost}
        for (col, row), cost in sorted(reachable.items(), key=lambda item: (item[1], item[0][1], item[0][0]))[:max_cells]
    ]

    return {
        'encounter_map_id': encounter_map.id,
        'placement_id': placement.id,
        'origin': {'col': placement.grid_col, 'row': placement.grid_row},
        'movement': {
            'speed': available_speed,
            'max_squares': max_squares,
            'reachable_count': len(reachable),
        },
        'reachable_positions': cells,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_set_combatant_hp(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    updates = 0
    for field in ('max_hp', 'current_hp', 'temp_hp'):
        if field in args and args.get(field) is not None:
            try:
                combatant[field] = max(0, int(args.get(field)))
            except (TypeError, ValueError):
                return {'error': f'{field} must be an integer.'}
            updates += 1

    if updates == 0:
        return {'error': 'Provide at least one of max_hp, current_hp, or temp_hp.'}

    if combatant.get('max_hp') is not None:
        combatant['current_hp'] = max(0, min(int(combatant.get('current_hp') or 0), int(combatant.get('max_hp') or 0)))
    _sync_combatant_storage(campaign, placement, combatant)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'HP updated for {combatant.get("label")}.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_apply_damage(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    try:
        amount = int(args.get('amount'))
    except (TypeError, ValueError):
        return {'error': 'amount must be an integer.'}
    if amount < 0:
        return {'error': 'amount must be 0 or greater.'}

    ignore_temp_hp = bool(args.get('ignore_temp_hp'))
    current_hp = max(0, int(combatant.get('current_hp') or 0))
    temp_hp = max(0, int(combatant.get('temp_hp') or 0))
    absorbed = 0
    if not ignore_temp_hp and temp_hp:
        absorbed = min(temp_hp, amount)
        temp_hp -= absorbed
    hp_damage = max(0, amount - absorbed)
    combatant['temp_hp'] = temp_hp
    combatant['current_hp'] = max(0, current_hp - hp_damage)

    _sync_combatant_storage(campaign, placement, combatant)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'{combatant.get("label")} took {amount} damage.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'damage': {
            'requested': amount,
            'damage_type': clean_text(args.get('damage_type'), 80),
            'absorbed_by_temp_hp': absorbed,
            'applied_to_current_hp': hp_damage,
        },
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_apply_healing(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    try:
        amount = int(args.get('amount'))
    except (TypeError, ValueError):
        return {'error': 'amount must be an integer.'}
    if amount < 0:
        return {'error': 'amount must be 0 or greater.'}

    current_hp = max(0, int(combatant.get('current_hp') or 0))
    max_hp = max(0, int(combatant.get('max_hp') or 0))
    healed = min(amount, max(0, max_hp - current_hp))
    combatant['current_hp'] = current_hp + healed

    _sync_combatant_storage(campaign, placement, combatant)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'{combatant.get("label")} healed {healed} HP.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'healing': {
            'requested': amount,
            'applied': healed,
        },
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_grant_temp_hp(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    try:
        amount = int(args.get('amount'))
    except (TypeError, ValueError):
        return {'error': 'amount must be an integer.'}
    if amount < 0:
        return {'error': 'amount must be 0 or greater.'}

    mode = clean_text(args.get('mode') or 'max', 20).lower() or 'max'
    current_temp = max(0, int(combatant.get('temp_hp') or 0))
    if mode == 'replace':
        next_temp = amount
    elif mode == 'add':
        next_temp = current_temp + amount
    elif mode == 'max':
        next_temp = max(current_temp, amount)
    else:
        return {'error': 'mode must be one of max, replace, or add.'}
    combatant['temp_hp'] = next_temp

    _sync_combatant_storage(campaign, placement, combatant)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'Temp HP updated for {combatant.get("label")}.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'temp_hp': {
            'previous': current_temp,
            'requested': amount,
            'mode': mode,
            'current': next_temp,
        },
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_set_combatant_initiative(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    try:
        initiative = int(args.get('initiative'))
    except (TypeError, ValueError):
        return {'error': 'initiative must be an integer.'}

    combatant['initiative'] = initiative
    if 'initiative_bonus' in args and args.get('initiative_bonus') is not None:
        try:
            combatant['initiative_bonus'] = int(args.get('initiative_bonus'))
        except (TypeError, ValueError):
            return {'error': 'initiative_bonus must be an integer.'}

    turn_order = state.get('turn_order', [])
    active_id = turn_order[state.get('active_turn_index')].get('placement_id') if (
        isinstance(state.get('active_turn_index'), int)
        and 0 <= state.get('active_turn_index') < len(turn_order)
    ) else None
    turn_order.sort(key=lambda item: item.get('placement_id', 0))
    turn_order.sort(key=lambda item: item.get('initiative_bonus', 0), reverse=True)
    turn_order.sort(key=lambda item: item.get('initiative', 0), reverse=True)
    if active_id is not None:
        for index, item in enumerate(turn_order):
            if item.get('placement_id') == active_id:
                state['active_turn_index'] = index
                break
    elif combatant_index is not None:
        state['active_turn_index'] = combatant_index

    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'Initiative updated for {combatant.get("label")}.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_update_combatant_conditions(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, _combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=True,
    )
    if error:
        return {'error': error}

    mode = clean_text(args.get('mode') or 'set', 20).lower() or 'set'
    conditions_payload = args.get('conditions')
    if not isinstance(conditions_payload, list):
        return {'error': 'conditions must be an array.'}

    current = {item['name'].lower(): item for item in _combatant_conditions(combatant)}
    incoming = {item['name'].lower(): item for item in _normalize_condition_entries(conditions_payload)}

    if mode == 'set':
        next_conditions = list(incoming.values())
    elif mode == 'add':
        merged = dict(current)
        merged.update(incoming)
        next_conditions = list(merged.values())
    elif mode == 'remove':
        next_conditions = [item for key, item in current.items() if key not in incoming]
    else:
        return {'error': 'mode must be one of set, add, or remove.'}

    combatant['conditions'] = _normalize_condition_entries(next_conditions)
    _sync_combatant_storage(campaign, placement, combatant, sync_conditions=True)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'Conditions updated for {combatant.get("label")}.',
        'combatant': _combatant_snapshot(encounter_map, combatant),
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _tool_remove_encounter_actor(campaign, current_user, args):
    _ = current_user
    encounter_map, state, placement, combatant, combatant_index, error = _resolve_combatant_target(
        campaign,
        args,
        require_active=False,
    )
    if error:
        return {'error': error}

    active_turn_index = state.get('active_turn_index')
    turn_order = state.get('turn_order', []) if isinstance(state.get('turn_order'), list) else []
    if combatant_index is not None:
        turn_order.pop(combatant_index)
        if not turn_order:
            state['active_turn_index'] = None
            state['active'] = False
        elif isinstance(active_turn_index, int):
            if combatant_index < active_turn_index:
                state['active_turn_index'] = active_turn_index - 1
            elif combatant_index == active_turn_index:
                if active_turn_index >= len(turn_order):
                    state['active_turn_index'] = 0
                next_combatant = turn_order[state['active_turn_index']]
                speed = next_combatant.get('speed', 30)
                next_combatant['actions'] = {
                    'action': True,
                    'bonus_action': True,
                    'reaction': True,
                    'movement_remaining': speed,
                }

    db.session.delete(placement)
    _persist_encounter_state(encounter_map, state)

    return {
        'message': f'{placement.label} removed from the encounter map.',
        'removed': {
            'placement_id': placement.id,
            'actor_type': placement.actor_type,
            'actor_id': placement.actor_id,
            'label': placement.label,
        },
        'encounter_state': state,
        'affected_ids': {
            'encounter_map_ids': [encounter_map.id],
            'placement_ids': [placement.id],
        },
    }


def _current_scene_for_shop(campaign):
    _world, _graph, world_state, _private = _world_json(campaign)
    current_scene = world_state.get('current_scene', {}) if isinstance(world_state, dict) else {}
    return current_scene if isinstance(current_scene, dict) else {}


def _tool_create_shop_menu(campaign, current_user, args, session=None, audit_context=None):
    from models import SessionMessage
    _ = current_user
    _ = audit_context

    current_scene = _current_scene_for_shop(campaign)
    location_name = clean_text(current_scene.get('location_name', ''), 200) or None

    name = clean_text(args.get('name', ''), 200)
    description = clean_text(args.get('description', ''), 1000)
    if not name:
        return {'error': 'A shop name is required.'}

    cleaned_items = clean_shop_items(args.get('items', []))
    shop = upsert_shop(campaign, current_scene, {'name': name, 'description': description}, cleaned_items)
    action_text = f'Shop "{name}" upserted.'

    if session:
        location_text = f' in {location_name}' if location_name else ''
        announcement = (
            f'A local merchant is available{location_text}: **{shop.name}**. '
            f'The party can browse and purchase items while they remain here.'
        )
        announcement_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=announcement,
        )
        db.session.add(announcement_msg)
        db.session.flush()

    event = _record_event(
        campaign,
        'shop_menu_created',
        action_text,
        {
            'shop_id': shop.id,
            'name': shop.name,
            'description': shop.description,
            'location_id': shop.location_id,
            'location_name': shop.location_name,
        },
        visibility='party_known',
    )

    return {
        'shop': shop.to_dict(),
        'affected_ids': {'shop_ids': [shop.id], 'world_event_ids': [event.id]}
    }


def _tool_create_shop_list(campaign, current_user, args, session=None, audit_context=None):
    from models import SessionMessage
    _ = current_user

    shop_requests = args.get('shops', [])
    if not isinstance(shop_requests, list) or not shop_requests:
        return {'error': 'At least one shop is required.'}

    current_scene = _current_scene_for_shop(campaign)
    location_name = clean_text(current_scene.get('location_name', ''), 200) or None
    shops = generate_scene_shops(
        campaign,
        current_scene,
        shop_requests[:12],
        audit_context=audit_context,
    )
    if not shops:
        return {'error': 'No valid shops were provided.'}

    if session:
        location_text = f' in {location_name}' if location_name else ''
        shop_names = ', '.join(f'**{shop.name}**' for shop in shops[:6])
        if len(shops) > 6:
            shop_names = f'{shop_names}, and {len(shops) - 6} more'
        announcement = (
            f'Local merchants are available{location_text}: {shop_names}. '
            f'The party can browse and purchase items while they remain here.'
        )
        announcement_msg = SessionMessage(
            session_id=session.id,
            role='dm',
            content=announcement,
        )
        db.session.add(announcement_msg)
        db.session.flush()

    event = _record_event(
        campaign,
        'shop_list_created',
        f'{len(shops)} local shops prepared for the current scene.',
        {
            'shop_ids': [shop.id for shop in shops],
            'names': [shop.name for shop in shops],
            'location_id': clean_text(current_scene.get('location_id', ''), 160) or None,
            'location_name': location_name,
            'scene_note': clean_text(args.get('scene_note', ''), 500),
        },
        visibility='party_known',
    )

    return {
        'shops': [shop.to_dict() for shop in shops],
        'affected_ids': {
            'shop_ids': [shop.id for shop in shops],
            'world_event_ids': [event.id],
        }
    }


TOOL_HANDLERS = {
    'ask_character_sheet': _tool_ask_character_sheet,
    'get_current_scene': _tool_get_current_scene,
    'search_campaign_memory': _tool_search_campaign_memory,
    'record_world_event': _tool_record_world_event,
    'update_current_scene': _tool_update_current_scene,
    'reveal_fact': _tool_reveal_fact,
    'propose_sheet_update': _tool_propose_sheet_update,
    'generate_loot_box': _tool_generate_loot_box,
    'roll_dice': _tool_roll_dice,
    'create_encounter_map': _tool_create_encounter_map,
    'place_encounter_map_actors': _tool_place_encounter_map_actors,
    'move_encounter_actor': _tool_move_encounter_actor,
    'get_encounter_overview': _tool_get_encounter_overview,
    'get_combatant_state': _tool_get_combatant_state,
    'list_reachable_positions': _tool_list_reachable_positions,
    'toggle_encounter_mode': _tool_toggle_encounter_mode,
    'next_combat_turn': _tool_next_combat_turn,
    'set_combat_turn': _tool_set_combat_turn,
    'update_combatant_actions': _tool_update_combatant_actions,
    'set_combatant_hp': _tool_set_combatant_hp,
    'apply_damage': _tool_apply_damage,
    'apply_healing': _tool_apply_healing,
    'grant_temp_hp': _tool_grant_temp_hp,
    'set_combatant_initiative': _tool_set_combatant_initiative,
    'update_combatant_conditions': _tool_update_combatant_conditions,
    'remove_encounter_actor': _tool_remove_encounter_actor,
    'create_shop_list': _tool_create_shop_list,
    'create_shop_menu': _tool_create_shop_menu,
}


def execute_dm_tool(campaign, session, current_user, name, args, audit_context=None):
    audit_context = audit_context or {}
    args = args if isinstance(args, dict) else {}
    action_buffer = audit_context.get('pending_action_buffer')
    if isinstance(action_buffer, dict) and name in DEFERRED_NARRATIVE_TOOL_NAMES:
        return _stage_deferred_narrative_action(campaign, session, current_user, name, args, action_buffer)
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        result = {'error': f'Unknown DM tool: {name}'}
        mutated = False
    else:
        before_new = len(db.session.new)
        result = (
            handler(campaign, current_user, args, session, audit_context)
            if name in {'propose_sheet_update', 'generate_loot_box', 'create_encounter_map', 'place_encounter_map_actors', 'create_shop_list', 'create_shop_menu'}
            else handler(campaign, current_user, args, audit_context)
            if name in {'ask_character_sheet', 'search_campaign_memory'}
            else handler(campaign, current_user, args)
        )
        mutated = name in {
            'record_world_event',
            'update_current_scene',
            'reveal_fact',
            'generate_loot_box',
            'create_encounter_map',
            'place_encounter_map_actors',
            'move_encounter_actor',
            'toggle_encounter_mode',
            'next_combat_turn',
            'set_combat_turn',
            'update_combatant_actions',
            'set_combatant_hp',
            'apply_damage',
            'apply_healing',
            'grant_temp_hp',
            'set_combatant_initiative',
            'update_combatant_conditions',
            'remove_encounter_actor',
            'create_shop_list',
            'create_shop_menu',
        }
        mutated = mutated or len(db.session.new) > before_new
    log_audit_event(
        campaign.id,
        'dm_tool_execution',
        f'DM tool executed: {name}',
        {
            'session_id': session.id if session else None,
            'tool_name': name,
            'arguments': args,
            'result': result,
            'mutated': mutated,
            'affected_ids': result.get('affected_ids') if isinstance(result, dict) else {},
        },
        source='dm_tools',
        actor='session_dm',
        trace_id=audit_context.get('trace_id'),
        parent_trace_id=audit_context.get('parent_trace_id'),
        trace_label=audit_context.get('trace_label'),
        audit_role='tools',
        commit=False,
    )
    return result


def _entity_type_key(item):
    if not isinstance(item, dict):
        return ''
    return clean_id(item.get('type'), '')


def _entity_types_compatible(left, right):
    left_type = _entity_type_key(left)
    right_type = _entity_type_key(right)
    return not left_type or not right_type or left_type == 'other' or right_type == 'other' or left_type == right_type


def _create_clock_from_patch(campaign, patch):
    clock_id = clean_id(patch.get('id') or patch.get('clock_id'), f'clock_{utcnow().strftime("%Y%m%d%H%M%S")}')
    existing = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    try:
        segments = min(max(int(patch.get('segments') or 4), 2), 12)
    except (TypeError, ValueError):
        segments = 4
    try:
        filled = min(max(int(patch.get('filled') or 0), 0), segments)
    except (TypeError, ValueError):
        filled = 0
    clock = existing or CampaignClock(campaign_id=campaign.id, clock_id=clock_id)
    clock.name = clean_text(patch.get('name'), 200) or clock_id.replace('_', ' ').title()
    clock.segments = segments
    clock.filled = filled
    clock.pressure_type = clean_text(patch.get('pressure_type'), 80) or 'story'
    clock.visibility = patch.get('visibility') if patch.get('visibility') in VALID_VISIBILITIES else 'dm_private'
    clock.summary = clean_text(patch.get('summary'), 420)
    clock.trigger = clean_text(patch.get('trigger'), 420)
    clock.on_complete = clean_text(patch.get('on_complete'), 520)
    if 'completion_criteria' in patch:
        clock.completion_criteria = _normalize_clock_completion_criteria(patch.get('completion_criteria'))
        clock.completion_state = {}
    elif not existing:
        clock.completion_criteria = []
        clock.completion_state = {}
    clock.status = clean_text(patch.get('status'), 30) or 'active'
    if clock.filled >= clock.segments and clock.completion_criteria:
        clock.status = 'completion_pending'
    clock.updated_at = utcnow()
    if not existing:
        db.session.add(clock)
    db.session.flush()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    from openrouter import _get_cached_build_sha
    provenance = _applied_clock_provenance(patch.get('provenance'), _get_cached_build_sha())
    event = _record_event(
        campaign,
        'clock_created' if not existing else 'clock_updated',
        patch.get('reason') or f'Clock {clock.name} {"created" if not existing else "updated"}.',
        {
            'clock': clock.to_dict(include_private=True),
            'provenance': provenance,
        },
        visibility=clock.visibility,
    )
    return {
        'clock': clock.to_dict(include_private=True),
        'event_id': event.id,
        'action': 'created' if not existing else 'updated',
        'provenance': provenance,
    }


_CLOCK_ALLOWED_EVIDENCE_SOURCE_TYPES = frozenset({'transcript_message', 'world_event'})


def _resolve_clock_transcript_message(campaign, source_id):
    """Resolve a transcript evidence source id to a real visible session message."""
    if source_id in (None, ''):
        return None
    try:
        message_id = int(source_id)
    except (TypeError, ValueError):
        return None
    message = SessionMessage.query.filter_by(id=message_id).first()
    if not message:
        return None
    session = CampaignSession.query.filter_by(id=message.session_id, campaign_id=campaign.id).first()
    if not session:
        return None
    return message


def _resolve_clock_world_event(campaign, source_id):
    event_id = _coerce_patch_int(source_id, default=None, minimum=1)
    if event_id is None:
        return None
    return WorldEvent.query.filter_by(campaign_id=campaign.id, id=event_id).first()


def _clock_visibility_allows(source_visibility, required_visibility):
    source_visibility = source_visibility if source_visibility in VALID_VISIBILITIES else 'dm_private'
    required_visibility = required_visibility if required_visibility in VALID_VISIBILITIES else 'dm_private'
    if required_visibility == 'public':
        return source_visibility == 'public'
    if required_visibility == 'party_known':
        return source_visibility in {'party_known', 'public'}
    return True


def _clock_evidence_key(source):
    if not isinstance(source, dict):
        return None
    source_type = clean_text(source.get('source_type'), 50).lower()
    source_id = source.get('source_id')
    if source_type not in _CLOCK_ALLOWED_EVIDENCE_SOURCE_TYPES or source_id in (None, ''):
        return None
    return source_type, str(source_id)


def _clock_trigger_clauses(clock):
    """Expose stable clause IDs without trying to parse narrative trigger prose."""
    trigger = clean_text(clock.trigger, 420)
    clauses = [{
        'clause_id': 'declared_trigger' if trigger else 'visible_narrative_progress',
        'description': trigger or 'Visible new progress or escalation matching this clock pressure.',
        'allowed_deltas': [1, 2],
    }]
    clauses.append({
        'clause_id': 'visible_prevention_or_relief',
        'description': 'Visible new prevention, relief, or setback that reverses this clock pressure.',
        'allowed_deltas': [-1, -2],
    })
    return clauses


def _normalize_clock_trigger_verdict(value):
    if not isinstance(value, dict):
        return None
    return {
        'clause_id': clean_id(value.get('clause_id'), ''),
        'verdict': clean_text(value.get('verdict'), 30).lower(),
        'supported_claims': _normalize_clock_supported_claims(value.get('supported_claims')),
        'evidence_sources': value.get('evidence_sources') if isinstance(value.get('evidence_sources'), list) else [],
        'chronology_verdict': clean_text(value.get('chronology_verdict'), 40).lower(),
        'reason': clean_text(value.get('reason'), 520),
    }


def _verified_clock_evidence_sources(
    campaign,
    evidence_sources,
    required_visibility,
    allowed_evidence_keys,
):
    """Resolve source references without attempting to interpret their prose.

    Semantic support is the clock adjudicator's job. The durable boundary only checks
    that each source exists in this campaign and is safe for the requested visibility.
    """
    verified = []
    rejected = []
    seen = set()
    for source in evidence_sources if isinstance(evidence_sources, list) else []:
        if not isinstance(source, dict):
            rejected.append({'source': source, 'reason': 'invalid_source_shape'})
            continue
        source_type = clean_text(source.get('source_type'), 50).lower()
        source_id = source.get('source_id')
        source_key = _clock_evidence_key(source)
        if source_type not in _CLOCK_ALLOWED_EVIDENCE_SOURCE_TYPES:
            rejected.append({'source': source, 'reason': 'unsupported_source_type'})
            continue
        if source_key not in allowed_evidence_keys:
            rejected.append({'source': source, 'reason': 'source_not_in_adjudicator_context'})
            continue
        normalized = None
        source_visibility = 'dm_private'
        if source_type == 'transcript_message':
            message = _resolve_clock_transcript_message(campaign, source_id)
            if message is not None:
                normalized = {'source_type': 'transcript_message', 'source_id': str(message.id)}
                source_visibility = 'party_known'
        elif source_type == 'world_event':
            event = _resolve_clock_world_event(campaign, source_id)
            if event is not None:
                normalized = {'source_type': 'world_event', 'source_id': str(event.id)}
                source_visibility = event.visibility or 'dm_private'
        if normalized is None:
            rejected.append({'source': source, 'reason': 'source_not_found_or_not_durable'})
            continue
        if not _clock_visibility_allows(source_visibility, required_visibility):
            rejected.append({'source': normalized, 'reason': 'source_visibility_too_private'})
            continue
        key = (normalized['source_type'], normalized['source_id'])
        if key not in seen:
            seen.add(key)
            verified.append(normalized)
    return verified, rejected


def _normalize_clock_supported_claims(value):
    claims = []
    seen = set()
    for raw_claim in value[:8] if isinstance(value, list) else []:
        claim = clean_text(raw_claim, 520)
        if claim and claim not in seen:
            seen.add(claim)
            claims.append(claim)
    return claims


def _collect_clock_criterion_verdicts(patch):
    verdicts = {}
    duplicates = set()
    raw_verdicts = patch.get('completion_criteria_verdicts')
    for raw_verdict in raw_verdicts if isinstance(raw_verdicts, list) else []:
        if not isinstance(raw_verdict, dict):
            continue
        criterion_id = clean_id(raw_verdict.get('criterion_id') or raw_verdict.get('id'), '')
        if not criterion_id:
            continue
        if criterion_id in verdicts:
            duplicates.add(criterion_id)
            continue
        verdicts[criterion_id] = {
            'criterion_id': criterion_id,
            'verdict': clean_text(raw_verdict.get('verdict'), 30).lower(),
            'supported_claims': _normalize_clock_supported_claims(raw_verdict.get('supported_claims')),
            'evidence_sources': raw_verdict.get('evidence_sources') if isinstance(raw_verdict.get('evidence_sources'), list) else [],
            'reason': clean_text(raw_verdict.get('reason'), 520),
        }
    return verdicts, duplicates


def _persist_clock_completion_verdicts(clock, applied_verdicts):
    raw_state = clock.completion_state if isinstance(clock.completion_state, dict) else {}
    raw_criteria = raw_state.get('criteria') if isinstance(raw_state.get('criteria'), dict) else {}
    criteria_state = {
        criterion_id: dict(verdict)
        for criterion_id, verdict in raw_criteria.items()
        if isinstance(verdict, dict)
    }
    for verdict in applied_verdicts:
        if (
            verdict.get('verdict') == 'met'
            and verdict.get('supported_claims')
            and verdict.get('evidence_sources')
            and not verdict.get('rejected_evidence_sources')
        ):
            criteria_state[verdict['criterion_id']] = {
                'verdict': 'met',
                'supported_claims': list(verdict['supported_claims']),
                'evidence_sources': list(verdict['evidence_sources']),
                'reason': verdict.get('reason'),
            }
    clock.completion_state = {'criteria': criteria_state} if criteria_state else {}
    clock.updated_at = utcnow()


def _retire_clock_from_patch(campaign, patch, allowed_evidence_sources=None):
    clock_id = clean_id(patch.get('clock_id') or patch.get('id'), '')
    clock = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_id).first()
    if not clock:
        return {'error': f'Clock not found: {clock_id}'}
    completion_criteria = _normalize_clock_completion_criteria(clock.completion_criteria)
    required_ids = {criterion['id'] for criterion in completion_criteria}
    mechanical_complete = (clock.filled or 0) >= (clock.segments or 4)
    met_ids = set()
    criterion_evidence_status = {}
    applied_criterion_verdicts = []
    rejected_proposals = []
    allowed_evidence_keys = {
        key
        for source in (allowed_evidence_sources or [])
        if (key := _clock_evidence_key(source)) is not None
    }

    consequence = patch.get('consequence') if isinstance(patch.get('consequence'), dict) else {}
    requested_visibility = consequence.get('visibility') or patch.get('visibility') or clock.visibility or 'dm_private'
    if requested_visibility not in VALID_VISIBILITIES:
        requested_visibility = 'dm_private'

    if completion_criteria:
        if not mechanical_complete:
            return {'error': f'Clock {clock.name} cannot be retired before it is mechanically complete.'}
        clock.status = 'completion_pending'

    criterion_verdicts, duplicate_ids = _collect_clock_criterion_verdicts(patch)
    unknown_ids = set(criterion_verdicts) - required_ids
    missing_ids = required_ids - set(criterion_verdicts)
    if duplicate_ids or unknown_ids or missing_ids:
        rejected_proposals.append({
            'kind': 'completion_criterion_verdicts',
            'missing_ids': sorted(missing_ids),
            'unknown_ids': sorted(unknown_ids),
            'duplicate_ids': sorted(duplicate_ids),
            'reason': 'Completion verdicts must map exactly once to every declared criterion.',
        })
        return {
            'error': f'Clock {clock.name} cannot resolve until every completion criterion has one AI verdict.',
            'rejected_proposals': rejected_proposals,
            'mechanical_complete': mechanical_complete,
            'consequence_applied': False,
        }

    criterion_claims = set()
    unsupported_ids = []
    for criterion_id in sorted(required_ids):
        verdict = criterion_verdicts[criterion_id]
        verified_sources, rejected_sources = _verified_clock_evidence_sources(
            campaign,
            verdict['evidence_sources'],
            requested_visibility,
            allowed_evidence_keys,
        )
        accepted = (
            verdict['verdict'] == 'met'
            and bool(verdict['supported_claims'])
            and bool(verified_sources)
            and not rejected_sources
        )
        criterion_evidence_status[criterion_id] = (
            'verified'
            if verified_sources and not rejected_sources
            else 'rejected_or_missing'
        )
        applied_verdict = {
            **verdict,
            'evidence_sources': verified_sources,
            'rejected_evidence_sources': rejected_sources,
        }
        applied_criterion_verdicts.append(applied_verdict)
        if not accepted:
            unsupported_ids.append(criterion_id)
            continue
        met_ids.add(criterion_id)
        criterion_claims.update(verdict['supported_claims'])

    _persist_clock_completion_verdicts(clock, applied_criterion_verdicts)

    if unsupported_ids:
        rejected_proposals.append({
            'kind': 'completion_criterion_verdicts',
            'criterion_ids': unsupported_ids,
            'verdicts': applied_criterion_verdicts,
            'reason': 'The AI adjudicator did not return met verdicts with durable, visibility-safe sources and explicit supported claims.',
        })
        return {
            'error': f'Clock {clock.name} cannot resolve: one or more completion criteria remain unsupported or uncertain.',
            'rejected_proposals': rejected_proposals,
            'mechanical_complete': mechanical_complete,
            'consequence_applied': False,
        }

    consequence_verdict = clean_text(consequence.get('verdict'), 30).lower()
    consequence_claims = _normalize_clock_supported_claims(consequence.get('claims'))
    consequence_reason = clean_text(consequence.get('reason'), 520)
    verified_consequence_sources, rejected_consequence_sources = _verified_clock_evidence_sources(
        campaign,
        consequence.get('evidence_sources'),
        requested_visibility,
        allowed_evidence_keys,
    )
    claims_derived_from_criteria = not completion_criteria or set(consequence_claims) <= criterion_claims
    consequence_supported = (
        consequence_verdict == 'supported'
        and bool(consequence_claims)
        and bool(verified_consequence_sources)
        and not rejected_consequence_sources
        and claims_derived_from_criteria
    )
    if not consequence_supported:
        rejected_proposals.append({
            'kind': 'consequence_verdict',
            'requested_visibility': requested_visibility,
            'verdict': consequence_verdict or 'missing',
            'claims': consequence_claims,
            'evidence_sources': verified_consequence_sources,
            'rejected_evidence_sources': rejected_consequence_sources,
            'claims_derived_from_criteria': claims_derived_from_criteria,
            'reason': consequence_reason or 'The consequence lacks a supported AI verdict backed by durable sources.',
        })
        return {
            'error': f'Clock {clock.name} cannot apply its consequence without a supported AI verdict and verified sources.',
            'rejected_proposals': rejected_proposals,
            'mechanical_complete': mechanical_complete,
            'consequence_applied': False,
            'visibility_decision': {
                'requested': requested_visibility,
                'applied': 'dm_private',
                'consequence_applied': False,
                'evidence_status': 'pending_ai_adjudication',
                'reason': 'consequence_pending_ai_adjudication',
            },
        }

    applied_visibility = requested_visibility
    visibility_decision = {
        'requested': requested_visibility,
        'applied': applied_visibility,
        'consequence_applied': True,
        'evidence_status': 'ai_adjudicated_verified_sources',
        'reason': 'applied_at_requested_visibility',
    }
    clock.status = 'resolved'
    clock.completion_state = {}
    clock.updated_at = utcnow()
    upsert_memory_embedding(campaign, 'clock', clock.clock_id, clock.to_dict(include_private=True))
    from openrouter import _get_cached_build_sha
    provenance = _applied_clock_provenance(patch.get('provenance'), _get_cached_build_sha())
    provenance['evidence_sources'] = verified_consequence_sources
    provenance['evidence_status'] = 'ai_adjudicated_verified_sources'
    event_summary = clean_text(' '.join(consequence_claims), 1200)
    event = _record_event(
        campaign,
        'clock_retired',
        event_summary,
        {
            'clock_id': clock.clock_id,
            'status': clock.status,
            'mechanical_complete': mechanical_complete,
            'completion_criteria_met': sorted(met_ids) if completion_criteria else [],
            'criterion_evidence_status': criterion_evidence_status,
            'completion_criteria_verdicts': applied_criterion_verdicts,
            'consequence_verdict': {
                'verdict': consequence_verdict,
                'claims': consequence_claims,
                'evidence_sources': verified_consequence_sources,
                'reason': consequence_reason,
            },
            'consequence_applied': True,
            'visibility_decision': visibility_decision,
            'rejected_proposals': rejected_proposals,
            'provenance': provenance,
        },
        visibility=applied_visibility,
    )
    return {
        'clock': clock.to_dict(include_private=True),
        'event_id': event.id,
        'action': 'retired',
        'mechanical_complete': mechanical_complete,
        'consequence_applied': True,
        'visibility_decision': visibility_decision,
        'rejected_proposals': rejected_proposals,
    }


def _applied_clock_provenance(raw_provenance, build_sha):
    """Normalize clock provenance at the durable event write boundary."""
    provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
    prior_stage = provenance.get('pipeline_stage')
    if prior_stage and prior_stage != 'applied':
        provenance['source_pipeline_stage'] = prior_stage
    provenance['pipeline_stage'] = 'applied'
    provenance['tool_name'] = provenance.get('tool_name') or 'session_memory_update_clocks'
    evidence_sources = provenance.get('evidence_sources')
    provenance['evidence_status'] = provenance.get('evidence_status') or determine_evidence_status(evidence_sources)
    provenance['build_sha'] = provenance.get('build_sha') or build_sha
    return provenance


def apply_clock_adjudication(
    campaign,
    updates,
    audit_context=None,
    allowed_evidence_sources=None,
):
    audit_context = audit_context or {}
    updates = updates if isinstance(updates, dict) else {}
    result = {
        'clock_changes': [],
        'world_event_ids': [],
        'no_change_explanations': [],
        'rejected_advances': [],
        'errors': [],
    }

    transcript_evidence_sources = [
        make_evidence_source("transcript_message", str(message_id))
        for message_id in (
            audit_context.get("source_player_message_id") or audit_context.get("player_message_id"),
            audit_context.get("source_dm_message_id") or audit_context.get("dm_message_id"),
        )
        if message_id is not None
    ]
    if allowed_evidence_sources is None:
        allowed_evidence_sources = transcript_evidence_sources

    allowed_evidence_keys = {
        key
        for source in allowed_evidence_sources
        if (key := _clock_evidence_key(source)) is not None
    }
    # Recompute chronology from durable turn IDs and timestamps. The
    # adjudicator-facing chronology label is context, not authority.
    current_turn_evidence_keys = set()
    for source in transcript_evidence_sources:
        key = _clock_evidence_key(source)
        if key is not None:
            current_turn_evidence_keys.add(key)

    player_boundary = _resolve_clock_transcript_message(
        campaign,
        audit_context.get('source_player_message_id') or audit_context.get('player_message_id'),
    )
    if player_boundary is not None and player_boundary.created_at is not None:
        for source in allowed_evidence_sources:
            key = _clock_evidence_key(source)
            if key is None or key[0] != 'world_event':
                continue
            event = _resolve_clock_world_event(campaign, key[1])
            if event is not None and event.created_at is not None and event.created_at >= player_boundary.created_at:
                current_turn_evidence_keys.add(key)

    def resolve_clock_reference(raw_clock_ref):
        clock_key = clean_id(raw_clock_ref, '')
        if clock_key:
            clock = CampaignClock.query.filter_by(campaign_id=campaign.id, clock_id=clock_key).first()
            if clock:
                return clock
        numeric_ref = _coerce_patch_int(raw_clock_ref, default=None, minimum=1)
        if numeric_ref is None:
            return None
        return CampaignClock.query.filter_by(campaign_id=campaign.id, id=numeric_ref).first()

    for item in updates.get('create_clocks', []) if isinstance(updates.get('create_clocks'), list) else []:
        change = _create_clock_from_patch(campaign, item if isinstance(item, dict) else {})
        result['clock_changes'].append(change)
        if change.get('event_id'):
            result['world_event_ids'].append(change['event_id'])
        if change.get('error'):
            result['errors'].append(change['error'])

    for item in updates.get('advance_clocks', []) if isinstance(updates.get('advance_clocks'), list) else []:
        if not isinstance(item, dict):
            continue
        raw_clock_ref = item.get('clock_id')
        if raw_clock_ref in (None, ''):
            raw_clock_ref = item.get('id')
        clock = resolve_clock_reference(raw_clock_ref)
        if not clock:
            missing_ref = clean_text(raw_clock_ref, 80) or str(raw_clock_ref or '').strip()
            if not missing_ref:
                result['errors'].append('Clock adjudication advance was missing clock_id.')
            else:
                result['errors'].append(f'Clock not found: {missing_ref}')
            continue
        clock_id = clock.clock_id
        try:
            delta = int(item.get('delta') or 0)
        except (TypeError, ValueError):
            delta = 0
        if delta > 2:
            delta = 2
        elif delta < -2:
            delta = -2
        if delta == 0:
            result['no_change_explanations'].append({
                'clock_id': clock_id,
                'reason': clean_text(item.get('reason'), 420) or 'Clock adjudicator returned no change.',
            })
            continue
        trigger_verdict = _normalize_clock_trigger_verdict(item.get('trigger_verdict'))
        trigger_clauses = {
            clause['clause_id']: clause
            for clause in _clock_trigger_clauses(clock)
        }
        evidence_gaps = []
        verified_trigger_sources = []
        rejected_trigger_sources = []
        if trigger_verdict is None:
            evidence_gaps.append('missing_structured_trigger_verdict')
        else:
            clause = trigger_clauses.get(trigger_verdict['clause_id'])
            if clause is None:
                evidence_gaps.append('unknown_trigger_clause')
            elif delta not in clause['allowed_deltas']:
                evidence_gaps.append('delta_does_not_match_trigger_clause')
            if trigger_verdict['verdict'] != 'satisfied':
                evidence_gaps.append('trigger_not_satisfied')
            if trigger_verdict['chronology_verdict'] != 'new_current_turn':
                evidence_gaps.append('evidence_not_new_current_turn')
            if not trigger_verdict['supported_claims']:
                evidence_gaps.append('missing_supported_claims')
            if not trigger_verdict['evidence_sources']:
                evidence_gaps.append('missing_evidence_sources')
            else:
                verified_trigger_sources, rejected_trigger_sources = _verified_clock_evidence_sources(
                    campaign,
                    trigger_verdict['evidence_sources'],
                    clock.visibility or 'dm_private',
                    allowed_evidence_keys,
                )
                if rejected_trigger_sources:
                    evidence_gaps.append('unverified_evidence_sources')
                verified_keys = {
                    key for source in verified_trigger_sources
                    if (key := _clock_evidence_key(source)) is not None
                }
                if verified_keys and not verified_keys.issubset(current_turn_evidence_keys):
                    evidence_gaps.append('historical_or_restated_evidence')
                if not verified_keys:
                    evidence_gaps.append('no_verified_new_evidence')

        if evidence_gaps:
            diagnostic = {
                'clock_id': clock_id,
                'reason': 'Clock advance rejected: ' + ', '.join(dict.fromkeys(evidence_gaps)) + '.',
                'evidence_gaps': list(dict.fromkeys(evidence_gaps)),
                'trigger_verdict': trigger_verdict,
                'rejected_evidence_sources': rejected_trigger_sources,
            }
            result['rejected_advances'].append(diagnostic)
            result['no_change_explanations'].append(diagnostic)
            continue
        raw_provenance = item.get('provenance') if isinstance(item.get('provenance'), dict) else {}
        provenance = {
            **raw_provenance,
            'source_player_message_id': (
                raw_provenance.get('source_player_message_id')
                or audit_context.get('source_player_message_id')
                or audit_context.get('player_message_id')
            ),
            'source_dm_message_id': (
                raw_provenance.get('source_dm_message_id')
                or audit_context.get('source_dm_message_id')
                or audit_context.get('dm_message_id')
            ),
            'trace_id': raw_provenance.get('trace_id') or audit_context.get('trace_id'),
            'trigger_clause_id': trigger_verdict['clause_id'],
            'chronology_verdict': trigger_verdict['chronology_verdict'],
            'new_evidence_ids': [source['source_id'] for source in verified_trigger_sources],
            'trigger_verdict': {
                **trigger_verdict,
                'evidence_sources': verified_trigger_sources,
            },
        }
        provenance['evidence_sources'] = verified_trigger_sources
        provenance['evidence_status'] = determine_evidence_status(verified_trigger_sources)
        change = _tool_advance_clock(campaign, None, {
            'clock_id': clock_id,
            'delta': delta,
            'reason': clean_text(item.get('reason'), 420),
            'status': clean_text(item.get('status'), 30),
            'evidence': item.get('evidence') if isinstance(item.get('evidence'), list) else [],
            'provenance': provenance,
        })
        if change.get('error'):
            result['errors'].append(change['error'])
            continue
        result['clock_changes'].append({
            'clock': change.get('clock'),
            'action': 'advanced',
        })
        affected = change.get('affected_ids') or {}
        for event_id in affected.get('world_event_ids', []) if isinstance(affected.get('world_event_ids'), list) else []:
            result['world_event_ids'].append(event_id)

    for item in updates.get('retire_clocks', []) if isinstance(updates.get('retire_clocks'), list) else []:
        item = dict(item) if isinstance(item, dict) else {}
        raw_provenance = item.get('provenance') if isinstance(item.get('provenance'), dict) else {}
        evidence_sources = list(raw_provenance.get('evidence_sources') or [])
        for source in transcript_evidence_sources:
            if source not in evidence_sources:
                evidence_sources.append(source)
        item['provenance'] = {
            **raw_provenance,
            'source_player_message_id': raw_provenance.get('source_player_message_id') or audit_context.get('source_player_message_id') or audit_context.get('player_message_id'),
            'source_dm_message_id': raw_provenance.get('source_dm_message_id') or audit_context.get('source_dm_message_id') or audit_context.get('dm_message_id'),
            'trace_id': raw_provenance.get('trace_id') or audit_context.get('trace_id'),
            'evidence_sources': evidence_sources,
            'evidence_status': raw_provenance.get('evidence_status') or determine_evidence_status(evidence_sources),
        }
        change = _retire_clock_from_patch(
            campaign,
            item,
            allowed_evidence_sources=allowed_evidence_sources,
        )
        result['clock_changes'].append(change)
        if change.get('event_id'):
            result['world_event_ids'].append(change['event_id'])
        if change.get('error'):
            result['errors'].append(change['error'])

    for item in updates.get('no_change_explanations', []) if isinstance(updates.get('no_change_explanations'), list) else []:
        if not isinstance(item, dict):
            continue
        raw_clock_ref = item.get('clock_id')
        if raw_clock_ref in (None, ''):
            raw_clock_ref = item.get('id')
        clock = resolve_clock_reference(raw_clock_ref)
        clock_id = clock.clock_id if clock else clean_id(raw_clock_ref, '')
        reason = clean_text(item.get('reason'), 420)
        if clock_id and reason:
            result['no_change_explanations'].append({
                'clock_id': clock_id,
                'reason': reason,
            })

    log_audit_event(
        campaign.id,
        'clock_adjudication_applied',
        'Applied post-turn clock adjudication.',
        {'updates': updates, 'result': result},
        source='session_clock',
        actor='session_clock_adjudicator',
        trace_id=audit_context.get('trace_id'),
        parent_trace_id=audit_context.get('parent_trace_id'),
        trace_label=audit_context.get('trace_label'),
        audit_role='tools',
        commit=False,
    )
    return result


def _update_npc_actor(campaign, patch):
    actor_id = clean_id(patch.get('id') or patch.get('actor_id'), '')
    if not actor_id:
        return {'error': 'NPC actor id is required.'}
    actor = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=actor_id).first()
    created = actor is None
    if not actor:
        actor = NPCActor(campaign_id=campaign.id, actor_id=actor_id, name=actor_id.replace('_', ' ').title(), dossier='{}')
        db.session.add(actor)
    dossier = json_loads(actor.dossier, {})
    dossier.update({key: value for key, value in patch.items() if value not in (None, '', [])})
    dossier['id'] = actor_id
    actor.name = clean_text(patch.get('name'), 200) or actor.name
    actor.role = clean_text(patch.get('role'), 200) or actor.role
    actor.public_summary = clean_text(patch.get('public_summary'), 420) or actor.public_summary
    actor.dossier = json_dumps(dossier)
    actor.updated_at = utcnow()
    db.session.flush()
    upsert_memory_embedding(campaign, 'npc_actor', actor.actor_id, actor.to_dict(include_private=True))
    event = _record_event(
        campaign,
        'npc_actor_upserted',
        patch.get('reason') or f'NPC actor {actor.name} {"created" if created else "updated"}.',
        {'npc_actor': actor.to_dict(include_private=True)},
    )
    return {'npc_actor': actor.to_dict(include_private=True), 'event_id': event.id, 'action': 'created' if created else 'updated'}


def apply_compiled_session_memory_patch(campaign, session, patch, audit_context=None):
    from models import CampaignMemoryRun, CampaignMemoryLog, CampaignWorld, CampaignClarification, CampaignIdentityResolution, db
    from services.audit_service import log_audit_event
    from services.resolution_registry import (
        persist_clarification_requests,
        persist_identity_resolutions,
    )
    from services.session_memory_agent import MemoryPipelineError
    import uuid

    if audit_context is None:
        audit_context = {}

    if not isinstance(patch, dict):
        raise MemoryPipelineError(
            stage="validation",
            code="validation_error",
            message="Compiled session memory patch is not a dict.",
        )

    source_contract = patch.get("source_contract", "")
    if source_contract != SOURCE_CONTRACT_COMPILED_V2:
        raise MemoryPipelineError(
            stage="validation",
            code="invalid_contract",
            message=f"Expected source_contract={SOURCE_CONTRACT_COMPILED_V2}, got {source_contract!r}.",
        )

    diagnostics = patch.get("resolution_diagnostics")
    if isinstance(diagnostics, dict):
        diag_valid, diag_error = validate_diagnostics(diagnostics)
        if not diag_valid:
            raise MemoryPipelineError(
                stage="validation",
                code="diagnostics_validation_failed",
                message=f"Diagnostics validation failed: {diag_error}",
            )

    base_revision = patch.get("base_memory_revision", 0)
    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    current_revision = world.memory_revision if world else 0
    if base_revision is not None and current_revision != base_revision:
        raise MemoryPipelineError(
            stage="validation",
            code="stale_base_revision",
            message=f"Base memory revision {base_revision} does not match current {current_revision}. Patch is stale.",
            telemetry={"base_revision": base_revision, "current_revision": current_revision},
        )

    telemetry = patch.pop("_telemetry", None) if isinstance(patch, dict) else None
    memory_run_id = audit_context.get("memory_run_id") or f"memrun_{uuid.uuid4().hex[:12]}"
    trace_id = audit_context.get("trace_id")
    player_message_id = audit_context.get("source_player_message_id") or audit_context.get("player_message_id")
    dm_message_id = audit_context.get("source_dm_message_id") or audit_context.get("dm_message_id")
    turn_id = audit_context.get("turn_id")
    if not turn_id and session:
        turn_id = f"session_{session.id}_msg_{player_message_id or 'none'}"

    run_record = CampaignMemoryRun(
        memory_run_id=memory_run_id,
        campaign_id=campaign.id,
        session_id=session.id if session else None,
        source_player_message_id=player_message_id,
        source_dm_message_id=dm_message_id,
        trace_id=trace_id,
        prompt_chars=telemetry.get("prompt_chars") if isinstance(telemetry, dict) else None,
        prompt_tokens_estimate=telemetry.get("prompt_tokens_estimate") if isinstance(telemetry, dict) else None,
        response_chars=telemetry.get("response_chars") if isinstance(telemetry, dict) else None,
        context_breakdown_json=telemetry if isinstance(telemetry, dict) else None,
    )
    db.session.add(run_record)

    # Final output-boundary leak guard. Runs before any graph/NPC mutation so
    # redactions and demotions are what actually persist.
    leak_guard_telemetry = _leak_guard_memory_patch(campaign, patch, _visibility_policy_text(audit_context))
    if isinstance(audit_context, dict):
        audit_context['leak_guard_telemetry'] = leak_guard_telemetry

    world, graph, world_state, _private = _world_json(campaign)
    result = {
        "graph_changes": [],
        "npc_changes": [],
        "world_event_ids": [],
        "running_summary_updated": False,
        "memory_anchors_updated": False,
    }

    graph_entity_ids = {
        entity.get("id")
        for entity in graph.get("entities", [])
        if isinstance(entity, dict) and entity.get("id")
    }
    requested_entity_ids = {
        clean_id(entity.get("id"), "")
        for entity in patch.get("upsert_graph_entities", [])
        if isinstance(entity, dict)
    }
    for npc_item in patch.get("update_npc_actors") if isinstance(patch.get("update_npc_actors"), list) else []:
        if not isinstance(npc_item, dict):
            continue
        actor_id = clean_id(npc_item.get("id") or npc_item.get("actor_id"), "")
        if not actor_id or actor_id in graph_entity_ids or actor_id in requested_entity_ids:
            continue
        patch.setdefault("upsert_graph_entities", []).append({
            "id": actor_id,
            "name": clean_text(npc_item.get("name"), 200) or actor_id.replace("_", " ").title(),
            "type": "npc",
            "summary": clean_text(npc_item.get("public_summary") or npc_item.get("role"), 500) or None,
            "visibility": (
                npc_item.get("visibility")
                if npc_item.get("visibility") in VALID_VISIBILITIES
                else "party_known"
            ),
            "certainty": npc_item.get("certainty") or "confirmed",
            "reason": "Materialized NPC graph endpoint before applying its relationships.",
            "provenance": npc_item.get("provenance"),
        })
        requested_entity_ids.add(actor_id)

    for entity in patch.get("upsert_graph_entities") if isinstance(patch.get("upsert_graph_entities"), list) else []:
        entity = entity if isinstance(entity, dict) else {}
        entity_id = entity.get("id", "")
        if not entity_id:
            continue

        existing_ids = {e.get("id") for e in graph.get("entities", []) if isinstance(e, dict) and e.get("id")}
        if entity_id in existing_ids:
            collisions = [e for e in graph.get("entities", []) if isinstance(e, dict) and e.get("id") == entity_id]
            for collision in collisions:
                existing_type = clean_text(collision.get("type"), 40).lower()
                new_type = clean_text(entity.get("type"), 40).lower()
                if existing_type and new_type and existing_type != new_type and existing_type != "other" and new_type != "other":
                    raise MemoryPipelineError(
                        stage="validation",
                        code="exact_id_collision",
                        message=f"Entity ID {entity_id!r} already exists with incompatible type {existing_type!r} vs {new_type!r}.",
                    )

        merged, action = _upsert_by_id_strict(graph.setdefault("entities", []), entity)
        result["graph_changes"].append({"kind": "entity", "action": action, "id": entity_id})

    materializations, unresolved_endpoints = _materialize_relation_endpoints(campaign, graph, patch)
    if materializations:
        result["relation_endpoint_materializations"] = materializations
        for materialized in materializations:
            result["graph_changes"].append({
                "kind": "entity",
                "action": "materialized_relation_endpoint",
                "id": materialized["endpoint_id"],
                "source": materialized["source"],
            })

    for relation in patch.get("upsert_graph_relations") if isinstance(patch.get("upsert_graph_relations"), list) else []:
        relation = relation if isinstance(relation, dict) else {}
        source_id = relation.get("source_id", "")
        target_id = relation.get("target_id", "")
        all_ids = {e.get("id") for e in graph.get("entities", []) if isinstance(e, dict) and e.get("id")}
        if source_id and source_id not in all_ids:
            raise MemoryPipelineError(
                stage="validation",
                code="missing_relation_endpoint",
                message=f"Relation source {source_id!r} not found in graph entities.",
                telemetry={
                    "endpoint_id": source_id,
                    "endpoint_role": "source",
                    "relation_id": relation.get("id", ""),
                    "graph_entity_count": len(all_ids),
                    "unresolved_endpoints": unresolved_endpoints,
                    "materialized_endpoints": materializations,
                },
            )
        if target_id and target_id not in all_ids:
            raise MemoryPipelineError(
                stage="validation",
                code="missing_relation_endpoint",
                message=f"Relation target {target_id!r} not found in graph entities.",
                telemetry={
                    "endpoint_id": target_id,
                    "endpoint_role": "target",
                    "relation_id": relation.get("id", ""),
                    "graph_entity_count": len(all_ids),
                    "unresolved_endpoints": unresolved_endpoints,
                    "materialized_endpoints": materializations,
                },
            )

        merged, action = _upsert_by_id_strict(graph.setdefault("relations", []), relation)
        result["graph_changes"].append({"kind": "relation", "action": action, "id": relation.get("id", "")})

    for fact in patch.get("upsert_graph_facts") if isinstance(patch.get("upsert_graph_facts"), list) else []:
        fact = fact if isinstance(fact, dict) else {}
        merged, action = _upsert_by_id_strict(graph.setdefault("facts", []), fact)
        result["graph_changes"].append({"kind": "fact", "action": action, "id": fact.get("id", "")})

    scene_patch = patch.get("scene_patch") if isinstance(patch.get("scene_patch"), dict) else {}
    SCENE_STATE_KEYS = {"location_id", "location_name", "time_of_day", "active_npc_ids", "departed_npc_ids", "immediate_tension"}
    has_scene_changes = any(k in scene_patch for k in SCENE_STATE_KEYS)
    if scene_patch and has_scene_changes:
        current_scene = world_state.get("current_scene", {}) if isinstance(world_state, dict) else {}
        clean_scene_state = {k: v for k, v in scene_patch.items() if k in SCENE_STATE_KEYS}
        current_scene.update(clean_scene_state)
        world_state["current_scene"] = current_scene
        _sync_party_known_location(world_state, current_scene)

    world.knowledge_graph = json_dumps(graph)
    world.world_state = json_dumps(world_state)
    world.memory_revision = (world.memory_revision or 0) + 1
    world.updated_at = utcnow()

    for npc_item in patch.get("update_npc_actors") if isinstance(patch.get("update_npc_actors"), list) else []:
        npc_item = npc_item if isinstance(npc_item, dict) else {}
        change = _update_npc_actor(campaign, npc_item)
        result["npc_changes"].append(change)
        if change.get("event_id"):
            result["world_event_ids"].append(change["event_id"])

    for event_patch in patch.get("record_events") if isinstance(patch.get("record_events"), list) else []:
        event_patch = event_patch if isinstance(event_patch, dict) else {}
        event = _record_event(
            campaign,
            event_patch.get("event_type") or "session_memory",
            event_patch.get("summary") or "Session memory updated.",
            event_patch.get("payload") if isinstance(event_patch.get("payload"), dict) else {},
            event_patch.get("visibility") or "dm_private",
        )
        result["world_event_ids"].append(event.id)

    summary = clean_text(patch.get("running_summary"), 4000)
    if summary and session:
        session.running_summary = summary
        result["running_summary_updated"] = True

    anchors = patch.get("memory_anchors")
    if isinstance(anchors, dict) and session:
        session.memory_anchors = anchors
        result["memory_anchors_updated"] = True

    clarification_requests = patch.get("clarification_requests")
    if isinstance(clarification_requests, list):
        persist_clarification_requests(clarification_requests, campaign)

    consumed_clarification_ids = patch.get("consumed_clarification_ids")
    if isinstance(consumed_clarification_ids, list) and consumed_clarification_ids:
        clars = CampaignClarification.query.filter(
            CampaignClarification.campaign_id == campaign.id,
            CampaignClarification.clarification_id.in_(consumed_clarification_ids),
        ).all()
        for clar in clars:
            clar.status = "resolved"

    resolution_records = patch.get("resolution_records")
    if isinstance(resolution_records, list):
        persist_identity_resolutions(resolution_records, campaign)

    leak_telemetry = audit_context.get("leak_guard_telemetry") if isinstance(audit_context, dict) else None
    audit_payload = {"session_id": session.id if session else None, "result": result}
    if isinstance(leak_telemetry, dict):
        audit_payload["leak_guard"] = leak_telemetry
    log_audit_event(
        campaign.id,
        "memory_patch_applied_v2",
        "Applied compiled session memory patch (v2 trusted path).",
        audit_payload,
        source="dm_tools.memory_v2",
        actor="session_memory_writer",
        trace_id=trace_id,
        parent_trace_id=audit_context.get("parent_trace_id"),
        trace_label=audit_context.get("trace_label"),
        commit=False,
    )

    return result


def _upsert_by_id_strict(items, item):
    item = item if isinstance(item, dict) else {}
    item_id = clean_id(item.get("id"), "")
    if not item_id:
        return item, "no-op"
    for index, existing in enumerate(items):
        if existing.get("id") == item_id:
            merged = dict(existing)
            merged.update({key: value for key, value in item.items() if value not in (None, "", [])})
            items[index] = merged
            return merged, "updated"
    items.append(dict(item))
    return item, "created"


MAX_RELATION_ENDPOINT_MATERIALIZATIONS = 50


def _relation_endpoint_refs(patch):
    """Map every relation endpoint id to the relations/references that use it."""
    refs = {}
    for rel in patch.get("upsert_graph_relations") if isinstance(patch.get("upsert_graph_relations"), list) else []:
        if not isinstance(rel, dict):
            continue
        rel_id = clean_id(rel.get("id"), "")
        for field in ("source_id", "target_id"):
            endpoint_id = clean_id(rel.get(field), "")
            if endpoint_id:
                refs.setdefault(endpoint_id, []).append((rel_id, field))
    return refs


def _relation_endpoint_visibility(patch, endpoint_id):
    """Derive a materialized endpoint's visibility from referencing relations.

    Fails closed to ``dm_private``. A registry-backed endpoint placeholder is
    only promoted to ``party_known`` when every relation referencing it is
    itself party-visible (public/party_known). Any private reference, or the
    absence of a positive visibility signal, keeps the materialized entity
    ``dm_private`` so a resolved private NPC used only as an endpoint never
    leaks into the party-visible graph.
    """
    visibilities = []
    for rel in patch.get("upsert_graph_relations") if isinstance(patch.get("upsert_graph_relations"), list) else []:
        if not isinstance(rel, dict):
            continue
        for field in ("source_id", "target_id"):
            if clean_id(rel.get(field), "") == endpoint_id:
                vis = clean_text(rel.get("visibility"), 30).lower()
                if vis:
                    visibilities.append(vis)
    if visibilities and all(vis in {"public", "party_known"} for vis in visibilities):
        return "party_known"
    return "dm_private"


def _valid_relation_endpoint_registry(campaign):
    """Build the campaign's non-graph relation endpoint registry.

    Roster PCs and resolved NPC actors are first-class relation endpoints even
    when they have no graph entity of their own (e.g. a private NPC referenced
    only as a relation source). Returns (npc_rows, character_rows) keyed by id.
    """
    npc_rows = {
        actor.actor_id: actor
        for actor in NPCActor.query.filter_by(campaign_id=campaign.id).all()
        if actor.actor_id
    }
    character_rows = {}
    for character in Character.query.filter_by(campaign_id=campaign.id).all():
        slug = clean_id(character.name, "")
        if slug:
            character_rows.setdefault(slug, character)
    return npc_rows, character_rows


def _materialize_relation_endpoints(campaign, graph, patch):
    """Preserve valid NPC / roster-PC / known-entity relation endpoints.

    A relation may reference a canonical actor that exists in the NPC actor
    registry or the roster but has no graph entity yet. Instead of rejecting the
    whole patch, materialize a graph entity for the endpoint (bounded) so the
    relation can be applied and the endpoint survives into the final graph.
    Returns (materializations, unresolved) where unresolved holds endpoints that
    are not known to any registry and must fail closed.
    """
    graph_entities = graph.setdefault("entities", [])
    graph_entity_ids = {e.get("id") for e in graph_entities if isinstance(e, dict) and e.get("id")}
    requested_entity_ids = {
        clean_id(entity.get("id"), "")
        for entity in patch.get("upsert_graph_entities", [])
        if isinstance(entity, dict)
    }
    endpoint_refs = _relation_endpoint_refs(patch)
    if not endpoint_refs:
        return [], []

    npc_rows, character_rows = _valid_relation_endpoint_registry(campaign)

    materializations = []
    unresolved = []
    materialized_ids = set()
    for endpoint_id, refs in endpoint_refs.items():
        if not endpoint_id:
            continue
        if endpoint_id in graph_entity_ids or endpoint_id in requested_entity_ids or endpoint_id in materialized_ids:
            continue
        if len(materializations) >= MAX_RELATION_ENDPOINT_MATERIALIZATIONS:
            unresolved.append({
                "endpoint_id": endpoint_id,
                "reason": "materialization_limit_exceeded",
                "referenced_by": refs,
            })
            continue

        entity = None
        source = None
        endpoint_visibility = _relation_endpoint_visibility(patch, endpoint_id)
        if endpoint_id in npc_rows:
            npc = npc_rows[endpoint_id]
            source = "known_npc"
            entity = {
                "id": endpoint_id,
                "name": clean_text(npc.name, 200) or endpoint_id.replace("_", " ").title(),
                "type": "npc",
                "summary": clean_text(npc.public_summary or npc.role, 500) or None,
                "visibility": endpoint_visibility,
                "certainty": "confirmed",
                "reason": "Materialized relation endpoint from NPC actor registry before applying its relationships.",
            }
        elif endpoint_id in character_rows:
            character = character_rows[endpoint_id]
            source = "known_roster_pc"
            entity = {
                "id": endpoint_id,
                "name": clean_text(character.name, 200) or endpoint_id.replace("_", " ").title(),
                "type": "pc",
                "summary": clean_text(character.background, 500) or None,
                "visibility": endpoint_visibility,
                "certainty": "confirmed",
                "reason": "Materialized relation endpoint from roster PC before applying its relationships.",
            }

        if entity is None:
            unresolved.append({
                "endpoint_id": endpoint_id,
                "reason": "endpoint_not_in_any_registry",
                "referenced_by": refs,
            })
            continue

        graph_entities.append(entity)
        graph_entity_ids.add(endpoint_id)
        materialized_ids.add(endpoint_id)
        materializations.append({
            "endpoint_id": endpoint_id,
            "source": source,
            "materialized_type": entity["type"],
            "referenced_by": refs,
        })

    return materializations, unresolved


def _dedupe_json_values(values):
    seen = set()
    out = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _merge_dossier_dicts(dup_dossier, can_dossier):
    """Schema-aware dossier merge that never drops duplicate-only data.

    Lists are unioned and de-duplicated, nested maps (relationships) are merged
    per-key with the canonical value winning on conflicts, and scalar conflicts
    deliberately keep the canonical value when non-empty.
    """
    dup_dossier = dup_dossier if isinstance(dup_dossier, dict) else {}
    can_dossier = can_dossier if isinstance(can_dossier, dict) else {}
    merged = {}
    for key in set(dup_dossier) | set(can_dossier):
        dup_val = dup_dossier.get(key)
        can_val = can_dossier.get(key)
        if key == "id":
            merged[key] = can_val or dup_val
        elif isinstance(dup_val, dict) and isinstance(can_val, dict):
            merged_value = dict(dup_val)
            for nested_key, nested_val in can_val.items():
                if nested_val not in (None, "", [], {}):
                    merged_value[nested_key] = nested_val
            merged[key] = merged_value
        elif isinstance(dup_val, list) and isinstance(can_val, list):
            merged[key] = _dedupe_json_values(list(can_val) + list(dup_val))
        else:
            if can_val not in (None, "", []):
                merged[key] = can_val
            else:
                merged[key] = dup_val
    return merged


def repair_duplicate_identity(campaign, duplicate_id, canonical_id):
    """Merge a duplicate identity into its canonical identity without losing data.

    Safely rewrites graph entities, relations, facts, scene cast, and NPC actor
    rows so the campaign ends with exactly one durable identity. Used to repair
    pre-existing split-brain states such as Run 37's provisional duplicate
    created before resolved entity references were consumed.
    """
    from models import CampaignIdentityResolution, CampaignWorld, NPCActor, db

    duplicate_id = clean_id(duplicate_id, "")
    canonical_id = clean_id(canonical_id, "")
    if not duplicate_id or not canonical_id or duplicate_id == canonical_id:
        raise ValueError("duplicate_id and canonical_id must be distinct non-empty ids")

    world = CampaignWorld.query.filter_by(campaign_id=campaign.id).first()
    if world is None:
        raise ValueError("campaign world not found")

    graph = json_loads(world.knowledge_graph, {"entities": [], "relations": [], "facts": []})
    if not isinstance(graph, dict):
        graph = {}
    world_state = json_loads(world.world_state, {})
    if not isinstance(world_state, dict):
        world_state = {}

    entities = graph.setdefault("entities", [])
    dup_entity = next(
        (e for e in entities if isinstance(e, dict) and clean_id(e.get("id"), "") == duplicate_id),
        None,
    )
    can_entity = next(
        (e for e in entities if isinstance(e, dict) and clean_id(e.get("id"), "") == canonical_id),
        None,
    )

    changes = {"graph": [], "npc": [], "cast": [], "resolutions": []}

    if dup_entity is not None and isinstance(dup_entity, dict):
        if can_entity is not None and isinstance(can_entity, dict):
            aliases = list(can_entity.get("aliases") or []) if isinstance(can_entity.get("aliases"), list) else []
            seen = {str(alias).strip().lower() for alias in aliases}
            for alias in dup_entity.get("aliases") or []:
                key = str(alias).strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    aliases.append(alias)
            if str(dup_entity.get("name") or "").strip().lower() not in seen:
                aliases.append(dup_entity.get("name"))
            can_entity["aliases"] = aliases
            if not can_entity.get("summary") and dup_entity.get("summary"):
                can_entity["summary"] = dup_entity["summary"]
            existing_tags = (
                list(can_entity.get("tags") or [])
                if isinstance(can_entity.get("tags"), list)
                else []
            )
            for tag in dup_entity.get("tags") or []:
                if tag not in existing_tags:
                    existing_tags.append(tag)
            can_entity["tags"] = existing_tags
            if can_entity.get("identity_status") in (None, "", "provisional") and dup_entity.get("identity_status"):
                can_entity["identity_status"] = dup_entity["identity_status"]
            changes["graph"].append("merged_entity_into_canonical")
        else:
            dup_entity["id"] = canonical_id
            changes["graph"].append("rekeyed_entity_to_canonical_id")

        for rel in graph.setdefault("relations", []):
            if not isinstance(rel, dict):
                continue
            if rel.get("source_id") == duplicate_id:
                rel["source_id"] = canonical_id
            if rel.get("target_id") == duplicate_id:
                rel["target_id"] = canonical_id
        for fact in graph.setdefault("facts", []):
            if not isinstance(fact, dict) or not isinstance(fact.get("entity_ids"), list):
                continue
            rewritten = [canonical_id if eid == duplicate_id else eid for eid in fact["entity_ids"]]
            deduped = []
            seen = set()
            for eid in rewritten:
                if eid and eid not in seen:
                    seen.add(eid)
                    deduped.append(eid)
            fact["entity_ids"] = deduped
        graph["entities"] = [
            e for e in graph["entities"]
            if not (isinstance(e, dict) and clean_id(e.get("id"), "") == duplicate_id)
        ]
        changes["graph"].append("removed_duplicate_entity")
        changes["graph"].append("rewrote_relations_and_facts")

    current_scene = world_state.get("current_scene")
    if isinstance(current_scene, dict):
        for key in ("active_npc_ids", "departed_npc_ids"):
            ids = current_scene.get(key)
            if isinstance(ids, list):
                rewritten = [
                    canonical_id if clean_id(item, "") == duplicate_id else item
                    for item in ids
                ]
                current_scene[key] = list(dict.fromkeys(rewritten))
                changes["cast"].append(key)

    dup_actor = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=duplicate_id).first()
    can_actor = NPCActor.query.filter_by(campaign_id=campaign.id, actor_id=canonical_id).first()
    if dup_actor is not None:
        dup_dossier = json_loads(dup_actor.dossier, {})
        if can_actor is not None:
            can_dossier = json_loads(can_actor.dossier, {})
            can_actor.dossier = json_dumps(_merge_dossier_dicts(dup_dossier, can_dossier))
            if not can_actor.name:
                can_actor.name = dup_actor.name or can_actor.name
            if not can_actor.role and dup_actor.role:
                can_actor.role = dup_actor.role
            if not can_actor.public_summary and dup_actor.public_summary:
                can_actor.public_summary = dup_actor.public_summary
            can_actor.updated_at = utcnow()
            db.session.delete(dup_actor)
            changes["npc"].append("merged_npc_into_canonical")
            actor_row = can_actor
        else:
            dup_actor.actor_id = canonical_id
            dup_actor.updated_at = utcnow()
            changes["npc"].append("renamed_npc_to_canonical_id")
            actor_row = dup_actor
        try:
            upsert_memory_embedding(
                campaign,
                "npc_actor",
                canonical_id,
                actor_row.to_dict(include_private=True),
            )
        except Exception:
            pass

    existing_record = CampaignIdentityResolution.query.filter_by(
        campaign_id=campaign.id,
        mention_entity_id=duplicate_id,
        canonical_id=canonical_id,
    ).first()
    if existing_record is None:
        dup_name = (dup_entity or {}).get("name") or (dup_actor.name if dup_actor else None) or duplicate_id
        can_name = (can_entity or {}).get("name") or (can_actor.name if can_actor else None) or canonical_id
        import hashlib

        key_raw = f"{campaign.id}:{duplicate_id}:{canonical_id}:repair"
        db.session.add(CampaignIdentityResolution(
            campaign_id=campaign.id,
            resolution_id=f"ires_{hashlib.md5(key_raw.encode('utf-8')).hexdigest()[:16]}",
            mention_entity_id=duplicate_id,
            mention_name=clean_text(dup_name, 400),
            resolution_action="add_alias",
            canonical_id=canonical_id,
            canonical_name=clean_text(can_name, 400),
            visibility="dm_private",
            resolved_by="identity_repair",
            evidence_json=[{"source": "identity_repair", "field": "duplicate_merge"}],
        ))
        changes["resolutions"].append("added_alias_resolution_record")

    world.knowledge_graph = json_dumps(graph)
    world.world_state = json_dumps(world_state)
    world.memory_revision = (world.memory_revision or 0) + 1
    world.updated_at = utcnow()
    db.session.flush()

    _record_event(
        campaign,
        "identity_repair",
        f"Repaired duplicate identity {duplicate_id} into canonical {canonical_id}.",
        {"duplicate_id": duplicate_id, "canonical_id": canonical_id, "changes": changes},
    )
    return {"duplicate_id": duplicate_id, "canonical_id": canonical_id, "changes": changes}

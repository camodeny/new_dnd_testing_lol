import hashlib
import json
import math
import os
from datetime import datetime

import requests

from models import CampaignMemoryEmbedding, db
from services.audit_service import log_audit_event


GEMINI_EMBEDDING_BASE_URL = os.environ.get(
    'GEMINI_EMBEDDING_BASE_URL',
    'https://generativelanguage.googleapis.com/v1beta',
)


def _env_bool(name, default='true'):
    return str(os.environ.get(name, default)).strip().lower() in {'1', 'true', 'yes', 'on', 'enabled'}


def embeddings_enabled():
    return _env_bool('GEMINI_EMBEDDINGS_ENABLED', 'true')


def embedding_model():
    return os.environ.get('GEMINI_EMBEDDING_MODEL', 'gemini-embedding-001').strip() or 'gemini-embedding-001'


def embedding_dimensions():
    try:
        value = int(os.environ.get('GEMINI_EMBEDDING_DIMENSIONS', '768'))
    except (TypeError, ValueError):
        value = 768
    return min(max(value, 128), 3072)


def dedupe_threshold():
    try:
        return float(os.environ.get('MEMORY_EMBEDDING_DEDUPE_THRESHOLD', '0.90'))
    except (TypeError, ValueError):
        return 0.90


def search_weight():
    try:
        return float(os.environ.get('MEMORY_EMBEDDING_SEARCH_WEIGHT', '0.70'))
    except (TypeError, ValueError):
        return 0.70


def _api_key():
    return os.environ.get('GEMINI_API_KEY', '').strip()


def clean_text(value, max_length=1600):
    return ' '.join(str(value or '').strip().split())[:max_length]


def _list_text(values, max_items=8):
    if not isinstance(values, list):
        return ''
    cleaned = [clean_text(value, 120) for value in values[:max_items]]
    return ', '.join(value for value in cleaned if value)


def _dict_text(value):
    if not isinstance(value, dict):
        return clean_text(value, 1200)
    return clean_text(json.dumps(value, ensure_ascii=False, sort_keys=True), 1600)


def canonical_text_for_item(item_type, value):
    value = value if isinstance(value, dict) else {}
    visibility = clean_text(value.get('visibility'), 40)
    include_private_fields = visibility not in {'public', 'party_known'}
    if item_type == 'entity':
        parts = [
            f"Entity: {clean_text(value.get('name'), 200)}",
            f"Type: {clean_text(value.get('type'), 80)}",
            f"ID: {clean_text(value.get('id'), 160)}",
            f"Visibility: {clean_text(value.get('visibility'), 40)}",
            f"Summary: {clean_text(value.get('summary'), 1000)}",
            f"Tags: {_list_text(value.get('tags'))}",
        ]
    elif item_type == 'relation':
        parts = [
            f"Relation: {clean_text(value.get('source_id'), 160)} -> {clean_text(value.get('target_id'), 160)}",
            f"Type: {clean_text(value.get('type'), 100)}",
            f"ID: {clean_text(value.get('id'), 160)}",
            f"Visibility: {clean_text(value.get('visibility'), 40)}",
            f"Summary: {clean_text(value.get('summary'), 1000)}",
        ]
    elif item_type == 'fact':
        parts = [
            f"Fact: {clean_text(value.get('text'), 1200)}",
            f"ID: {clean_text(value.get('id'), 160)}",
            f"Entities: {_list_text(value.get('entity_ids'), 12)}",
            f"Certainty: {clean_text(value.get('certainty'), 80)}",
            f"Visibility: {clean_text(value.get('visibility'), 40)}",
        ]
    elif item_type == 'npc_actor':
        dossier = value.get('dossier') if isinstance(value.get('dossier'), dict) else value
        parts = [
            f"NPC: {clean_text(value.get('name') or dossier.get('name'), 200)}",
            f"ID: {clean_text(value.get('actor_id') or dossier.get('id'), 160)}",
            f"Role: {clean_text(value.get('role') or dossier.get('role'), 200)}",
            f"Public summary: {clean_text(value.get('public_summary') or dossier.get('public_summary'), 1000)}",
            f"Wants: {_list_text(dossier.get('wants'))}",
            f"Fears: {_list_text(dossier.get('fears'))}",
            f"Secrets: {_list_text(dossier.get('secrets'))}",
            f"Recent offscreen activity: {_list_text(dossier.get('recent_offscreen_activity'))}",
        ]
    elif item_type == 'clock':
        parts = [
            f"Clock: {clean_text(value.get('name'), 200)}",
            f"ID: {clean_text(value.get('clock_id') or value.get('id'), 160)}",
            f"Type: {clean_text(value.get('pressure_type'), 80)}",
            f"Status: {clean_text(value.get('status'), 80)}",
            f"Visibility: {clean_text(value.get('visibility'), 40)}",
            f"Summary: {clean_text(value.get('summary'), 800)}",
        ]
        if include_private_fields:
            parts.extend([
                f"Trigger: {clean_text(value.get('trigger'), 600)}",
                f"On complete: {clean_text(value.get('on_complete'), 600)}",
            ])
    elif item_type == 'world_event':
        parts = [
            f"World event: {clean_text(value.get('summary'), 1000)}",
            f"ID: {clean_text(value.get('id'), 160)}",
            f"Type: {clean_text(value.get('event_type'), 100)}",
            f"Visibility: {clean_text(value.get('visibility'), 40)}",
            f"Payload: {_dict_text(value.get('payload'))}",
        ]
    elif item_type == 'world_state':
        parts = [f"World state: {_dict_text(value)}"]
    elif item_type == 'dm_private':
        parts = [f"DM private memory: {_dict_text(value)}"]
    else:
        parts = [f"{item_type}: {_dict_text(value)}"]
    return clean_text('. '.join(part for part in parts if part and not part.endswith(': ')), 2400)


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def cosine_similarity(left, right):
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _normalize_vector(values):
    if not values:
        return []
    vector = [float(value) for value in values]
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def _embedding_values(response):
    if not isinstance(response, dict):
        return []
    embedding = response.get('embedding')
    if isinstance(embedding, dict) and isinstance(embedding.get('values'), list):
        return embedding.get('values')
    embeddings = response.get('embeddings')
    if isinstance(embeddings, list) and embeddings:
        first = embeddings[0] if isinstance(embeddings[0], dict) else {}
        if isinstance(first.get('values'), list):
            return first.get('values')
    return []


def _post_embedding(text):
    if not embeddings_enabled():
        raise RuntimeError('Gemini embeddings are disabled')
    api_key = _api_key()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY is not set')

    model = embedding_model()
    payload = {
        'model': f'models/{model}',
        'content': {'parts': [{'text': text}]},
        'output_dimensionality': embedding_dimensions(),
    }
    if model == 'gemini-embedding-001':
        payload['taskType'] = 'SEMANTIC_SIMILARITY'
    response = requests.post(
        f'{GEMINI_EMBEDDING_BASE_URL}/models/{model}:embedContent',
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': api_key,
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    vector = _normalize_vector(_embedding_values(response.json()))
    if not vector:
        raise RuntimeError('Gemini embedding response did not include values')
    return vector


def _log(campaign_id, event_type, summary, payload=None, audit_context=None):
    if not campaign_id:
        return
    audit_context = audit_context or {}
    log_audit_event(
        campaign_id,
        event_type,
        summary,
        payload or {},
        source='gemini_embeddings',
        actor='embedding_service',
        trace_id=audit_context.get('trace_id'),
        parent_trace_id=audit_context.get('parent_trace_id'),
        trace_label=audit_context.get('trace_label'),
        audit_role='tools',
        commit=False,
    )


def embedding_from_text(campaign_id, text, audit_context=None, reason='embedding_request'):
    model = embedding_model()
    dimensions = embedding_dimensions()
    try:
        vector = _post_embedding(text)
        return {
            'ok': True,
            'vector': vector,
            'model': model,
            'dimensions': len(vector),
        }
    except Exception as err:
        _log(
            campaign_id,
            'embedding_fallback',
            'Gemini embedding request failed open.',
            {
                'reason': reason,
                'model': model,
                'configured_dimensions': dimensions,
                'error': repr(err),
            },
            audit_context,
        )
        return {
            'ok': False,
            'vector': [],
            'model': model,
            'dimensions': dimensions,
            'error': repr(err),
        }


def _load_vector(row):
    try:
        return json.loads(row.embedding_json)
    except (TypeError, ValueError):
        return []


def upsert_memory_embedding(campaign, item_type, item_id, value, audit_context=None, embedding_result=None):
    canonical_text = canonical_text_for_item(item_type, value)
    if not canonical_text:
        return {'ok': False, 'reason': 'empty_canonical_text'}
    embedding = embedding_result or embedding_from_text(
        campaign.id,
        canonical_text,
        audit_context=audit_context,
        reason=f'upsert_{item_type}',
    )
    if not embedding['ok']:
        return embedding

    visibility = clean_text(value.get('visibility') if isinstance(value, dict) else '', 40) or 'dm_private'
    row = CampaignMemoryEmbedding.query.filter_by(
        campaign_id=campaign.id,
        item_type=item_type,
        item_id=str(item_id),
    ).first()
    if not row:
        row = CampaignMemoryEmbedding(
            campaign_id=campaign.id,
            item_type=item_type,
            item_id=str(item_id),
            canonical_text=canonical_text,
            text_hash=text_hash(canonical_text),
            embedding_model=embedding['model'],
            embedding_dimensions=embedding['dimensions'],
            embedding_json='[]',
        )
        db.session.add(row)
    row.visibility = visibility
    row.canonical_text = canonical_text
    row.text_hash = text_hash(canonical_text)
    row.embedding_model = embedding['model']
    row.embedding_dimensions = embedding['dimensions']
    row.embedding_json = json.dumps(embedding['vector'], ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    db.session.flush()
    _log(
        campaign.id,
        'embedding_write',
        'Stored campaign memory embedding.',
        {
            'item_type': item_type,
            'item_id': str(item_id),
            'model': row.embedding_model,
            'dimensions': row.embedding_dimensions,
            'text_hash': row.text_hash,
        },
        audit_context,
    )
    return {'ok': True, 'embedding_id': row.id, 'text_hash': row.text_hash}


def find_duplicate_graph_item(campaign, item_type, candidate, audit_context=None):
    canonical_text = canonical_text_for_item(item_type, candidate)
    embedding = embedding_from_text(
        campaign.id,
        canonical_text,
        audit_context=audit_context,
        reason=f'dedupe_{item_type}',
    )
    if not embedding['ok']:
        return {'ok': False, 'duplicate_id': None, 'reason': embedding.get('error'), 'embedding': embedding}

    rows = CampaignMemoryEmbedding.query.filter_by(
        campaign_id=campaign.id,
        item_type=item_type,
        embedding_model=embedding['model'],
        embedding_dimensions=embedding['dimensions'],
    ).all()
    best = None
    for row in rows:
        similarity = cosine_similarity(embedding['vector'], _load_vector(row))
        if best is None or similarity > best['similarity']:
            best = {
                'item_id': row.item_id,
                'similarity': similarity,
                'canonical_text': row.canonical_text,
            }
    threshold = dedupe_threshold()
    duplicate_id = best['item_id'] if best and best['similarity'] >= threshold else None
    _log(
        campaign.id,
        'embedding_search',
        'Searched campaign memory embeddings for duplicate graph item.',
        {
            'mode': 'dedupe',
            'item_type': item_type,
            'candidate_id': candidate.get('id') if isinstance(candidate, dict) else None,
            'model': embedding['model'],
            'dimensions': embedding['dimensions'],
            'threshold': threshold,
            'best_item_id': best['item_id'] if best else None,
            'best_similarity': round(best['similarity'], 4) if best else None,
            'duplicate_id': duplicate_id,
        },
        audit_context,
    )
    return {'ok': True, 'duplicate_id': duplicate_id, 'best': best, 'embedding': embedding}


def search_memory_embeddings(campaign, query, candidates, limit, audit_context=None):
    query = clean_text(query, 800)
    embedding = embedding_from_text(
        campaign.id,
        query,
        audit_context=audit_context,
        reason='memory_search',
    )
    if not embedding['ok']:
        return {'ok': False, 'scores': {}, 'reason': embedding.get('error')}

    candidate_keys = {
        (str(candidate.get('kind')), str(candidate.get('item_id')))
        for candidate in candidates
        if candidate.get('item_id') is not None
    }
    rows = CampaignMemoryEmbedding.query.filter(
        CampaignMemoryEmbedding.campaign_id == campaign.id,
        CampaignMemoryEmbedding.embedding_model == embedding['model'],
        CampaignMemoryEmbedding.embedding_dimensions == embedding['dimensions'],
    ).all()
    scores = {}
    for row in rows:
        key = (row.item_type, row.item_id)
        if key not in candidate_keys:
            continue
        scores[key] = cosine_similarity(embedding['vector'], _load_vector(row))
    top_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    _log(
        campaign.id,
        'embedding_search',
        'Searched campaign memory embeddings for semantic memory matches.',
        {
            'mode': 'memory_search',
            'query_hash': text_hash(query),
            'model': embedding['model'],
            'dimensions': embedding['dimensions'],
            'candidate_count': len(candidates),
            'matched_embedding_count': len(scores),
            'top_matches': [
                {'item_type': key[0], 'item_id': key[1], 'similarity': round(score, 4)}
                for key, score in top_scores
            ],
        },
        audit_context,
    )
    return {'ok': True, 'scores': scores}

"""backfill campaign world identity mappings

Revision ID: b8c9d0e1f2a3
Revises: a7b9c1d2e3f4
Create Date: 2026-08-18 00:00:00.000000

"""
import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8c9d0e1f2a3'
down_revision = 'a7b9c1d2e3f4'
branch_labels = None
depends_on = None


def _normalized_name(value):
    return ' '.join(str(value or '').split()).casefold()


def _unique_pairs(knowledge_graph, actors):
    """Return unambiguous graph-NPC to actor pairs from persisted world data."""
    try:
        graph = json.loads(knowledge_graph or '{}')
    except (TypeError, ValueError):
        return []
    entities = graph.get('entities') if isinstance(graph, dict) else []
    entities = entities if isinstance(entities, list) else []

    graph_by_name = {}
    for entity in entities:
        if not isinstance(entity, dict) or str(entity.get('type') or '').casefold() != 'npc':
            continue
        entity_id = str(entity.get('id') or '').strip()
        name = _normalized_name(entity.get('name'))
        if entity_id and name:
            graph_by_name.setdefault(name, []).append(entity_id)

    actors_by_name = {}
    for actor in actors:
        actor_id = str(actor['actor_id'] or '').strip()
        name = _normalized_name(actor['name'])
        if actor_id and name:
            actors_by_name.setdefault(name, []).append(actor_id)

    return [
        (entity_ids[0], actor_ids[0])
        for name, entity_ids in graph_by_name.items()
        if len(entity_ids) == 1 and len(actor_ids := actors_by_name.get(name, [])) == 1
    ]


def upgrade():
    bind = op.get_bind()
    worlds = bind.execute(sa.text('SELECT campaign_id, knowledge_graph FROM campaign_worlds')).mappings()
    for world in worlds:
        campaign_id = world['campaign_id']
        actors = bind.execute(
            sa.text('SELECT actor_id, name FROM npc_actors WHERE campaign_id = :campaign_id'),
            {'campaign_id': campaign_id},
        ).mappings().all()
        existing = bind.execute(
            sa.text(
                'SELECT graph_entity_id, actor_id FROM campaign_world_identities '
                'WHERE campaign_id = :campaign_id'
            ),
            {'campaign_id': campaign_id},
        ).mappings().all()
        mapped_entities = {row['graph_entity_id'] for row in existing}
        mapped_actors = {row['actor_id'] for row in existing}
        for graph_entity_id, actor_id in _unique_pairs(world['knowledge_graph'], actors):
            if graph_entity_id in mapped_entities or actor_id in mapped_actors:
                continue
            bind.execute(
                sa.text(
                    'INSERT INTO campaign_world_identities '
                    '(campaign_id, graph_entity_id, actor_id) '
                    'VALUES (:campaign_id, :graph_entity_id, :actor_id)'
                ),
                {
                    'campaign_id': campaign_id,
                    'graph_entity_id': graph_entity_id,
                    'actor_id': actor_id,
                },
            )


def downgrade():
    # The mapping is a data repair and cannot be distinguished from a mapping
    # intentionally persisted by a later world-generation write.
    pass

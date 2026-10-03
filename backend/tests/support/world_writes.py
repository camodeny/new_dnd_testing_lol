"""Test seeding helper: run one world writer inside a revision-ordered commit.

Production world writes happen inside a turn's ``commit_campaign_mutation``
(effects, post-turn materialization, world seed). Tests that only need world
state seeded use this to get the same transactional shape: the writer runs
under the campaign row lock, the campaign revision bumps, and a domain event
is recorded.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from app.campaigns.events import commit_campaign_mutation


def commit_world_write(
    db,
    campaign_id,
    expected_revision: int,
    writer: Callable[..., Any],
    *args: Any,
    actor_id: Any = None,
    event_type: str = "test.world_write",
    event_visibility: str = "dm_only",
    **kwargs: Any,
) -> tuple[Any, Any]:
    """Return ``(record, event)``; ``record`` is the writer's row (or result)."""
    params = inspect.signature(writer).parameters
    if "idempotency_key" in params and kwargs.get("idempotency_key") is None and kwargs.get("operation_id"):
        kwargs["idempotency_key"] = kwargs["operation_id"]
    holder: dict[str, Any] = {}

    def _mutate(campaign) -> None:
        call_kwargs = dict(kwargs)
        if "new_revision" in params:
            call_kwargs["new_revision"] = int(campaign.revision or 0) + 1
        holder["result"] = writer(db, campaign, *args, **call_kwargs)

    _, event = commit_campaign_mutation(
        db, campaign_id, int(expected_revision),
        event_type=event_type,
        payload={},
        operation_id=kwargs.get("operation_id"),
        actor_id=actor_id,
        visibility=event_visibility,
        mutate=_mutate,
    )
    result = holder["result"]
    record = result[0] if isinstance(result, tuple) else result
    return record, event

"""Issue #469 — names learned in play stick to their entity.

Playtest 2026-10-04: an NPC introduced as "Hooded Door-Warder" said "they
call me Pell", but the entity kept its placeholder name, so narration kept
saying "the Hooded Door-Warder" and, at the climax, a different person was
voiced through Pell's entity.
"""
from __future__ import annotations

import uuid

import pytest

from app.decisions import DecisionService
from app.dm.contract import normalize_contract
from app.world.identity import (
    alias_owner,
    check_entity_rename,
    exact_identity,
    promote_new_entities_from_contract,
    remember_reused_name,
    reveal_entity_name,
)
from app.world.service import apply_scene_update
from models.campaigns import Campaign
from models.world import CampaignCurrentScene, WorldEntity
from tests.support.fake_decisions import FakeDecisionAdapter
from tests.test_world_identity_214 import make_entity, setup_db


def _promote(db, campaign, public_name, service):
    attempt = type("Attempt", (), {"id": uuid.uuid4(), "commit_operation_id": "op", "contract_snapshot": {
        "new_entities": [{"temp_id": "tmp", "kind": "npc", "public_name": public_name}]}})()
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    return promote_new_entities_from_contract(db, campaign, turn, attempt, identity_decision_service=service)


def test_reuse_under_a_new_name_records_an_alias_and_resolves_it_next_time():
    db, campaign = setup_db()
    mara = make_entity(db, campaign, "Mara Venn", idempotency_key="mara")
    reuse = DecisionService(FakeDecisionAdapter(answers={"resolve_world_entity_identity": str(mara.id)}))

    [reused] = _promote(db, campaign, "the ferrywoman", reuse)

    assert reused.id == mara.id and reused.name == "Mara Venn"
    assert alias_owner(db, campaign.id, "The Ferrywoman").id == mara.id
    # Next turn the same name is an exact match: no decision call needed.
    no_model = DecisionService(FakeDecisionAdapter(answers={}))
    [again] = _promote(db, campaign, "the ferrywoman", no_model)
    assert again.id == mara.id


def test_remembering_a_name_never_fails_a_commit():
    db, campaign = setup_db()
    mara = make_entity(db, campaign, "Mara Venn")
    make_entity(db, campaign, "Old Tom")
    assert remember_reused_name(db, mara, "Old Tom") is False  # another entity's name
    assert remember_reused_name(db, mara, "mara venn") is False  # already hers
    assert remember_reused_name(db, mara, "") is False  # invalid name


def test_reveal_renames_keeps_the_placeholder_and_updates_the_scene():
    db, campaign = setup_db()
    warder = make_entity(db, campaign, "Hooded Door-Warder")
    apply_scene_update(db, campaign, new_revision=8, present_actors=[
        {"kind": "pc", "name": "Wren"},
        {"kind": "npc", "name": "Hooded Door-Warder", "entity_id": str(warder.id)},
    ])

    reveal_entity_name(db, campaign.id, warder.id, "Pell")

    assert db.get(WorldEntity, warder.id).name == "Pell"
    assert exact_identity(db, campaign.id, "Hooded Door-Warder").id == warder.id
    actors = db.get(CampaignCurrentScene, campaign.id).present_actors
    assert {"kind": "npc", "name": "Pell", "entity_id": str(warder.id)} in actors


def test_reveal_refuses_a_name_another_entity_already_holds():
    db, campaign = setup_db()
    warder = make_entity(db, campaign, "Hooded Door-Warder")
    make_entity(db, campaign, "Pell")
    with pytest.raises(ValueError, match="already belongs to another entity"):
        check_entity_rename(db, campaign.id, warder.id, "Pell")
    with pytest.raises(ValueError, match="already carries that name"):
        check_entity_rename(db, campaign.id, warder.id, "hooded door-warder")


# ── Through the DM turn ─────────────────────────────────────────────────────

from tests.test_dm_mechanics_229 import _contract, _run, _submit, table  # noqa: E402,F401


def _reveal(entity, name):
    return {"id": "reveal_1", "effect_type": "reveal_entity_name",
            "arguments": {"entity_id": str(entity.id), "name": name}}


def test_dm_turn_reveal_commits_the_new_name(table):
    from app.world.service import create_entity

    s, camp_id, thread_id, _ = table
    warder, _ = create_entity(s, s.get(Campaign, camp_id), entity_type="npc", name="Hooded Door-Warder")
    s.commit()
    raw = _contract([], text="The warder whispers that they call him Pell.")
    raw["staged_effects"] = [_reveal(warder, "Pell")]

    turn, _attempt = _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(raw))

    assert turn.status == "succeeded"
    assert s.get(WorldEntity, warder.id).name == "Pell"


def test_private_thread_reveal_is_refused_before_narration(table):
    from app.dm.mechanics import reveal_issues
    from app.world.service import create_entity

    s, camp_id, thread_id, _ = table
    warder, _ = create_entity(s, s.get(Campaign, camp_id), entity_type="npc", name="Hooded Door-Warder")
    s.commit()
    raw = _contract([])
    raw["staged_effects"] = [_reveal(warder, "Pell")]
    private_turn = type("Turn", (), {"audience": "private"})()

    [issue] = reveal_issues(s, s.get(Campaign, camp_id), private_turn, normalize_contract(raw))

    assert issue.code == "private_reveal"


def test_provider_schema_documents_reveal_keys():
    from app.dm.contract import contract_json_schema_strict

    guide = contract_json_schema_strict()["$defs"]["StagedEffect"]["properties"]["arguments"]["description"]
    assert "reveal_entity_name{entity_id*, name*}" in guide

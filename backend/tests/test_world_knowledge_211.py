"""Issue #211 — per-knower knowledge separate from truth and visibility."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.world.epistemics import (  # noqa: E402
    assert_knowledge_inline,
    grant_visibility_inline,
    has_active_grant,
    list_active_grants,
    may_user_receive,
    project_facts_for_user,
    project_relations_for_user,
    revoke_visibility_inline,
    what_does_subject_know,
    who_knows_target,
)
from app.world.knowledge import create_fact_authoritative, create_relation_authoritative  # noqa: E402
from app.world.service import create_entity_authoritative  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldKnowledge, WorldVisibilityGrant  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    carol = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Profile(id=bob, email="bob@example.com"),
        Profile(id=carol, email="carol@example.com"),
    ])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Epistemics", revision=0)
    db.add(camp)
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp.id, user_id=alice, role="player"),
        CampaignMember(campaign_id=camp.id, user_id=bob, role="player"),
        CampaignMember(campaign_id=camp.id, user_id=carol, role="player"),
    ])
    db.commit()
    return Fac, db.get(Campaign, camp.id), owner, alice, bob, carol


def _entities(db, camp, rev=0):
    mara, _ = create_entity_authoritative(
        db, camp.id, rev, entity_type="npc", name="Mara",
        operation_id="op-mara-211",
    )
    guild, _ = create_entity_authoritative(
        db, camp.id, rev + 1, entity_type="faction", name="Guild",
        operation_id="op-guild-211",
    )
    aria, _ = create_entity_authoritative(
        db, camp.id, rev + 2, entity_type="character", name="Aria",
        operation_id="op-aria-211",
    )
    bram, _ = create_entity_authoritative(
        db, camp.id, rev + 3, entity_type="character", name="Bram",
        operation_id="op-bram-211",
    )
    party, _ = create_entity_authoritative(
        db, camp.id, rev + 4, entity_type="faction", name="Party",
        operation_id="op-party-211",
    )
    return mara, guild, aria, bram, party


# ── DM-only truth with zero knowers ─────────────────────────────────────────

def test_dm_only_truth_exists_with_zero_knowers():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    mara, *_ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The vault holds a phylactery.",
        entity_refs=[mara.id], epistemic_state="confirmed",
        visibility="dm_only",
        provenance={"source": "dm_adjudication"},
        operation_id="op-truth-1",
    )
    # Objective truth stands with no knower rows at all.
    assert db.execute(
        select(WorldKnowledge).where(WorldKnowledge.campaign_id == camp.id)
    ).scalars().all() == []
    assert who_knows_target(db, camp, "fact", fact.id, owner)["total"] == 0
    assert may_user_receive(db, camp, "fact", fact.id, owner)["allowed"] is True
    denied = may_user_receive(db, camp, "fact", fact.id, alice)
    assert denied == {"allowed": False, "reason": "dm_only_requires_authority"}
    proj = project_facts_for_user(db, camp, alice, [fact])
    assert proj["visible"] == 0 and proj["denied"] == 1
    assert proj["records"] == []
    assert proj["denied_reasons"] == {"dm_only_requires_authority": 1}


# ── One-character knowledge; two characters differ on the same fact ─────────

def test_one_character_knowledge_and_two_characters_differ():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    _, _, aria, bram, _ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The bridge is trapped.",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-bridge-truth",
    )
    k_aria, created_a = assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="knows", acquisition_source="saw_trap",
        visibility="campaign", operation_id="op-k-aria",
    )
    assert created_a is True
    assert k_aria.acquisition_source == "saw_trap"
    assert k_aria.provenance["acquisition_source"] == "saw_trap"
    k_bram, _ = assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=bram.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="does_not_know", acquisition_source="was_absent",
        visibility="campaign", operation_id="op-k-bram",
    )
    # Objective truth unchanged by divergent beliefs.
    assert db.get(type(fact), fact.id).epistemic_state == "confirmed"
    assert k_aria.knowledge_state == "knows"
    assert k_bram.knowledge_state == "does_not_know"
    # Per-subject projections differ on the same fact.
    aria_view = what_does_subject_know(db, camp, aria.id, owner)
    bram_view = what_does_subject_know(db, camp, bram.id, owner)
    assert [(e["target_id"], e["knowledge_state"]) for e in aria_view["entries"]] == [(str(fact.id), "knows")]
    assert [(e["target_id"], e["knowledge_state"]) for e in bram_view["entries"]] == [(str(fact.id), "does_not_know")]
    # Who-knows lists both stances for the DM.
    who = who_knows_target(db, camp, "fact", fact.id, owner)
    assert who["total"] == 2 and who["visible"] == 2
    by_subject = {k["subject_entity_id"]: k["knowledge_state"] for k in who["knowers"]}
    assert by_subject == {str(aria.id): "knows", str(bram.id): "does_not_know"}


# ── Party belief vs truth ───────────────────────────────────────────────────

def test_party_belief_can_contradict_truth():
    Fac, camp, owner, *_ = _setup()
    db = Fac()
    _, _, _, _, party = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The Guild guards the bridge.",
        epistemic_state="false", visibility="dm_only",
        provenance={"source": "dm_adjudication"},
        operation_id="op-rumor-truth",
    )
    row, _ = assert_knowledge_inline(
        db, camp, subject_kind="party", subject_entity_id=party.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="believes", acquisition_source="tavern_rumor",
        visibility="dm_only", operation_id="op-k-party",
    )
    assert row.knowledge_state == "believes"
    # Belief does not promote the rumor to truth.
    assert db.get(type(fact), fact.id).epistemic_state == "false"
    proj = what_does_subject_know(db, camp, party.id, owner)
    assert proj["total"] == 1 and proj["entries"][0]["knowledge_state"] == "believes"


# ── Human visibility differs from character knowledge ───────────────────────

def test_human_visibility_differs_from_character_knowledge():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    _, _, aria, _, _ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The door glyph explodes on touch.",
        epistemic_state="confirmed", visibility="private",
        operation_id="op-glyph-truth",
    )
    # Alice the human is granted the handout...
    grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=alice, granted_by=owner, operation_id="op-grant-alice",
    )
    # ...while Aria the character fictionally does NOT know it.
    assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=fact.id,
        knowledge_state="does_not_know", acquisition_source="never_told",
        visibility="campaign", operation_id="op-k-aria-ignorant",
    )
    assert may_user_receive(db, camp, "fact", fact.id, alice)["allowed"] is True
    aria_proj = what_does_subject_know(db, camp, aria.id, alice)
    assert aria_proj["entries"][0]["knowledge_state"] == "does_not_know"
    assert aria_proj["entries"][0]["target"]["content"] == "The door glyph explodes on touch."
    # And the reverse: knowledge never implies human access.
    bram_fact, _ = create_fact_authoritative(
        db, camp.id, 6, content="Unrelated secret.",
        epistemic_state="confirmed", visibility="private",
        operation_id="op-other-secret",
    )
    assert may_user_receive(db, camp, "fact", bram_fact.id, alice) == {
        "allowed": False, "reason": "private_requires_grant",
    }


# ── Arbitrary subsets + owner denial ────────────────────────────────────────

def test_arbitrary_subset_visibility_and_owner_denial():
    Fac, camp, owner, alice, bob, carol = _setup()
    db = Fac()
    _, _, aria, _, _ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The conspirators meet at midnight.",
        epistemic_state="confirmed", visibility="private",
        operation_id="op-conspiracy",
    )
    grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=alice, granted_by=owner, operation_id="op-g-a",
    )
    grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=bob, granted_by=owner, operation_id="op-g-b",
    )
    assert may_user_receive(db, camp, "fact", fact.id, alice)["allowed"] is True
    assert may_user_receive(db, camp, "fact", fact.id, bob)["allowed"] is True
    # Non-grantees denied — including the campaign owner (no implicit access).
    assert may_user_receive(db, camp, "fact", fact.id, carol) == {
        "allowed": False, "reason": "private_requires_grant",
    }
    assert may_user_receive(db, camp, "fact", fact.id, owner) == {
        "allowed": False, "reason": "private_requires_grant",
    }
    # Projection counts without leaking content.
    proj = project_facts_for_user(db, camp, carol, [fact])
    assert proj == {
        "records": [], "total": 1, "visible": 0, "denied": 1,
        "denied_reasons": {"private_requires_grant": 1},
    }
    # Grants are explicit rows, not a boolean.
    assert len(list_active_grants(db, camp.id, "fact", fact.id)) == 2
    # Aria's fictional knowledge is untouched by human grants.
    assert what_does_subject_know(db, camp, aria.id, owner)["total"] == 0


# ── Fail-closed projections ─────────────────────────────────────────────────

def test_fail_closed_on_ambiguous_and_missing_visibility():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    mara, *_ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="Ambiguous record.",
        visibility="campaign", operation_id="op-ambig",
    )
    # Corrupt visibility at the row level (bypasses validators) → deny.
    fact.visibility = ""
    db.flush()
    assert may_user_receive(db, camp, "fact", fact.id, alice) == {
        "allowed": False, "reason": "ambiguous_visibility",
    }
    assert may_user_receive(db, camp, "fact", fact.id, owner) == {
        "allowed": False, "reason": "ambiguous_visibility",
    }
    # Unknown record → deny, never raise to the viewer.
    assert may_user_receive(db, camp, "fact", uuid.uuid4(), alice) == {
        "allowed": False, "reason": "record_not_found",
    }
    # Non-member → deny even for member-visible records.
    outsider = uuid.uuid4()
    db.add(Profile(id=outsider, email="outsider@example.com"))
    db.flush()
    fact.visibility = "campaign"
    db.flush()
    assert may_user_receive(db, camp, "fact", fact.id, outsider) == {
        "allowed": False, "reason": "not_campaign_member",
    }
    # Unknown visibility string on knowledge writes is rejected, not stored.
    with pytest.raises(ValueError):
        assert_knowledge_inline(
            db, camp, subject_kind="npc", subject_entity_id=mara.id,
            target_kind="fact", target_fact_id=fact.id,
            knowledge_state="suspects", visibility="everyone-ish",
            operation_id="op-k-badvis",
        )
    db.rollback()


# ── Failure / recovery ──────────────────────────────────────────────────────

def test_knowledge_update_failure_leaves_truth_intact():
    Fac, camp, owner, *_ = _setup()
    db = Fac()
    mara, guild, _, _, _ = _entities(db, camp, 0)
    rel, _ = create_relation_authoritative(
        db, camp.id, 5, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-truth",
    )
    bogus = uuid.uuid4()
    with pytest.raises(ValueError):
        assert_knowledge_inline(
            db, camp, subject_kind="npc", subject_entity_id=bogus,
            target_kind="relation", target_relation_id=rel.id,
            knowledge_state="knows", operation_id="op-k-bad",
        )
    db.rollback()
    assert db.get(type(rel), rel.id).epistemic_state == "confirmed"
    assert db.get(type(rel), rel.id).status == "active"
    assert list_active_grants(db, camp.id, "relation", rel.id) == []
    # Cross-campaign target reference fails closed too.
    other = Campaign(id=uuid.uuid4(), owner_id=owner, name="Other", revision=0)
    db.add(other)
    db.flush()
    outsider, _ = create_entity_authoritative(
        db, other.id, 0, entity_type="npc", name="Outsider",
        operation_id="op-outsider-211",
    )
    with pytest.raises(ValueError):
        assert_knowledge_inline(
            db, camp, subject_kind="npc", subject_entity_id=mara.id,
            target_kind="entity", target_entity_id=outsider.id,
            knowledge_state="knows", operation_id="op-k-foreign",
        )
    db.rollback()


def test_revoke_affects_future_reads_without_deleting_provenance():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    _, _, aria, _, _ = _entities(db, camp, 0)
    fact, _ = create_fact_authoritative(
        db, camp.id, 5, content="The password is 'moth'.",
        epistemic_state="confirmed", visibility="private",
        operation_id="op-password",
    )
    grant, created = grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=alice, granted_by=owner, operation_id="op-g-pw",
    )
    assert created is True
    assert may_user_receive(db, camp, "fact", fact.id, alice)["allowed"] is True
    assert revoke_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=alice, operation_id="op-r-pw",
    ) is True
    # Future reads deny...
    assert may_user_receive(db, camp, "fact", fact.id, alice) == {
        "allowed": False, "reason": "private_requires_grant",
    }
    # ...while the revoked row stays durable provenance (not deleted).
    rows = db.execute(select(WorldVisibilityGrant)).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is not None
    assert has_active_grant(db, camp.id, "fact", fact.id, alice) is False
    assert list_active_grants(db, camp.id, "fact", fact.id) == []
    # Duplicate grant after revoke is a no-op returning the existing row.
    again, created2 = grant_visibility_inline(
        db, camp, target_kind="fact", target_id=fact.id,
        grantee_user_id=alice, granted_by=owner, operation_id="op-g-pw-2",
    )
    assert created2 is True  # new grant row: history preserved, access restored
    assert may_user_receive(db, camp, "fact", fact.id, alice)["allowed"] is True
    assert len(db.execute(select(WorldVisibilityGrant)).scalars().all()) == 2
    # Knowledge rows are independent of the grant lifecycle.
    assert what_does_subject_know(db, camp, aria.id, owner)["total"] == 0
    assert grant.to_dict()["grantee_user_id"] == str(alice)


def test_no_access_inferred_from_related_shared_records():
    Fac, camp, owner, alice, *_ = _setup()
    db = Fac()
    mara, guild, aria, _, _ = _entities(db, camp, 0)
    open_rel, _ = create_relation_authoritative(
        db, camp.id, 5, subject_entity_id=mara.id, relation_type="knows",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-open-rel",
    )
    secret_fact, _ = create_fact_authoritative(
        db, camp.id, 6, content="Mara's handler is the Guildmaster.",
        entity_refs=[mara.id, guild.id], epistemic_state="confirmed",
        visibility="private", operation_id="op-handler-secret",
    )
    # Aria fictionally knows the secret; Alice may read the open relation.
    assert_knowledge_inline(
        db, camp, subject_kind="character", subject_entity_id=aria.id,
        target_kind="fact", target_fact_id=secret_fact.id,
        knowledge_state="knows", acquisition_source="overheard",
        visibility="campaign", operation_id="op-k-aria-secret",
    )
    assert may_user_receive(db, camp, "relation", open_rel.id, alice)["allowed"] is True
    # Neither the shared relation nor the visible knowledge row grants the secret.
    assert may_user_receive(db, camp, "fact", secret_fact.id, alice) == {
        "allowed": False, "reason": "private_requires_grant",
    }
    proj = project_relations_for_user(db, camp, alice, [open_rel])
    assert proj["visible"] == 1
    who = who_knows_target(db, camp, "fact", secret_fact.id, alice)
    assert who["knowers"] == []  # target itself not visible → knowers hidden
    assert who["denied_reasons"] == {"target_not_visible": 1}

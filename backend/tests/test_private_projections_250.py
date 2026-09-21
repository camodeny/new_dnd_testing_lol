"""Issue #250 — per-player projections for secret knowledge, maps, items, shops, clues, state."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.realtime.service import (  # noqa: E402
    InMemoryRealtimePublisher,
    build_projection_invalidated_event,
    publish_projection_invalidated,
    publish_projection_invalidated_for_grantee,
    set_realtime_publisher,
)
from app.snapshot.surfaces import build_surfaces_for_viewer  # noqa: E402
from app.world import clocks as _clocks  # noqa: E402
from app.world import epistemics as _epistemics  # noqa: E402
from app.world import knowledge as _knowledge  # noqa: E402
from app.world import service as _world  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402
from models.world import WorldVisibilityGrant  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture
def ctx():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Profile(id=bob, email="bob@example.com"),
    ])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Projections", revision=0)
    db.add(camp)
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=camp.id, user_id=alice, role="player"),
        CampaignMember(campaign_id=camp.id, user_id=bob, role="player"),
    ])
    thread = CampaignThread(
        id=uuid.uuid4(), campaign_id=camp.id, thread_type="campaign",
        title="Campaign", created_by=owner,
    )
    db.add(thread)
    db.commit()
    yield {"factory": Fac, "campaign_id": camp.id, "owner": owner,
           "alice": alice, "bob": bob, "thread_id": thread.id}
    db.close()


def _db(ctx):
    return ctx["factory"]()


def _seed_private_fact(ctx, content="the vault sigil is a moth"):
    """One campaign-visible decoy + one private secret granted to Alice only."""
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        decoy, _ = _knowledge.create_fact_inline(
            db, camp, content="the tavern serves stew",
            visibility="campaign", operation_id="op-decoy-250",
        )
        secret, _ = _knowledge.create_fact_inline(
            db, camp, content=content,
            visibility="private", operation_id="op-secret-250",
        )
        dm_only, _ = _knowledge.create_fact_inline(
            db, camp, content="the DM tracks a hidden omen",
            visibility="dm_only", operation_id="op-omen-250",
        )
        _epistemics.grant_visibility_inline(
            db, camp, target_kind="fact", target_id=secret.id,
            grantee_user_id=ctx["alice"], granted_by=ctx["owner"],
            operation_id="op-grant-250",
        )
        db.commit()
        return decoy.id, secret.id
    finally:
        db.close()


def test_two_players_receive_different_projections(ctx):
    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        alice_texts = {r["content"] for r in alice_view["clues"]["records"]}
        bob_texts = {r["content"] for r in bob_view["clues"]["records"]}
        assert "the vault sigil is a moth" in alice_texts
        assert "the tavern serves stew" in alice_texts
        assert "the tavern serves stew" in bob_texts
        assert "the vault sigil is a moth" not in bob_texts
        # Member payload carries no denied metadata to infer hidden counts.
        assert "denied" not in bob_view["clues"]
        assert "denied_reasons" not in bob_view["clues"]
        assert "total" not in bob_view["clues"]
    finally:
        db.close()


def test_hidden_data_absent_not_masked(ctx):
    _, secret_id = _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        blob = str(bob_view["clues"]) + str(bob_view["knowledge"])
        assert "moth" not in blob
        assert str(secret_id) not in blob
        # No denied metadata at all: counts/classes of hidden records
        # cannot be inferred from an unauthorized payload.
        assert "denied" not in blob
        assert "private_requires_grant" not in blob
        assert "dm_only_requires_authority" not in blob
    finally:
        db.close()


def test_visible_record_fields_survive_member_sanitization(ctx):
    """Review #418 round 5: envelope stripping must not corrupt visible
    records — a legitimate `total` inside item details survives."""
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        _world.create_entity_inline(
            db, camp, entity_type="item", name="Visible Rope",
            visibility="campaign",
            details={"total": 5, "length_ft": 50, "denied": "no entry here"},
            operation_id="op-rope-250",
        )
        db.commit()
        view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert view["items"]["visible"] == 1
        record = view["items"]["records"][0]
        assert record["details"] == {"total": 5, "length_ft": 50, "denied": "no entry here"}
        # ...while the envelope itself carries no denied metadata.
        assert "denied" not in view["items"]
        assert "denied_reasons" not in view["items"]
        assert "total" not in view["items"]
    finally:
        db.close()


def test_hidden_records_do_not_change_unauthorized_serialized_view(ctx):
    """Review #418 round 4: adding hidden records must not change an
    unauthorized viewer's serialized surfaces — no count/class inference."""
    import json

    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        before = json.dumps(
            build_surfaces_for_viewer(db, camp, ctx["bob"]), sort_keys=True, default=str,
        )
        # Add more secrets Bob cannot see: private fact, private item, secret clock.
        secret, _ = _knowledge.create_fact_inline(
            db, camp, content="a second moth sigil",
            visibility="private", operation_id="op-secret2-250",
        )
        dagger, _ = _world.create_entity_inline(
            db, camp, entity_type="item", name="Hidden Dagger",
            visibility="private", operation_id="op-dagger2-250",
        )
        _clocks.create_clock_inline(
            db, camp, name="Hidden Doom", threshold=8,
            advancement_criteria={"kind": "deterministic"}, visibility="dm_only",
            provenance={"source": "test-250"},
            operation_id="op-clock-hidden-250",
        )
        db.commit()
        after = json.dumps(
            build_surfaces_for_viewer(db, camp, ctx["bob"]), sort_keys=True, default=str,
        )
        assert before == after
        _ = (secret, dagger)
    finally:
        db.close()


def test_reveal_expands_projection(ctx):
    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        secret = _knowledge.list_facts(db, camp.id, limit=200)
        secret = [f for f in secret if f.visibility == "private"][0]
        before = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert all(r["id"] != str(secret.id) for r in before["clues"]["records"])
        _epistemics.grant_visibility_inline(
            db, camp, target_kind="fact", target_id=secret.id,
            grantee_user_id=ctx["bob"], granted_by=ctx["owner"],
            operation_id="op-reveal-250",
        )
        db.commit()
        after = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert any(r["id"] == str(secret.id) for r in after["clues"]["records"])
    finally:
        db.close()


def test_revoke_removes_future_access_preserves_audit(ctx):
    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        secret = [f for f in _knowledge.list_facts(db, camp.id, limit=200)
                  if f.visibility == "private"][0]
        assert _epistemics.revoke_visibility_inline(
            db, camp, target_kind="fact", target_id=secret.id,
            grantee_user_id=ctx["alice"], operation_id="op-revoke-250",
        ) is True
        db.commit()
        view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert all(r["id"] != str(secret.id) for r in view["clues"]["records"])
        # Audit history preserved: soft-revoked row still exists.
        rows = db.query(WorldVisibilityGrant).filter_by(
            campaign_id=camp.id, grantee_user_id=ctx["alice"]).all()
        assert rows and all(r.revoked_at is not None for r in rows)
    finally:
        db.close()


def test_shared_appearance_differs_from_hidden_reality(ctx):
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        details = {
            "flavor": "scarred veteran",
            "resources": [{"name": "signal whistle", "uses": 1}],
            "rules_state_visibility": {"resources": "dm_private"},
        }
        row, _ = _world.create_entity_inline(
            db, camp, entity_type="npc", name="Vex",
            visibility="campaign", details=details, operation_id="op-vex-250",
        )
        db.commit()
        owner_view = build_surfaces_for_viewer(db, camp, ctx["owner"])
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert owner_view["knowledge"]["visible"] >= 0  # sanity: builder ran
        # NPCs are not items/shops; assert via the entity projection path directly.
        owner_proj = _world.project_entity_for_viewer(row, True)
        member_proj = _world.project_entity_for_viewer(row, False)
        assert str(owner_proj["id"]) == str(member_proj["id"]) == str(row.id)
        assert member_proj["details"].get("resources") == []
        assert "rules_state_visibility" not in member_proj["details"]
        assert owner_proj["details"]["resources"] == [{"name": "signal whistle", "uses": 1}]
        # Same campaign-visible entity id reaches both viewers' knowledge-adjacent
        # world reads; hidden reality (full details) stays owner-only.
        _ = alice_view
    finally:
        db.close()


def test_hidden_item_and_shop_only_for_grantee(ctx):
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        dagger, _ = _world.create_entity_inline(
            db, camp, entity_type="item", name="Moon Dagger",
            visibility="private", operation_id="op-dagger-250",
        )
        shop, _ = _world.create_entity_inline(
            db, camp, entity_type="shop", name="Night Market",
            visibility="private", operation_id="op-shop-250",
        )
        _epistemics.grant_visibility_inline(
            db, camp, target_kind="entity", target_id=dagger.id,
            grantee_user_id=ctx["alice"], granted_by=ctx["owner"],
            operation_id="op-grant-dagger-250",
        )
        _epistemics.grant_visibility_inline(
            db, camp, target_kind="entity", target_id=shop.id,
            grantee_user_id=ctx["alice"], granted_by=ctx["owner"],
            operation_id="op-grant-shop-250",
        )
        db.commit()
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        bob_view = build_surfaces_for_viewer(db, camp, ctx["bob"])
        assert {r["name"] for r in alice_view["items"]["records"]} == {"Moon Dagger"}
        assert {r["name"] for r in alice_view["shops"]["records"]} == {"Night Market"}
        assert alice_view["items"]["records"] and "Moon Dagger" not in str(bob_view["items"])
        assert bob_view["items"]["records"] == []
        assert bob_view["shops"]["records"] == []
        assert "Moon Dagger" not in str(bob_view) and "Night Market" not in str(bob_view)
    finally:
        db.close()


def test_reconnect_restores_same_authorized_view(ctx):
    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        first = build_surfaces_for_viewer(db, camp, ctx["alice"])
        second = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert first == second
    finally:
        db.close()


def test_owner_sees_all_and_outsider_sees_nothing(ctx):
    _seed_private_fact(ctx)
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        owner_view = build_surfaces_for_viewer(db, camp, ctx["owner"])
        owner_texts = {r["content"] for r in owner_view["clues"]["records"]}
        # Owner (DM authority) sees dm_only truth but never private grants
        # without an explicit grant — may_user_receive semantics.
        assert "the DM tracks a hidden omen" in owner_texts
        assert "the vault sigil is a moth" not in owner_texts
        outsider_view = build_surfaces_for_viewer(db, camp, uuid.uuid4())
        assert outsider_view["clues"]["records"] == []
        assert outsider_view["items"]["records"] == []
        assert outsider_view["clocks"] == {"clocks": [], "count": 0}
    finally:
        db.close()


def test_clocks_member_vs_owner(ctx):
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        _clocks.create_clock_inline(
            db, camp, name="City Alarm", threshold=4,
            advancement_criteria={"kind": "deterministic"}, visibility="campaign",
            completion_effect={"effect": "guards arrive"},
            provenance={"source": "test-250"},
            operation_id="op-clock-open-250",
        )
        _clocks.create_clock_inline(
            db, camp, name="Secret Ritual", threshold=6,
            advancement_criteria={"kind": "deterministic"}, visibility="dm_only",
            provenance={"source": "test-250"},
            operation_id="op-clock-secret-250",
        )
        db.commit()
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        owner_view = build_surfaces_for_viewer(db, camp, ctx["owner"])
        alice_names = {c["name"] for c in alice_view["clocks"]["clocks"]}
        owner_names = {c["name"] for c in owner_view["clocks"]["clocks"]}
        assert alice_names == {"City Alarm"}
        assert owner_names == {"City Alarm", "Secret Ritual"}
        # Member projection strips DM-side internals.
        open_clock = alice_view["clocks"]["clocks"][0]
        assert "completion_effect" not in open_clock
        assert "Secret Ritual" not in str(alice_view["clocks"])
    finally:
        db.close()


def test_maps_blind_without_visible_encounter(ctx):
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert view["maps"] == {"visible": False}
    finally:
        db.close()


def test_invalidation_event_carries_no_secrets(ctx):
    """Review #418 round 2: shared-thread broadcast must be audience-neutral —
    no grantee, no target kind, no grant/revoke direction."""
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        ev = build_projection_invalidated_event(camp, thread_id=ctx["thread_id"])
        assert ev["type"] == "projection.invalidated"
        assert set(ev) == {
            "type", "event_id", "campaign_id", "thread_id",
            "revision", "timestamp", "dedupe_key",
        }
        blob = str(ev)
        assert str(ctx["alice"]) not in blob
        assert str(ctx["bob"]) not in blob
        assert "granted" not in blob and "revoked" not in blob
        assert "fact" not in blob.replace("projection-invalidated", "").replace("invalidated", "")
        assert "moth" not in blob
        # Publish path: best-effort, never raises, stays neutral.
        mem = InMemoryRealtimePublisher()
        set_realtime_publisher(mem)
        try:
            ok = publish_projection_invalidated(
                db, camp, thread_id=ctx["thread_id"],
            )
            assert ok is True
            assert len(mem.published) == 1
            payload = mem.published[0]["payload"]
            assert payload["type"] == "projection.invalidated"
            assert "grantee_user_id" not in payload
            assert "target_kind" not in payload
            assert "transition" not in payload
        finally:
            set_realtime_publisher(None)
    finally:
        db.close()


def test_invalidation_fanout_targets_grantee_threads(ctx):
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        mem = InMemoryRealtimePublisher()
        set_realtime_publisher(mem)
        try:
            sent = publish_projection_invalidated_for_grantee(
                db, camp, grantee_user_id=ctx["alice"],
            )
            assert sent >= 1
            for rec in mem.published:
                # Grantee id resolves threads only; never serialized.
                assert str(ctx["alice"]) not in str(rec["payload"])
                assert rec["payload"]["type"] == "projection.invalidated"
        finally:
            set_realtime_publisher(None)
    finally:
        db.close()


def test_authoritative_grant_and_revoke_publish_invalidation(ctx):
    """Review #418: real grant/revoke commits must emit the invalidation event."""
    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        secret, _ = _knowledge.create_fact_inline(
            db, camp, content="the vault sigil is a moth",
            visibility="private", operation_id="op-secret-auth-250",
        )
        db.commit()
        mem = InMemoryRealtimePublisher()
        set_realtime_publisher(mem)
        try:
            rev = int(db.get(Campaign, ctx["campaign_id"]).revision)
            row, _ = _epistemics.grant_visibility_authoritative(
                db, ctx["campaign_id"], rev,
                operation_id="op-auth-grant-250", actor_id=ctx["owner"],
                target_kind="fact", target_id=secret.id,
                grantee_user_id=ctx["bob"],
            )
            assert row is not None
            grants = [r for r in mem.published
                      if r["payload"]["type"] == "projection.invalidated"]
            assert len(grants) == 1
            # Audience-neutral: no grantee, kind, or direction on the wire.
            assert str(ctx["bob"]) not in str(grants[0]["payload"])
            assert "moth" not in str(grants[0]["payload"])

            rev = int(db.get(Campaign, ctx["campaign_id"]).revision)
            revoked, _ = _epistemics.revoke_visibility_authoritative(
                db, ctx["campaign_id"], rev,
                operation_id="op-auth-revoke-250", actor_id=ctx["owner"],
                target_kind="fact", target_id=secret.id,
                grantee_user_id=ctx["bob"],
            )
            assert revoked is True
            assert len([r for r in mem.published
                        if r["payload"]["type"] == "projection.invalidated"]) == 2

            # No-op revoke (nothing active) changes nothing and stays silent.
            before = len(mem.published)
            rev = int(db.get(Campaign, ctx["campaign_id"]).revision)
            revoked_again, _ = _epistemics.revoke_visibility_authoritative(
                db, ctx["campaign_id"], rev,
                operation_id="op-auth-revoke-noop-250", actor_id=ctx["owner"],
                target_kind="fact", target_id=secret.id,
                grantee_user_id=ctx["bob"],
            )
            assert revoked_again is False
            assert len(mem.published) == before
        finally:
            set_realtime_publisher(None)
    finally:
        db.close()


def test_dm_only_map_zones_absent_for_non_owner(ctx):
    """Review #418 round 3: hidden trap geometry (kind/rect/label) must be
    absent from unauthorized payloads — in surfaces AND the full snapshot."""
    from app.combat.maps import ensure_map
    from models.combat import Encounter

    db = _db(ctx)
    try:
        camp = db.get(Campaign, ctx["campaign_id"])
        enc = Encounter(
            campaign_id=camp.id, thread_id=str(ctx["thread_id"]),
            status="pending_initiative",
        )
        db.add(enc)
        db.flush()
        rev = int(db.get(Campaign, ctx["campaign_id"]).revision)
        ensure_map(
            db, enc.id, actor_id=ctx["owner"], width=6, height=6,
            terrain=[
                {"kind": "difficult",
                 "rect": {"col": 0, "row": 0, "width": 2, "height": 2},
                 "visibility": "public"},
                {"kind": "blocked",
                 "rect": {"col": 4, "row": 3, "width": 1, "height": 1},
                 "visibility": "dm_only", "label": "secret pit trap"},
            ],
            expected_revision=rev, operation_id="op-trapmap-250", commit=True,
        )
        alice_view = build_surfaces_for_viewer(db, camp, ctx["alice"])
        assert alice_view["maps"]["visible"] is True
        alice_zones = alice_view["maps"]["map"]["zones"]
        assert len(alice_zones) == 1
        assert alice_zones[0]["kind"] == "difficult"
        assert "secret pit trap" not in str(alice_view["maps"])
        owner_view = build_surfaces_for_viewer(db, camp, ctx["owner"])
        assert len(owner_view["maps"]["map"]["zones"]) == 2

        # Full-snapshot paths expose the same safe projection.
        from app.snapshot.service import build_live_table_snapshot

        snap = build_live_table_snapshot(db, ctx["campaign_id"], ctx["alice"])
        assert "secret pit trap" not in str(snap["surfaces"]["maps"])
        assert "secret pit trap" not in str(snap["encounter"])
        assert len(snap["surfaces"]["maps"]["map"]["zones"]) == 1
    finally:
        db.close()


def test_snapshot_wires_surfaces(ctx):
    from app.snapshot.service import build_live_table_snapshot

    db = _db(ctx)
    try:
        _seed_private_fact(ctx)
        snap = build_live_table_snapshot(db, ctx["campaign_id"], ctx["alice"])
        assert "surfaces" in snap
        surfaces = snap["surfaces"]
        for key in ("knowledge", "clues", "items", "shops", "maps", "clocks"):
            assert key in surfaces, key
        texts = {r["content"] for r in surfaces["clues"]["records"]}
        assert "the vault sigil is a moth" in texts
        bob_snap = build_live_table_snapshot(db, ctx["campaign_id"], ctx["bob"])
        bob_texts = {r["content"] for r in bob_snap["surfaces"]["clues"]["records"]}
        assert "the vault sigil is a moth" not in bob_texts
    finally:
        db.close()

"""Deterministic world fixtures for the #268 DM-quality eval.

Seeds one fixture's canon into an existing, already-started campaign in a
single revision-ordered domain event (the same shape as the #245 world seed).
Run against a disposable eval database only, never a shared one:

  POSTGRES_URL=postgresql://localhost:5432/eval268 \
    python -m scripts.eval_fixtures_268 <fixture> <campaign_id> [--grantee <user_id>]

Fixtures (see docs/evals/luna-playtest-matrix.md, "Resettable fixtures"):

  f1   identity: Mara Venn (alias "Mara"), a distinct Maren Venn, two named
       locations, and a known relation + fact.
  f1b  two distinct NPCs sharing the public name "Warden Hale" with different
       roles and locations.
  f2   epistemics: a public fact, a false player claim, an NPC who knowingly
       lies, a secret known to one PC, a DM-only fact, and a superseded fact.
       Every record carries a unique harmless marker (MK-…) so leaks are
       detectable in any transcript or payload.
  f5   encounter: Greywater Ford with two bandit stat blocks; a setup DM turn
       with a fixed start_encounter contract (no model call; NPC initiative d20
       pinned) leaves the encounter in pending_initiative with a blocked wall
       and difficult shallows. Needs a campaign whose members selected PCs.

Prints the seeded ids (the hidden oracle) as JSON. Never give that output to a
player agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import SessionLocal  # noqa: E402
from app.campaigns.events import commit_campaign_mutation  # noqa: E402
from app.world.facts import create_fact, create_relation, supersede_fact  # noqa: E402
from app.world.identity import add_alias  # noqa: E402
from app.world.knowledge import assert_knowledge, grant_visibility  # noqa: E402
from app.world.service import apply_scene_update, create_entity  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character  # noqa: E402

TAG = "eval-fixture-268"
PROVENANCE = {"source": TAG, "issue": 268}


def _pcs(db, campaign_id) -> list[Character]:
    members = db.query(CampaignMember).filter(CampaignMember.campaign_id == campaign_id).all()
    ids = [m.selected_character_id for m in members if m.selected_character_id]
    return [db.get(Character, cid) for cid in ids]


class _Seeder:
    """Thin keyed wrappers so reruns are idempotent and ids are recorded."""

    def __init__(self, db, campaign, fixture):
        self.db, self.c, self.fixture = db, campaign, fixture
        self.out: dict = {"fixture": fixture, "campaign_id": str(campaign.id)}

    def key(self, slot):
        digest = hashlib.sha1(f"{slot}:{self.c.id}".encode()).hexdigest()[:24]
        return f"{TAG}:{self.fixture}:{digest}"

    def entity(self, slot, entity_type, name, summary, details=None):
        row, _ = create_entity(
            self.db, self.c, entity_type=entity_type, name=name, summary=summary,
            status="active", visibility="campaign", details={"seed": TAG, **(details or {})},
            operation_id=self.key(slot), idempotency_key=self.key(slot),
        )
        self.out[slot] = str(row.id)
        return row

    def fact(self, slot, content, *, refs, state="confirmed", visibility="campaign", details=None):
        row, _ = create_fact(
            self.db, self.c, content=content, entity_refs=[r.id for r in refs],
            epistemic_state=state, visibility=visibility, provenance=PROVENANCE,
            details=details, operation_id=self.key(slot), idempotency_key=self.key(slot),
        )
        self.out[slot] = str(row.id)
        return row

    def relation(self, slot, subject, relation_type, obj, visibility="campaign"):
        row, _ = create_relation(
            self.db, self.c, subject_entity_id=subject.id, relation_type=relation_type,
            object_entity_id=obj.id, epistemic_state="confirmed", visibility=visibility,
            provenance=PROVENANCE, operation_id=self.key(slot), idempotency_key=self.key(slot),
        )
        self.out[slot] = str(row.id)
        return row

    def knows(self, slot, subject, *, subject_kind, target_kind="fact", target, state="knows",
              visibility="campaign", source="fixture"):
        target_arg = {"fact": "target_fact_id", "relation": "target_relation_id",
                      "entity": "target_entity_id"}[target_kind]
        row, _ = assert_knowledge(
            self.db, self.c, subject_kind=subject_kind, subject_entity_id=subject.id,
            target_kind=target_kind, **{target_arg: target.id}, knowledge_state=state,
            acquisition_source=source, visibility=visibility, provenance=PROVENANCE,
            operation_id=self.key(slot), idempotency_key=self.key(slot),
        )
        return row

    def pc_entities(self, location):
        rows = []
        for pc in _pcs(self.db, self.c.id):
            rows.append(self.entity(
                f"pc:{pc.id}", "character", pc.name,
                f"Player character currently at {location.name}.",
                details={"character_id": str(pc.id)},
            ))
        return rows

    def scene(self, new_revision, location, actors, pcs, time_of_day, premise):
        apply_scene_update(
            self.db, self.c, new_revision=new_revision, location_entity_id=location.id,
            location_name=location.name, fictional_time=time_of_day,
            present_actors=[{"entity_id": str(a.id), "name": a.name, "kind": "npc", "role": role}
                            for a, role in actors] + [{"name": p.name, "kind": "pc"} for p in pcs],
            environment={"premise": premise, "seed": TAG}, visibility="campaign",
            operation_id=self.key("scene"),
        )


def seed_f1(s: _Seeder, new_revision: int, **_):
    landing = s.entity("loc_landing", "location", "Ferrymarsh Landing",
                       "A reed-choked river landing with a toll hut and a single night ferry.")
    saltgate = s.entity("loc_saltgate", "location", "Saltgate",
                        "A walled salt-trading town across the river from Ferrymarsh Landing.")
    mara = s.entity("npc_mara", "npc", "Mara Venn",
                    "Weathered ferrywoman who runs the night ferry from Ferrymarsh Landing to Saltgate.",
                    details={"role": "ferrywoman", "location_ref": "Ferrymarsh Landing"})
    add_alias(s.db, mara, "Mara", provenance=PROVENANCE)
    maren = s.entity("npc_maren", "npc", "Maren Venn",
                     "Mara Venn's younger cousin; the fussy toll clerk in the landing's toll hut.",
                     details={"role": "toll clerk", "location_ref": "Ferrymarsh Landing"})
    pcs = s.pc_entities(landing)
    s.relation("rel_cousins", maren, "cousin_of", mara)
    s.relation("rel_mara_at", mara, "present_at", landing)
    s.relation("rel_maren_at", maren, "present_at", landing)
    ferry = s.fact("fact_ferry", "Mara Venn runs the only night ferry from Ferrymarsh Landing to "
                   "Saltgate; the crossing costs two silver.", refs=[mara, landing, saltgate])
    toll = s.fact("fact_toll", "Maren Venn collects the landing toll and keeps a ledger of every "
                  "traveller who crosses.", refs=[maren, landing])
    for pc in pcs:
        s.knows(f"pc_ferry:{pc.id}", pc, subject_kind="character", target=ferry)
        s.knows(f"pc_toll:{pc.id}", pc, subject_kind="character", target=toll)
    for npc in (mara, maren):
        for fact in (ferry, toll):
            s.knows(f"npc:{npc.id}:{fact.id}", npc, subject_kind="npc", target=fact)
        for ent in (mara, maren, landing, saltgate):
            if ent.id != npc.id:
                s.knows(f"npcent:{npc.id}:{ent.id}", npc, subject_kind="npc", target_kind="entity",
                        target=ent)
    s.scene(new_revision, landing, [(mara, "ferrywoman"), (maren, "toll clerk")], pcs, "dusk",
            "Travellers wait at Ferrymarsh Landing for the night ferry to Saltgate.")


def seed_f1b(s: _Seeder, new_revision: int, **_):
    gate = s.entity("loc_north_gate", "location", "North Gate",
                    "The fortified northern gate of Brackenford, with a guard post and a portcullis.")
    yard = s.entity("loc_chapel_yard", "location", "Chapel Yard",
                    "The walled cemetery behind Brackenford's chapel, beside the North Gate road.")
    hale_gate = s.entity("npc_hale_gate", "npc", "Warden Hale",
                         "Gate warden of Brackenford's North Gate: a stern, armoured veteran who checks "
                         "travel papers.", details={"role": "gate warden", "location_ref": "North Gate"})
    hale_yard = s.entity("npc_hale_yard", "npc", "Warden Hale",
                         "Cemetery warden of the Chapel Yard: an elderly gravekeeper with a lantern and "
                         "a ring of crypt keys.", details={"role": "cemetery warden",
                                                           "location_ref": "Chapel Yard"})
    pcs = s.pc_entities(gate)
    s.relation("rel_gate_hale", hale_gate, "present_at", gate)
    s.relation("rel_yard_hale", hale_yard, "present_at", yard)
    for npc in (hale_gate, hale_yard):
        for ent in (gate, yard):
            s.knows(f"npcent:{npc.id}:{ent.id}", npc, subject_kind="npc", target_kind="entity", target=ent)
    s.scene(new_revision, gate, [(hale_gate, "gate warden"), (hale_yard, "cemetery warden")], pcs,
            "late afternoon",
            "At Brackenford's North Gate. The cemetery warden has come over from the Chapel Yard to "
            "argue with the gate warden about a missing crypt key.")


def seed_f2(s: _Seeder, new_revision: int, grantee=None, **_):
    mill = s.entity("loc_mill", "location", "Harrow Mill",
                    "A village of millers and grain barges on the Harrow river; its granary burned "
                    "three nights ago.")
    osric = s.entity("npc_osric", "npc", "Osric Thale",
                     "Genial innkeeper of the Millstone Inn, eager to talk about the granary fire.",
                     details={"role": "innkeeper"})
    mayor = s.entity("npc_mayor", "npc", "Mayor Aldwen Cray",
                     "Harrow Mill's careful, soft-spoken mayor.", details={"role": "mayor"})
    pcs = s.pc_entities(mill)
    public = s.fact("fact_public", "The granary at Harrow Mill burned three nights ago; the mill "
                    "bell was rung to raise the alarm. [MK-PUB-17]", refs=[mill])
    claim = s.fact("fact_player_claim", "The mayor set the granary fire. [MK-CLAIM-23]",
                   refs=[mayor, mill], state="false",
                   details={"claimed_by": "player character", "marker": "MK-CLAIM-23"})
    truth = s.fact("fact_osric_truth", "Osric Thale set the granary fire himself to hide that he had "
                   "been selling the village's stored grain. [MK-DM-TRUTH-42]",
                   refs=[osric, mill], visibility="dm_only")
    lie = s.fact("fact_osric_lie", "Raiders from the eastern hills set the granary fire. [MK-LIE-31]",
                 refs=[mill], state="false")
    dm_only = s.fact("fact_dm_only", "The mayor is secretly paying the river barge guild to delay "
                     "grain shipments. [MK-DM-99]", refs=[mayor], visibility="dm_only")
    secret = s.fact("fact_pc_secret", "On the night of the fire, Tamsin saw a hooded figure carrying a "
                    "brass lantern leave the granary by the river door. [MK-PRIV-08]",
                    refs=[mill], visibility="private")
    old = s.fact("fact_bridge_v1", "The Harrow footbridge is open and safe to cross. [MK-OLD-55]",
                 refs=[mill], state="believed")
    new = supersede_fact(
        s.db, s.c, old.id, content="The Harrow footbridge collapsed in yesterday's flood and is "
        "closed. [MK-NEW-56]", epistemic_state="believed", provenance=PROVENANCE,
        operation_id=s.key("fact_bridge_v2"), idempotency_key=s.key("fact_bridge_v2"),
    )[0]
    s.out["fact_bridge_v2"] = str(new.id)
    for pc in pcs:
        s.knows(f"pc_public:{pc.id}", pc, subject_kind="character", target=public)
        s.knows(f"pc_claim:{pc.id}", pc, subject_kind="character", target=claim, state="claims",
                source="player_claim")
        s.knows(f"pc_bridge:{pc.id}", pc, subject_kind="character", target=new)
    if grantee and pcs:
        s.knows(f"pc_secret:{pcs[0].id}", pcs[0], subject_kind="character", target=secret,
                visibility="private")
        grant_visibility(s.db, s.c, target_kind="fact", target_id=secret.id, grantee_user_id=grantee,
                         operation_id=s.key("grant_secret"), idempotency_key=s.key("grant_secret"))
    s.knows("osric_truth", osric, subject_kind="npc", target=truth, visibility="dm_only")
    s.knows("osric_lie", osric, subject_kind="npc", target=lie, state="claims", visibility="dm_only")
    s.knows("osric_public", osric, subject_kind="npc", target=public)
    s.knows("osric_mayor", osric, subject_kind="npc", target_kind="entity", target=mayor)
    s.knows("mayor_public", mayor, subject_kind="npc", target=public)
    s.knows("mayor_dm", mayor, subject_kind="npc", target=dm_only, visibility="dm_only")
    s.knows("mayor_osric", mayor, subject_kind="npc", target_kind="entity", target=osric)
    s.scene(new_revision, mill, [(osric, "innkeeper"), (mayor, "mayor")], pcs, "evening",
            "In the common room of the Millstone Inn at Harrow Mill, three nights after the granary "
            "fire. The innkeeper and the mayor are both present.")


def seed_f5(s: _Seeder, new_revision: int, **_):
    from app.rules.bestiary import get_stat_block, stat_block_details

    ford = s.entity("loc_ford", "location", "Greywater Ford",
                    "A shallow, rocky river ford with a ruined stone wall on the near bank.")
    bandits = [
        s.entity(f"npc_bandit_{i}", "npc", name, summary,
                 details=stat_block_details(get_stat_block("bandit")))
        for i, (name, summary) in enumerate([
            ("Scarred Bandit", "A scarred highway bandit with a scimitar, blocking the ford."),
            ("Bandit Archer", "A wiry bandit with a light crossbow, crouched behind the ruined wall."),
        ])
    ]
    pcs = s.pc_entities(ford)
    s.scene(new_revision, ford, [(b, "bandit") for b in bandits], pcs, "midday",
            "Two bandits ambush the party at Greywater Ford.")


def start_f5_encounter(db, campaign_id, oracle):
    """Run the setup DM turn with a fixed start_encounter contract (no model).

    Map: 12x10 grid; a ruined wall (blocked) at col 6 rows 0-5; difficult
    river shallows at cols 0-2 rows 7-9. NPC initiative d20 pinned to 10.
    """
    from unittest import mock

    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    from app.dm.execution import execute_dm_attempt
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    campaign = db.get(Campaign, campaign_id)
    members = db.query(CampaignMember).filter(CampaignMember.campaign_id == campaign_id).all()
    owner = next(m for m in members if m.user_id == campaign.owner_id)
    from models.threads import CampaignThread
    thread = db.query(CampaignThread).filter(CampaignThread.campaign_id == campaign_id,
                                             CampaignThread.thread_type == "campaign").one()
    text = "We wade toward the ford."
    accept_submission(db, campaign_id=campaign_id, user_id=owner.user_id,
                      character_id=owner.selected_character_id, raw_content=text,
                      segments=[{"type": "ic", "text": text}], thread_id=str(thread.id))
    db.commit()
    turn, attempt = coordinate_turn(db, campaign_id, str(thread.id), commit=False)
    db.commit()
    participants = [{"character_id": str(m.selected_character_id)} for m in members]
    participants += [{"npc_entity_id": oracle["npc_bandit_0"]}, {"npc_entity_id": oracle["npc_bandit_1"]}]
    contract = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "bandit ambush (fixture)",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "Two bandits rise from behind the ruined wall at Greywater Ford, weapons drawn.",
            "claim_kind": "observation", "origin": "dm_adjudication", "visibility": "public"}]}],
        "staged_effects": [{"id": "fixture_start_encounter", "effect_type": "start_encounter",
                            "arguments": {
                                "participants": participants,
                                "scene": {"location_entity_id": oracle["loc_ford"],
                                          "location_name": "Greywater Ford"},
                                "map": {"width": 12, "height": 10, "terrain": [
                                    {"kind": "blocked", "label": "Ruined wall",
                                     "rect": {"col": 6, "row": 0, "width": 1, "height": 6}},
                                    {"kind": "difficult", "label": "River shallows", "cost_multiplier": 2,
                                     "rect": {"col": 0, "row": 7, "width": 3, "height": 3}}]}}}],
    })
    with mock.patch("app.combat.service.secrets.randbelow", return_value=9):
        execute_dm_attempt(db, attempt.id, adjudicate=lambda packet, feedback=None: contract,
                           narrator="deterministic")
    db.commit()
    return {"setup_turn_id": str(turn.id)}


FIXTURES = {"f1": seed_f1, "f1b": seed_f1b, "f2": seed_f2, "f5": seed_f5}
POST_SEED = {"f5": start_f5_encounter}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("fixture", choices=sorted(FIXTURES))
    p.add_argument("campaign_id")
    p.add_argument("--grantee", help="user id granted the f2 private secret (the PC's player)")
    args = p.parse_args()
    os.environ.setdefault("POST_TURN_AUTO_TRIGGER", "0")
    db = SessionLocal()
    campaign = db.get(Campaign, uuid.UUID(args.campaign_id))
    seeder = None

    def mutate(locked):
        nonlocal seeder
        seeder = _Seeder(db, locked, args.fixture)
        FIXTURES[args.fixture](seeder, int(locked.revision) + 1, grantee=args.grantee)

    commit_campaign_mutation(
        db, campaign.id, int(campaign.revision), event_type="fixture.seeded",
        payload_builder=lambda: {"fixture": args.fixture, "seed": TAG},
        operation_id=f"{TAG}:{args.fixture}:{campaign.id}",
        targets={"campaign_id": str(campaign.id)}, visibility="dm_only",
        provenance=PROVENANCE, mutate=mutate, commit=True,
    )
    if args.fixture in POST_SEED:
        seeder.out.update(POST_SEED[args.fixture](db, campaign.id, seeder.out))
    print(json.dumps(seeder.out, indent=1))


if __name__ == "__main__":
    main()

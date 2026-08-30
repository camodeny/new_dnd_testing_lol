"""Issue #201 — dm_turn_contract_v1 fixture tests.

Covers acceptance criteria:
- strict validation (malformed/unknown fields fail)
- all explicit modes
- NPC utterance independent from private truth
- player declarations distinguishable from world facts
- existing vs temp entity refs structurally distinct
- no generic SQL mutation
- typed roll / evidence / staged effect / new entity
- mixed IC/OOC input identity preserved
- open choice & narration hints separate from facts
- version/normalize/serialize/ projection idempotent
"""
import json
import pytest
from app.dm.contract import (
    CONTRACT_VERSION,
    ContractValidationError,
    normalize_contract,
    public_projection,
    contract_json_schema,
)


def _base_beat_claim(text="You see a door.", kind="observation", origin="established_state", **over):
    base = {"text": text, "claim_kind": kind, "origin": origin, "visibility": "public"}
    base.update(over)
    return base


def _respond(**over):
    payload = {
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "story continuation",
        "beats": [
            {
                "id": "beat_1",
                "type": "narration",
                "claims": [_base_beat_claim()],
            }
        ],
    }
    payload.update(over)
    return payload


def test_contract_version_constant():
    assert CONTRACT_VERSION == "dm_turn_contract_v1"
    schema = contract_json_schema()
    assert "contract_version" in schema.get("properties", {})


def test_strict_unknown_fields_fail():
    with pytest.raises(ContractValidationError) as ei:
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "x", "beats": [], "unknown_field": 123,
        })
    assert ei.value.code in ("unknown_field", "contract_validation_failed")


def test_strict_unknown_field_on_claim():
    with pytest.raises(ContractValidationError):
        normalize_contract(_respond(beats=[{
            "id": "beat_1", "type": "narration",
            "claims": [{**_base_beat_claim(), "bad": 1}],
        }]))


def test_strict_unknown_field_on_top():
    with pytest.raises(ContractValidationError):
        normalize_contract({**_respond(), "sql": "DROP TABLE"})


def test_all_modes_fixture():
    # respond
    c = normalize_contract(_respond(open_player_choice="What do you do?", narration_hints={"max_words": 120}))
    assert c.mode == "respond"
    # await_roll
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "uncertain",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim(kind="roll_instruction", origin="dm_adjudication")]}],
        "roll_request": {"request_id": "roll_1", "roll_kind": "check", "ability_or_skill": "Perception", "label": "Perception check", "advantage_state": "normal", "reason_public": "Spot trap", "dc_private": 14},
    })
    assert c.mode == "await_roll"
    assert c.roll_request.dc_private == 14
    # need_evidence
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "need_evidence", "reason": "need sheet", "beats": [],
        "evidence_requests": [{"id": "evidence_1", "tool": "ask_character_sheet", "question": "What is AC?", "scope": "current_player"}],
        "safe_prelude": "Checking sheet...",
    })
    assert c.mode == "need_evidence"
    # clarify
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "clarify", "reason": "ambiguous", "beats": [],
        "clarify_question": "Which door?",
        "open_player_choice": "Which door do you mean?",
    })
    assert c.mode == "clarify"
    # table_chat
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "table_chat", "reason": "ooc", "beats": [],
        "table_chat_intent": "Brief warm answer, no story advance.",
    })
    assert c.mode == "table_chat"
    # silent
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "no output", "beats": []})
    assert c.mode == "silent"
    # unsupported
    c = normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "unsupported", "reason": "combat unsupported", "beats": []})
    assert c.mode == "unsupported"


def test_npc_utterance_private_truth_independent():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "deception",
        "beats": [{
            "id": "beat_1", "type": "npc_dialogue",
            "speaker_ref": {"type": "npc", "id": "npc:reeve_marlen"}, "speaker_public_name": "Reeve Marlen",
            "claims": [{"text": "The writ costs one gold piece.", "claim_kind": "npc_utterance", "actor_ref": {"type": "npc", "id": "npc:reeve_marlen"}, "origin": "dm_adjudication"}],
            "truth_status": "deceptive", "dm_private_context": "He is lying; true cost is two gold.",
        }],
    })
    assert c.beats[0].dm_private_context.startswith("He is lying")
    assert c.beats[0].claims[0].text == "The writ costs one gold piece."
    proj = public_projection(c)
    assert "dm_private_context" not in json.dumps(proj)
    assert proj["beats"][0].get("truth_status") is None
    # missing private context for deceptive must fail
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{
                "id": "beat_1", "type": "npc_dialogue",
                "speaker_ref": {"type": "npc", "id": "npc:reeve_marlen"}, "speaker_public_name": "Reeve Marlen",
                "claims": [{"text": "hi", "claim_kind": "npc_utterance", "actor_ref": {"type": "npc", "id": "npc:reeve_marlen"}, "origin": "dm_adjudication"}],
                "truth_status": "deceptive", "dm_private_context": "",
            }],
        })


def test_player_declaration_distinguishable_from_world_fact():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "pc agency",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [
                {"text": "Elara declares she inspects the seal.", "claim_kind": "player_declaration", "actor_ref": {"type": "character", "id": "char:elara"}, "origin": "player_transcript", "evidence_refs": ["session_message:12"]},
                {"text": "The seal bears a crack.", "claim_kind": "world_fact", "origin": "established_state"},
            ],
        }],
    })
    assert c.beats[0].claims[0].claim_kind == "player_declaration"
    assert c.beats[0].claims[1].claim_kind == "world_fact"
    # world_fact with player_transcript origin is allowed but claim_kind still distinguishes
    # player_declaration must have character actor
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "hi", "claim_kind": "player_declaration", "actor_ref": {"type": "npc", "id": "npc:1"}, "origin": "player_transcript", "evidence_refs": ["session_message:1"]}],
            }],
        })


def test_existing_vs_temp_entity_refs_structurally_distinct():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "new npc",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
        "new_entities": [{"temp_id": "tmp_npc_1", "kind": "npc", "public_name": "a dock clerk"}],
    })
    assert c.new_entities[0].temp_id == "tmp_npc_1"
    # also accepts new_npc_ legacy prefix
    c2 = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
        "new_entities": [{"temp_id": "new_npc_1", "kind": "npc", "public_name": "keeper"}],
    })
    assert c2.new_entities[0].temp_id == "new_npc_1"
    # Existing EntityRef must not use temp prefix
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "hi", "claim_kind": "observation", "origin": "established_state", "actor_ref": {"type": "npc", "id": "tmp_npc_1"}}]}],
        })
    # New entity bad pattern
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
            "new_entities": [{"temp_id": "bad-id", "kind": "npc", "public_name": "Foo"}],
        })


def test_no_generic_sql_mutation_capability():
    # attempt to sneak SQL via staged effect args
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
            "staged_effects": [{"id": "effect_1", "effect_type": "record_world_event", "arguments": {"event_type": "x", "summary": "hi", "payload": {"sql": "DROP TABLE campaigns;--"}}}],
        })
    # unknown effect type must fail (no exec_sql)
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
            "staged_effects": [{"id": "effect_1", "effect_type": "exec_sql", "arguments": {"sql": "select"}}],
        })


def test_typed_staged_effects_ok():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
        "staged_effects": [
            {"id": "effect_1", "effect_type": "record_world_event", "arguments": {"event_type": "clue_found", "summary": "Seal examined", "visibility": "public"}},
            {"id": "effect_2", "effect_type": "reveal_fact", "arguments": {"item_type": "fact", "item_id": "fact:seal_crack", "visibility": "public", "reason": "found"}},
            {"id": "effect_3", "effect_type": "propose_sheet_update", "arguments": {"character_id": "char:1", "reason": "pay", "changes": [{"field": "gp", "operation": "subtract", "value": 1}]}},
        ],
    })
    assert len(c.staged_effects) == 3
    # player trap: claim with invented id vs staged reveal — both typed, but contract validates shape


def test_typed_roll_request():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "check",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{"text": "Make a check.", "claim_kind": "roll_instruction", "origin": "dm_adjudication"}]}],
        "roll_request": {"request_id": "roll_1", "roll_kind": "check", "ability_or_skill": "Investigation", "label": "Investigation check", "advantage_state": "normal", "reason_public": "Inspect seal", "dc_private": 14},
    })
    assert c.roll_request.dc_private == 14
    proj = public_projection(c)
    assert "dc_private" not in json.dumps(proj)


def test_typed_evidence_request():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "need_evidence", "reason": "need",
        "beats": [], "evidence_requests": [{"id": "evidence_1", "tool": "search_campaign_memory", "query": "seal"}],
        "safe_prelude": "Searching...",
    })
    assert c.evidence_requests[0].tool == "search_campaign_memory"
    # unsafe tool must fail
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "need_evidence", "reason": "x", "beats": [],
            "evidence_requests": [{"id": "evidence_1", "tool": "roll_dice", "question": "hi", "scope": "current_player"}],
            "safe_prelude": "hi",
        })


def test_mixed_ic_ooc_input_identity_preserved():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()]}],
        "adjudication_input": {
            "submission_ids": ["sub_1"],
            "segments": [
                {"position": 0, "segment_type": "ic", "text": "I draw my sword"},
                {"position": 1, "segment_type": "ooc", "text": "(does this provoke?)"},
            ],
        },
    })
    assert c.adjudication_input.segments[0].segment_type == "ic"
    assert c.adjudication_input.segments[1].segment_type == "ooc"
    assert c.adjudication_input.segments[0].text == "I draw my sword"


def test_open_choice_and_narration_hints_separate_from_facts():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [_base_beat_claim()], "delivery": "ominous, brief"}],
        "open_player_choice": "Do you enter?",
        "narration_hints": {"max_words": 90, "style_guidance": "terse, second-person"},
    })
    assert c.open_player_choice == "Do you enter?"
    assert c.beats[0].delivery == "ominous, brief"
    assert c.beats[0].claims[0].text == "You see a door."
    # hints do not contain facts; facts remain in claims
    assert c.narration_hints.style_guidance == "terse, second-person"


def test_version_normalize_serialize_and_metrics():
    raw = {"contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "x", "beats": []}
    c = normalize_contract(raw)
    blob = json.dumps(c.model_dump(mode="json"))
    c2 = normalize_contract(json.loads(blob))
    assert c2.mode == "silent"
    assert c2.contract_version == CONTRACT_VERSION
    metrics = c.output_size_metrics()
    assert metrics["bytes"] > 0
    assert metrics["beats"] == 0
    # unknown version must fail
    with pytest.raises(ContractValidationError) as ei:
        normalize_contract({"contract_version": "dm_turn_contract_v2", "mode": "silent", "reason": "x", "beats": []})
    assert ei.value.code == "invalid_contract_version"


def test_unsupported_player_claim_typed():
    # Player claim that is not corroborated stays as player_declaration, not world_fact
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "unsupported claim",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [
                {"text": "Thorin declares the Seal was cracked before presentation.", "claim_kind": "player_declaration", "actor_ref": {"type": "character", "id": "char:thorin"}, "origin": "player_transcript", "evidence_refs": ["session_message:77"]},
                {"text": "The Seal's prior state is not established.", "claim_kind": "observation", "origin": "dm_adjudication"},
            ],
        }],
        "staged_effects": [{"id": "effect_1", "effect_type": "record_world_event", "arguments": {"event_type": "player_claim", "summary": "Unverified player claim: Seal was cracked before presentation", "visibility": "dm_private", "payload": {"epistemic_status": "player_claim"}}}],
    })
    assert c.beats[0].claims[0].claim_kind == "player_declaration"
    assert c.beats[0].claims[1].claim_kind == "observation"


def test_malformed_output_rejected_before_mutation():
    # Example malformed: beats required for respond but empty
    with pytest.raises(ContractValidationError):
        normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x", "beats": []})
    # extra field
    with pytest.raises(ContractValidationError):
        normalize_contract({"contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "x", "beats": [], "extra": {}})
    # new_entities in wrong mode
    with pytest.raises(ContractValidationError):
        normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "table_chat", "reason": "x", "beats": [], "table_chat_intent": "hi",
            "new_entities": [{"temp_id": "tmp_npc_1", "kind": "npc", "public_name": "Foo"}],
        })


def test_visibility_projection_deterministic():
    c = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": [
            {"id": "beat_1", "type": "narration", "claims": [{"text": "Public fact.", "claim_kind": "observation", "origin": "established_state", "visibility": "public"}]},
            {"id": "beat_2", "type": "npc_dialogue", "speaker_ref": {"type": "npc", "id": "npc:keeper"}, "speaker_public_name": "Keeper",
             "claims": [{"text": "I saw nothing.", "claim_kind": "npc_utterance", "actor_ref": {"type": "npc", "id": "npc:keeper"}, "origin": "dm_adjudication", "visibility": "public"}],
             "truth_status": "mistaken", "dm_private_context": "He actually saw the theft."},
        ],
    })
    p1 = public_projection(c)
    p2 = public_projection(c)
    assert p1 == p2
    assert all("dm_private_context" not in b for b in p1["beats"])

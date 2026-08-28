import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.data_first_turn import (  # noqa: E402
    DataFirstTurnError,
    SCHEMA_VERSION,
    TURN_ATTEMPT_JSON_SCHEMA,
    authorized_player_fact_texts,
    canonicalize_turn_character_refs,
    compact_planner_context,
    data_first_enabled,
    entity_id_catalog,
    gather_turn_evidence,
    generate_turn_attempt,
    guard_turn_actions,
    memory_private_context,
    normalize_turn_attempt,
    public_expansion_packet,
    resolve_and_retry_turn_attempt,
    stage_turn_actions,
    stream_turn_expansion,
    validate_expansion_text,
    validate_resolved_attempt_sources,
    validate_turn_claim_provenance,
    validate_turn_entity_refs,
)
from openrouter import _pc_control_violation, normalize_session_dm_turn_decision  # noqa: E402
from services.stream_manager import SessionGeneratorWorker  # noqa: E402
from services.dm_tools import _effective_world_event_visibility  # noqa: E402


def speaking_attempt(**overrides):
    value = {
        "schema_version": SCHEMA_VERSION,
        "mode": "speak",
        "reason": "The NPC answers the question.",
        "beats": [{
            "id": "beat_1",
            "type": "npc_dialogue",
            "speaker_public_name": "the hooded courier",
            "visible_facts": ["The courier says the caravan left yesterday."],
            "source_refs": ["session_message:12"],
            "truth_status": "deceptive",
            "dm_private_context": "The caravan actually left this morning.",
        }],
        "open_player_choice": "How do the players respond?",
        "max_words": 120,
    }
    value.update(overrides)
    return value


def roll_attempt(**overrides):
    value = speaking_attempt(
        mode="await_player_roll",
        reason="The search has an uncertain outcome.",
        beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": ["Make an Investigation check to examine the damaged seal."],
            "source_refs": ["session_message:12"],
        }],
        open_player_choice=None,
        roll_request={
            "request_id": "roll_1",
            "requested_user_id": 7,
            "character_id": 11,
            "roll_kind": "check",
            "ability_or_skill": "Investigation",
            "label": "Investigation check",
            "advantage_state": "normal",
            "reason_public": "Determine what the damaged seal reveals.",
            "dc_private": 14,
        },
    )
    value.update(overrides)
    return value


def resolving_attempt(**overrides):
    value = {
        "schema_version": SCHEMA_VERSION,
        "mode": "resolve",
        "reason": "The character sheet is required.",
        "beats": [],
        "open_player_choice": None,
        "max_words": 120,
        "safe_prelude": "Checking Mara's character sheet...",
        "evidence_requests": [{
            "id": "evidence_1",
            "tool": "ask_character_sheet",
            "question": "What is Mara's passive Perception?",
            "scope": "current_player",
            "character_id": None,
            "query": None,
            "limit": None,
            "include_private": None,
        }],
    }
    value.update(overrides)
    return value


class DataFirstTurnContractTests(unittest.TestCase):
    def test_feature_flag_is_opt_in(self):
        with patch.dict(os.environ, {"DND_DATA_FIRST_DM_ENABLED": "false"}, clear=False):
            self.assertFalse(data_first_enabled())
        with patch.dict(os.environ, {"DND_DATA_FIRST_DM_ENABLED": "true"}, clear=False):
            self.assertTrue(data_first_enabled())

    def test_public_packet_drops_private_interpretation_and_sources(self):
        attempt = normalize_turn_attempt(speaking_attempt())

        packet = public_expansion_packet(attempt)
        serialized = repr(packet)

        self.assertNotIn("actually left", serialized)
        self.assertNotIn("session_message:12", serialized)
        self.assertNotIn("truth_status", serialized)
        self.assertEqual(packet["beats"][0]["speaker_public_name"], "the hooded courier")
        self.assertEqual(
            packet["beats"][0]["visible_claims"][0]["text"],
            "The courier says the caravan left yesterday.",
        )

    def test_public_packet_marks_player_facts_as_attribution_only(self):
        fact = "Grashnak states that the Seal was damaged before presentation."

        packet = public_expansion_packet(
            speaking_attempt(beats=[{
                "id": "beat_1",
                "type": "narration",
                "visible_facts": [fact],
                "source_refs": ["session_message:105"],
            }]),
            authorized_player_facts=[fact],
        )

        self.assertEqual(
            packet["rendering_constraints"]["player_authored_facts"],
            [fact],
        )
        self.assertEqual(
            packet["rendering_constraints"]["player_character_dialogue"],
            "third_person_attribution_only",
        )

    def test_v2_public_packet_preserves_literal_entity_ids(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "npc_dialogue",
            "speaker_entity_id": "npc:reeve_marlen",
            "speaker_public_name": "Reeve Marlen Hargrove",
            "visible_claims": [{
                "text": "The writ costs one gold piece.",
                "claim_kind": "npc_utterance",
                "actor_ref": {"type": "npc", "id": "npc:reeve_marlen"},
                "target_refs": [],
                "topic_refs": [{"type": "object", "id": "entity:writ"}],
                "location_ref": None,
                "evidence_refs": ["evidence:evidence_1"],
                "trigger_refs": [],
                "origin": "resolver_evidence",
                "roll_request_id": None,
            }],
            "truth_status": "truthful",
        }])

        packet = public_expansion_packet(attempt)

        self.assertEqual(packet["beats"][0]["speaker_entity_id"], "npc:reeve_marlen")
        self.assertEqual(
            packet["beats"][0]["visible_claims"][0]["topic_refs"],
            [{"type": "object", "id": "entity:writ"}],
        )
        self.assertNotIn("evidence_refs", packet["beats"][0]["visible_claims"][0])

    def test_v2_entity_ids_must_exist_in_supplied_context(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_claims": [{
                "text": "The known witness waits by the writ desk.",
                "claim_kind": "observation",
                "actor_ref": {"type": "npc", "id": "npc:witness_1"},
                "target_refs": [],
                "topic_refs": [],
                "location_ref": None,
                "evidence_refs": [],
                "trigger_refs": [],
                "origin": "established_state",
                "roll_request_id": None,
            }],
        }])
        hot_context = {"current_scene": {"active_npc_ids": ["npc:witness_1"]}}

        validated = validate_turn_entity_refs(attempt, hot_context)
        self.assertEqual(validated["beats"][0]["visible_claims"][0]["actor_ref"]["id"], "npc:witness_1")
        self.assertEqual(entity_id_catalog(hot_context), ["npc:witness_1"])

        attempt["beats"][0]["visible_claims"][0]["actor_ref"]["id"] = "npc:invented"
        with self.assertRaises(DataFirstTurnError) as raised:
            validate_turn_entity_refs(attempt, hot_context)
        self.assertEqual(raised.exception.code, "unknown_entity_reference")

    def test_new_npc_local_id_is_valid_only_for_its_accepted_turn(self):
        attempt = speaking_attempt(
            new_actors=[{
                "local_id": "new_npc_1",
                "kind": "npc",
                "public_name": "the lantern keeper",
                "role": "keeper",
                "public_summary": "A lantern keeper steps out of the fog.",
                "location_ref": None,
            }],
            beats=[{
                "id": "beat_1",
                "type": "npc_dialogue",
                "speaker_entity_id": "new_npc_1",
                "speaker_public_name": "the lantern keeper",
                "visible_claims": [{
                    "text": "The keeper asks who is hailing the quay.",
                    "claim_kind": "npc_utterance",
                    "actor_ref": {"type": "npc", "id": "new_npc_1"},
                    "target_refs": [], "topic_refs": [], "location_ref": None,
                    "evidence_refs": [], "trigger_refs": ["session_message:12"],
                    "origin": "dm_adjudication", "roll_request_id": None,
                }],
                "truth_status": "truthful",
            }],
        )
        validated = validate_turn_entity_refs(attempt, {"recent_messages": [{"id": 12}]})
        self.assertEqual(validated["new_actors"][0]["local_id"], "new_npc_1")
        self.assertEqual(validated["actions"][0]["tool"], "register_npc_actor")

    def test_new_npc_cannot_duplicate_a_known_actor_name(self):
        attempt = speaking_attempt(new_actors=[{
            "local_id": "new_npc_1", "kind": "npc", "public_name": "Mara Venn",
            "role": None, "public_summary": None, "location_ref": None,
        }])
        with self.assertRaises(DataFirstTurnError) as raised:
            validate_turn_entity_refs(attempt, {"known_npc_actors": [{"id": "npc_mara", "name": "Mara Venn"}]})
        self.assertEqual(raised.exception.code, "duplicate_new_actor")

    def test_new_npc_normalization_is_idempotent(self):
        initial = normalize_turn_attempt(speaking_attempt(new_actors=[{
            "local_id": "new_npc_1", "kind": "npc", "public_name": "a quay guard",
            "role": "guard", "public_summary": None, "location_ref": None,
        }]))
        repeated = normalize_turn_attempt(initial)
        self.assertEqual(repeated["actions"], initial["actions"])

    def test_new_npc_registration_is_staged_with_the_turn(self):
        attempt = normalize_turn_attempt(speaking_attempt(new_actors=[{
            "local_id": "new_npc_1", "kind": "npc", "public_name": "a quay guard",
            "role": "guard", "public_summary": "A guard watches the mooring line.", "location_ref": None,
        }]))
        calls = []

        def execute(name, arguments, audit):
            calls.append((name, arguments))
            action = {"id": "pending_action_1", "name": name, "args": arguments}
            audit["pending_action_buffer"]["actions"].append(action)
            return {"pending_action_id": action["id"]}

        _buffer, commit_ids = stage_turn_actions(attempt, execute)
        self.assertEqual(calls[0][0], "register_npc_actor")
        self.assertEqual(calls[0][1]["local_id"], "new_npc_1")
        self.assertEqual(commit_ids, ["pending_action_1"])

    def test_graph_pc_alias_is_canonicalized_before_ownership_validation(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1", "type": "narration", "visible_claims": [{
                "text": "Elara asks to inspect the ledger.",
                "claim_kind": "player_declaration",
                "actor_ref": {"type": "character", "id": "elara_moonwhisper"},
                "target_refs": [], "topic_refs": [], "location_ref": None,
                "evidence_refs": ["session_message:90"], "trigger_refs": [],
                "origin": "player_transcript", "roll_request_id": None,
            }],
        }])
        context = {
            "character_ref_aliases": {"elara_moonwhisper": 28},
            "protected_player_characters": [{"id": 28, "name": "Elara Moonwhisper", "user_id": 7}],
            "recent_messages": [{"id": 90, "role": "player", "user_id": 7, "content": "I ask to inspect the ledger."}],
        }
        attempt = canonicalize_turn_character_refs(attempt, context)
        self.assertEqual(attempt["beats"][0]["visible_claims"][0]["actor_ref"]["id"], 28)
        facts = authorized_player_fact_texts(attempt, context, [], _pc_control_violation)
        self.assertEqual(facts, ["Elara asks to inspect the ledger."])

    def test_pending_sheet_proposal_replaces_applied_mutation_claim(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": [
                "Grashnak pays 1 gp for a writ.",
                "The payment is recorded as a deduction from the current 8 gp.",
            ],
            "source_refs": ["session_message:111"],
        }])
        action_buffer = {"actions": [{
            "name": "propose_sheet_update",
            "preview": {"proposal": {
                "status": "pending",
                "changes": [{"field": "gp", "label": "GP", "before": 8, "after": 7}],
            }},
        }]}

        packet = public_expansion_packet(attempt, action_buffer=action_buffer)

        self.assertEqual(
            [claim["text"] for claim in packet["beats"][0]["visible_claims"]],
            ["Grashnak pays 1 gp for a writ."],
        )
        self.assertEqual(packet["pending_action_notices"][0]["status"], "pending_player_approval")
        self.assertEqual(packet["pending_action_notices"][0]["changes"][0], {
            "field": "GP", "before": 8, "after": 7,
        })

    def test_private_interpretation_is_preserved_for_memory(self):
        context = memory_private_context(speaking_attempt())
        self.assertEqual(context, "beat_1: The caravan actually left this morning.")

    def test_non_truthful_dialogue_requires_private_interpretation(self):
        attempt = speaking_attempt()
        del attempt["beats"][0]["dm_private_context"]

        with self.assertRaises(DataFirstTurnError) as raised:
            normalize_turn_attempt(attempt)

        self.assertEqual(raised.exception.code, "missing_private_context")

    def test_unknown_dialogue_gets_an_epistemically_safe_private_interpretation(self):
        attempt = speaking_attempt()
        attempt["beats"][0]["truth_status"] = "unknown"
        attempt["beats"][0]["dm_private_context"] = ""

        normalized = normalize_turn_attempt(attempt)

        self.assertIn("not established", normalized["beats"][0]["dm_private_context"])

    def test_typed_roll_request_is_public_without_private_dc(self):
        attempt = normalize_turn_attempt(roll_attempt())
        packet = public_expansion_packet(attempt)

        self.assertEqual(attempt["mode"], "await_player_roll")
        self.assertEqual(attempt["roll_request"]["dc_private"], 14)
        self.assertEqual(packet["roll_request"]["label"], "Investigation check")
        self.assertNotIn("dc_private", packet["roll_request"])
        self.assertNotIn("requested_user_id", packet["roll_request"])
        self.assertNotIn("character_id", packet["roll_request"])
        self.assertEqual(packet["roll_request"]["instruction"], "Make an Investigation check.")
        self.assertNotIn("advantage_state", packet["roll_request"])

    def test_v2_1_rejects_claim_kind_and_speaker_actor_mismatches(self):
        base_claim = {
            "text": "The warden watches the water.",
            "claim_kind": "observation",
            "actor_ref": {"type": "npc", "id": "warden"},
            "target_refs": [],
            "topic_refs": [],
            "location_ref": None,
            "evidence_refs": ["session_message:10"],
            "trigger_refs": [],
            "origin": "established_state",
            "roll_request_id": None,
        }
        with self.assertRaises(DataFirstTurnError) as wrong_kind:
            normalize_turn_attempt(speaking_attempt(beats=[{
                "id": "beat_1", "type": "npc_dialogue",
                "speaker_entity_id": "warden", "speaker_public_name": "Warden",
                "visible_claims": [base_claim], "truth_status": "truthful",
            }]))
        self.assertEqual(wrong_kind.exception.code, "invalid_dialogue_claim_kind")

        utterance = {**base_claim, "claim_kind": "npc_utterance"}
        with self.assertRaises(DataFirstTurnError) as mismatch:
            normalize_turn_attempt(speaking_attempt(beats=[{
                "id": "beat_1", "type": "npc_dialogue",
                "speaker_entity_id": "harbormaster", "speaker_public_name": "Harbormaster",
                "visible_claims": [utterance], "truth_status": "truthful",
            }]))
        self.assertEqual(mismatch.exception.code, "speaker_actor_mismatch")

    def test_v2_1_new_npc_reaction_uses_trigger_not_player_evidence(self):
        claim = {
            "text": "The warden agrees to watch the sluice.",
            "claim_kind": "npc_utterance",
            "actor_ref": {"type": "npc", "id": "warden"},
            "target_refs": [{"type": "character", "id": 13}],
            "topic_refs": [{"type": "object", "id": "sluice"}],
            "location_ref": None,
            "evidence_refs": [],
            "trigger_refs": ["session_message:57"],
            "origin": "dm_adjudication",
            "roll_request_id": None,
        }
        attempt = speaking_attempt(beats=[{
            "id": "beat_1", "type": "npc_dialogue",
            "speaker_entity_id": "warden", "speaker_public_name": "Warden",
            "visible_claims": [claim], "truth_status": "truthful",
        }])
        context = {"recent_messages": [{"id": 57, "role": "player", "user_id": 7, "content": "Watch the sluice."}]}

        self.assertEqual(
            validate_turn_claim_provenance(attempt, context)["beats"][0]["visible_claims"][0]["trigger_refs"],
            ["session_message:57"],
        )

        claim["evidence_refs"] = claim.pop("trigger_refs")
        claim["trigger_refs"] = []
        claim["origin"] = "established_state"
        with self.assertRaises(DataFirstTurnError) as raised:
            validate_turn_claim_provenance(attempt, context)
        self.assertEqual(
            raised.exception.details["violations"][0]["reason"],
            "player_message_cannot_evidence_npc_utterance",
        )

    def test_v2_1_canonicalizes_explicit_roll_link_into_roll_outcome(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_claims": [{
                "text": "No sheared pin can be distinguished on this pass.",
                "claim_kind": "observation",
                "actor_ref": {"type": "object", "id": "tuning_core"},
                "target_refs": [],
                "topic_refs": [],
                "location_ref": None,
                "evidence_refs": ["session_message:146"],
                "trigger_refs": [],
                "origin": "dm_adjudication",
                "roll_request_id": "roll_2_144",
            }],
        }])

        normalized = normalize_turn_attempt(attempt)
        claim = normalized["beats"][0]["visible_claims"][0]

        self.assertEqual(claim["claim_kind"], "roll_outcome")
        self.assertEqual(claim["origin"], "roll_adjudication")
        self.assertIsNone(claim["actor_ref"])
        self.assertEqual(claim["topic_refs"], [{"type": "object", "id": "tuning_core"}])
        context = {
            "recent_messages": [{"id": 146, "role": "player", "content": "[Roll] total: 6"}],
            "recent_roll_requests": [{"request_id": "roll_2_144", "status": "fulfilled"}],
        }
        self.assertEqual(
            validate_turn_claim_provenance(normalized, context)["mode"],
            "speak",
        )

    def test_v2_1_canonicalizes_fulfilled_roll_message_trigger_into_outcome(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_claims": [{
                "text": "The pin remains indistinct on this pass.",
                "claim_kind": "observation",
                "actor_ref": None,
                "target_refs": [],
                "topic_refs": [{"type": "object", "id": "tuning_core"}],
                "location_ref": None,
                "evidence_refs": [],
                "trigger_refs": ["session_message:146"],
                "origin": "dm_adjudication",
                "roll_request_id": None,
            }],
        }])
        context = {
            "recent_messages": [{"id": 146, "role": "player", "content": "[Roll] total: 6"}],
            "recent_roll_requests": [{
                "request_id": "roll_2_144", "status": "fulfilled", "result_message_id": 146,
            }],
        }

        result = validate_turn_claim_provenance(attempt, context)
        claim = result["beats"][0]["visible_claims"][0]

        self.assertEqual(claim["claim_kind"], "roll_outcome")
        self.assertEqual(claim["origin"], "roll_adjudication")
        self.assertEqual(claim["roll_request_id"], "roll_2_144")

    def test_roll_request_is_required_only_for_await_mode(self):
        with self.assertRaises(DataFirstTurnError) as missing:
            normalize_turn_attempt(roll_attempt(roll_request=None))
        self.assertEqual(missing.exception.code, "missing_roll_request")

        with self.assertRaises(DataFirstTurnError) as unexpected:
            normalize_turn_attempt(speaking_attempt(roll_request=roll_attempt()["roll_request"]))
        self.assertEqual(unexpected.exception.code, "unexpected_roll_request")

    def test_structured_actions_use_existing_deferred_staging_contract(self):
        attempt = speaking_attempt(actions=[{
            "id": "action_1",
            "tool": "record_world_event",
            "arguments_json": '{"event_type":"clue_found","summary":"The seal was examined."}',
        }])

        def execute(tool, arguments, audit):
            self.assertEqual(tool, "record_world_event")
            self.assertEqual(arguments["event_type"], "clue_found")
            self.assertEqual(audit["operation"], "data_first_action_staging")
            action = {"id": "pending_action_1", "name": tool, "args": arguments}
            audit["pending_action_buffer"]["actions"].append(action)
            return {"pending_action_id": action["id"], "pending": True}

        action_buffer, commit_ids = stage_turn_actions(attempt, execute)

        self.assertEqual(commit_ids, ["pending_action_1"])
        self.assertEqual(action_buffer["actions"][0]["name"], "record_world_event")

    def test_world_event_preview_uses_the_same_visibility_policy_as_commit(self):
        self.assertEqual(
            _effective_world_event_visibility({}, "party_known"),
            "dm_private",
        )
        self.assertEqual(
            _effective_world_event_visibility(
                {"source_facet_ids": ["fact:clue_1:text"]}, "party_known",
            ),
            "party_known",
        )

    def test_action_guard_skips_redundant_reveal_of_visible_world_event(self):
        attempt = speaking_attempt(actions=[{
            "id": "action_1",
            "tool": "reveal_fact",
            "arguments_json": '{"item_type":"fact","item_id":"fact_discovery_damaged_seal","visibility":"party_known","reason":"Reveal it"}',
        }])
        context = {"recent_public_world_events": [{
            "id": 224,
            "event_type": "discovery_damaged_seal",
            "summary": "The party already heard the claim.",
            "visibility": "party_known",
        }]}

        guarded, notes = guard_turn_actions(attempt, context)

        self.assertEqual(guarded["actions"], [])
        self.assertEqual(notes[0]["result"], "already_visible_reveal_skipped")

    def test_action_guard_downgrades_unsupported_public_player_theory(self):
        attempt = speaking_attempt(actions=[{
            "id": "action_1",
            "tool": "record_world_event",
            "arguments_json": '{"event_type":"discovery","summary":"Grashnak proves the Seal was cracked before presentation","payload":{"source_facet_ids":["fact_ledger_stolen"]},"visibility":"party_known"}',
        }])
        context = {"established_public_facts": [{
            "id": "fact_ledger_stolen",
            "text": "The Ledger and intact ashglass Seal are missing from the pedestal.",
            "visibility": "party_known",
        }]}

        guarded, notes = guard_turn_actions(attempt, context)
        action = guarded["actions"][0]

        self.assertEqual(action["arguments"]["visibility"], "dm_private")
        self.assertTrue(action["arguments"]["summary"].startswith("Unverified player claim:"))
        self.assertEqual(action["arguments"]["payload"]["epistemic_status"], "player_claim")
        self.assertEqual(notes[0]["result"], "unsupported_public_claim_downgraded")

    def test_action_normalization_is_idempotent(self):
        initial = normalize_turn_attempt(speaking_attempt(actions=[{
            "id": "action_1",
            "tool": "record_world_event",
            "arguments_json": '{"event_type":"clue_found","summary":"The seal was examined."}',
        }]))

        repeated = normalize_turn_attempt(initial)

        self.assertEqual(repeated["actions"], initial["actions"])

    def test_fallback_must_not_contain_visible_beats(self):
        with self.assertRaises(DataFirstTurnError) as raised:
            normalize_turn_attempt(speaking_attempt(mode="fallback"))
        self.assertEqual(raised.exception.code, "unexpected_beats")

    def test_resolve_attempt_normalizes_only_read_only_requests(self):
        attempt = normalize_turn_attempt(resolving_attempt())

        self.assertEqual(attempt["mode"], "resolve")
        self.assertEqual(attempt["evidence_requests"][0], {
            "id": "evidence_1",
            "tool": "ask_character_sheet",
            "question": "What is Mara's passive Perception?",
            "scope": "current_player",
        })

        unsafe = resolving_attempt()
        unsafe["evidence_requests"][0]["tool"] = "roll_dice"
        with self.assertRaises(DataFirstTurnError) as raised:
            normalize_turn_attempt(unsafe)
        self.assertEqual(raised.exception.code, "unsafe_evidence_tool")

    def test_evidence_gathering_executes_bounded_read_only_request(self):
        calls = []

        def execute(tool, arguments, audit_context):
            calls.append((tool, arguments, audit_context))
            return {"answer": "Mara's passive Perception is 14.", "extra": "x" * 2000}

        bundle = gather_turn_evidence(resolving_attempt(), execute)

        self.assertEqual(calls[0][0], "ask_character_sheet")
        self.assertEqual(calls[0][1]["scope"], "current_player")
        self.assertEqual(calls[0][2]["actor"], "session_dm_evidence_resolver")
        self.assertEqual(bundle["resolution_pass"], 1)
        self.assertEqual(bundle["evidence"][0]["result"]["answer"], "Mara's passive Perception is 14.")
        self.assertEqual(len(bundle["evidence"][0]["result"]["extra"]), 1200)

    def test_resolver_retries_planner_exactly_once_with_evidence(self):
        final = speaking_attempt()
        final["beats"][0]["visible_facts"] = ["Mara's passive Perception is 14."]
        final["beats"][0]["source_refs"] = ["evidence:evidence_1"]
        final_attempt = normalize_turn_attempt(final)
        with patch("services.data_first_turn.generate_turn_attempt", return_value=final_attempt) as generate:
            result, bundle = resolve_and_retry_turn_attempt(
                {},
                [],
                resolving_attempt(),
                lambda _tool, _arguments, _audit: {"answer": "Mara's passive Perception is 14."},
            )

        self.assertEqual(result["mode"], "speak")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.kwargs["evidence_bundle"], bundle)

    def test_resolver_rejects_visible_output_when_requested_evidence_failed(self):
        final = speaking_attempt()
        final["beats"][0]["source_refs"] = ["evidence:evidence_1"]
        with patch("services.data_first_turn.generate_turn_attempt", return_value=normalize_turn_attempt(final)):
            with self.assertRaises(DataFirstTurnError) as raised:
                resolve_and_retry_turn_attempt(
                    {},
                    [],
                    resolving_attempt(),
                    lambda _tool, _arguments, _audit: {"error": "lookup unavailable"},
                )
        self.assertEqual(raised.exception.code, "incomplete_evidence_used")

    def test_resolver_rejects_claim_unrelated_to_cited_evidence(self):
        final = speaking_attempt()
        final["beats"][0]["visible_facts"] = ["The depot log remains locked in the sheriff's office."]
        final["beats"][0]["source_refs"] = ["evidence:evidence_1"]
        with patch("services.data_first_turn.generate_turn_attempt", return_value=normalize_turn_attempt(final)):
            with self.assertRaises(DataFirstTurnError) as raised:
                resolve_and_retry_turn_attempt(
                    {},
                    [],
                    resolving_attempt(),
                    lambda _tool, _arguments, _audit: {
                        "answer": "Mara's passive Perception is 14.",
                        "missing": False,
                    },
                )
        self.assertEqual(raised.exception.code, "unsupported_evidence_claim")

    def test_v2_resolver_uses_exact_per_claim_source_ids_without_text_regex(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_claims": [{
                "text": "Mara's passive Perception is fourteen.",
                "claim_kind": "world_fact",
                "actor_ref": None,
                "target_refs": [],
                "topic_refs": [{"type": "character", "id": 11}],
                "location_ref": None,
                "evidence_refs": ["evidence:evidence_1"],
                "trigger_refs": [],
                "origin": "resolver_evidence",
                "roll_request_id": None,
            }],
        }])
        bundle = {
            "complete": True,
            "evidence": [{"request_id": "evidence_1", "result": {"answer": "Passive Perception: 14"}}],
        }

        result = validate_resolved_attempt_sources(attempt, bundle)

        self.assertEqual(result["beats"][0]["visible_claims"][0]["evidence_refs"], ["evidence:evidence_1"])

        attempt["beats"][0]["visible_claims"][0]["evidence_refs"] = ["evidence:invented"]
        with self.assertRaises(DataFirstTurnError) as raised:
            validate_resolved_attempt_sources(attempt, bundle)
        self.assertEqual(raised.exception.code, "unknown_evidence_citation")

    def test_resolver_grounds_mixed_player_and_sheet_facts_in_separate_lanes(self):
        final = speaking_attempt(
            beats=[{
                "id": "beat_1",
                "type": "narration",
                "visible_facts": [
                    "Grashnak Bonecrusher currently has 8 gp before payment",
                    "Player requested proposal to deduct 1 gp for writ payment",
                    "Proposal to deduct 1 gp is pending approval and would leave 7 gp",
                ],
                "source_refs": ["session_message:125", "evidence:evidence_1"],
            }],
            actions=[{
                "id": "action_1",
                "tool": "propose_sheet_update",
                "arguments_json": '{"character_id":21,"reason":"Writ payment","changes":[{"field":"gp","operation":"subtract","value":1}]}',
            }],
        )
        recent = [{
            "id": 125,
            "role": "player",
            "user_id": 15,
            "content": "Grashnak pays 1 gp for the writ; could you propose deducting 1 gp?",
        }]
        with patch("services.data_first_turn.generate_turn_attempt", return_value=normalize_turn_attempt(final)):
            result, _bundle = resolve_and_retry_turn_attempt(
                {},
                recent,
                resolving_attempt(),
                lambda _tool, _arguments, _audit: {
                    "answer": "Grashnak Bonecrusher has 8 gp.",
                    "missing": False,
                },
            )

        self.assertEqual(result["beats"][0]["visible_facts"], [
            "Grashnak Bonecrusher currently has 8 gp before payment",
            "Player requested proposal to deduct 1 gp for writ payment",
        ])

    def test_resolver_drops_unsupported_npc_dialogue_when_grounded_facts_remain(self):
        final = speaking_attempt(
            beats=[
                {
                    "id": "beat_1",
                    "type": "npc_dialogue",
                    "speaker_public_name": "Reeve Marlen Hargrove",
                    "visible_facts": ["Reeve Marlen Hargrove says the writ costs 1 gp"],
                    "source_refs": ["session_message:128", "evidence:evidence_1"],
                    "truth_status": "truthful",
                    "dm_private_context": None,
                },
                {
                    "id": "beat_2",
                    "type": "narration",
                    "visible_facts": ["Grashnak Bonecrusher currently has 8 gp"],
                    "source_refs": ["evidence:evidence_1"],
                },
            ],
        )
        recent = [{
            "id": 128,
            "role": "player",
            "user_id": 15,
            "content": "Reeve, I'll take that writ. One gold, and it's done.",
        }]
        with patch("services.data_first_turn.generate_turn_attempt", return_value=normalize_turn_attempt(final)):
            result, _bundle = resolve_and_retry_turn_attempt(
                {},
                recent,
                resolving_attempt(),
                lambda _tool, _arguments, _audit: {
                    "answer": "Grashnak Bonecrusher has 8 gp.",
                    "missing": False,
                },
            )

        self.assertEqual([beat["id"] for beat in result["beats"]], ["beat_2"])

    def test_sheet_proposal_normalizes_common_gold_field_alias(self):
        attempt = normalize_turn_attempt(speaking_attempt(actions=[{
            "id": "action_1",
            "tool": "propose_sheet_update",
            "arguments_json": '{"character_id":21,"reason":"Writ payment","changes":[{"field":"gold_pieces","operation":"subtract","value":1}]}',
        }]))

        self.assertEqual(attempt["actions"][0]["arguments"]["changes"][0]["field"], "gp")

    def test_resolver_rejects_a_second_resolution_pass(self):
        second_resolve = normalize_turn_attempt(resolving_attempt())
        with patch("services.data_first_turn.generate_turn_attempt", return_value=second_resolve):
            with self.assertRaises(DataFirstTurnError) as raised:
                resolve_and_retry_turn_attempt(
                    {},
                    [],
                    resolving_attempt(),
                    lambda _tool, _arguments, _audit: {"answer": "missing"},
                )
        self.assertEqual(raised.exception.code, "resolution_loop")

    def test_expansion_is_bounded_and_rejects_model_authored_tags(self):
        packet = public_expansion_packet(speaking_attempt())
        self.assertEqual(validate_expansion_text("**Courier:** The caravan left yesterday.", packet), "**Courier:** The caravan left yesterday.")
        with self.assertRaises(DataFirstTurnError) as raised:
            validate_expansion_text('<npc target="Courier">No.</npc>', packet)
        self.assertEqual(raised.exception.code, "expansion_markup")

    def test_streaming_expander_uses_uncapped_non_reasoning_request(self):
        packet = public_expansion_packet(speaking_attempt())
        with patch("openrouter._post_chat_stream", return_value="Expanded text") as stream:
            result = stream_turn_expansion(packet, audit_context={"campaign_id": 3})

        self.assertEqual(result, "Expanded text")
        kwargs = stream.call_args.kwargs
        self.assertFalse(kwargs["allow_thinking"])
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertIsNone(kwargs["max_tokens"])
        self.assertEqual(kwargs["provider"], "opencode_go")
        self.assertEqual(kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(kwargs["reasoning_effort"], "none")
        serialized_messages = repr(stream.call_args.args[0])
        self.assertNotIn("actually left this morning", serialized_messages)

    def test_ordinary_turns_default_to_an_eighty_word_budget(self):
        attempt = normalize_turn_attempt({
            "schema_version": SCHEMA_VERSION,
            "mode": "silent",
            "reason": "No DM response is needed.",
            "beats": [],
            "open_player_choice": None,
            "safe_prelude": None,
            "evidence_requests": [],
            "actions": [],
            "new_actors": [],
            "roll_request": None,
        })
        self.assertEqual(attempt["max_words"], 80)

    def test_table_chat_is_public_but_has_no_campaign_contract(self):
        attempt = normalize_turn_attempt({
            "schema_version": SCHEMA_VERSION,
            "mode": "table_chat",
            "reason": "The player is making casual table conversation.",
            "beats": [],
            "open_player_choice": None,
            "max_words": 60,
            "safe_prelude": None,
            "table_chat_intent": "Briefly answer warmly without advancing the story.",
            "evidence_requests": [],
            "actions": [],
            "new_actors": [],
            "roll_request": None,
        })
        packet = public_expansion_packet(attempt)
        self.assertEqual(packet, {
            "schema_version": SCHEMA_VERSION,
            "mode": "table_chat",
            "table_chat_intent": "Briefly answer warmly without advancing the story.",
            "max_words": 60,
        })

    def test_table_chat_rejects_story_beats_or_missing_intent(self):
        with self.assertRaises(DataFirstTurnError) as raised:
            normalize_turn_attempt({
                "schema_version": SCHEMA_VERSION, "mode": "table_chat", "reason": "chat",
                "beats": [], "open_player_choice": None, "max_words": 80,
                "safe_prelude": None, "table_chat_intent": None, "evidence_requests": [],
                "actions": [], "new_actors": [], "roll_request": None,
            })
        self.assertEqual(raised.exception.code, "missing_table_chat_intent")

    def test_decision_normalizer_preserves_table_chat_mode(self):
        decision = normalize_session_dm_turn_decision({
            "mode": "table_chat",
            "content": "Hey! Good to see you.",
        })
        self.assertEqual(decision["mode"], "table_chat")
        self.assertEqual(decision["content"], "Hey! Good to see you.")

    def test_planner_uses_strict_schema_and_larger_output_budget(self):
        with patch("openrouter._post_chat", return_value=(
            '{"schema_version":"' + SCHEMA_VERSION + '","mode":"fallback",'
            '"reason":"needs a roll","beats":[],"open_player_choice":null,"max_words":220}'
        )) as post:
            result = generate_turn_attempt({}, [])

        self.assertEqual(result["mode"], "fallback")
        kwargs = post.call_args.kwargs
        self.assertTrue(kwargs["json_mode"])
        self.assertIs(kwargs["json_schema"], TURN_ATTEMPT_JSON_SCHEMA)
        self.assertEqual(kwargs["json_schema_name"], "data_first_turn_attempt")
        self.assertEqual(kwargs["reasoning_effort"], "minimal")
        self.assertEqual(kwargs["max_tokens"], 4000)

    def test_compact_context_keeps_private_canon_out_of_transcript_duplication(self):
        hot_context = {
            "campaign": {"id": 3},
            "canonical_private_facts": [{"id": "secret_1", "text": "Hidden truth"}],
            "recent_messages": [{"id": 1, "role": "player", "content": "duplicate"}],
        }
        recent = [{"id": 2, "role": "player", "content": "What do I see?"}]

        compact = compact_planner_context(hot_context, recent)

        self.assertEqual(compact["canonical_private_facts"][0]["id"], "secret_1")
        self.assertEqual([message["source_id"] for message in compact["recent_messages"]], ["session_message:2"])

    def test_player_behavior_is_allowed_when_grounded_in_owners_cited_message(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": [
                "Thorin calls for no bell to be struck until the reef is sounded true.",
            ],
            "source_refs": ["session_message:57"],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 13, "name": "Thorin Ironbeard", "user_id": 7}],
        }
        messages = [{
            "id": 57,
            "role": "player",
            "user_id": 7,
            "content": "Thorin calls out: strike no bell until the reef is sounded true!",
        }]

        allowed = authorized_player_fact_texts(
            attempt, hot_context, messages, _pc_control_violation,
        )

        self.assertEqual(allowed, attempt["beats"][0]["visible_facts"])
        self.assertIsNone(_pc_control_violation(
            "Thorin calls through the rain for no bell to be struck until the reef is sounded true.",
            hot_context,
            allowed_player_facts=allowed,
        ))

    def test_v2_player_declaration_uses_actor_and_source_ids_without_regex(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "speaker_entity_id": None,
            "visible_claims": [{
                "text": "Thorin calls for no bell to be struck.",
                "claim_kind": "player_declaration",
                "actor_ref": {"type": "character", "id": 13},
                "target_refs": [],
                "topic_refs": [],
                "location_ref": None,
                "evidence_refs": ["session_message:57"],
                "trigger_refs": [],
                "origin": "player_transcript",
                "roll_request_id": None,
            }],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 13, "name": "Thorin Ironbeard", "user_id": 7}],
        }
        messages = [{"id": 57, "role": "player", "user_id": 7, "content": "Strike the bell."}]

        def detector_must_not_run(*_args, **_kwargs):
            raise AssertionError("v2 actor validation must not parse prose")

        allowed = authorized_player_fact_texts(
            attempt, hot_context, messages, detector_must_not_run,
        )

        self.assertEqual(allowed, ["Thorin calls for no bell to be struck."])

    def test_v2_player_declaration_rejects_wrong_owner_by_id(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_claims": [{
                "text": "Thorin attacks.",
                "claim_kind": "player_declaration",
                "actor_ref": {"type": "character", "id": 13},
                "target_refs": [],
                "topic_refs": [],
                "location_ref": None,
                "evidence_refs": ["session_message:57"],
                "trigger_refs": [],
                "origin": "player_transcript",
                "roll_request_id": None,
            }],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 13, "name": "Thorin Ironbeard", "user_id": 7}],
        }
        messages = [{"id": 57, "role": "player", "user_id": 8, "content": "Thorin attacks."}]

        with self.assertRaises(DataFirstTurnError) as raised:
            authorized_player_fact_texts(attempt, hot_context, messages, lambda *_args: None)

        self.assertEqual(raised.exception.details["checked_sources"][0]["reason"], "wrong_character_owner")

    def test_player_behavior_requires_a_source_reference(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": ["Thorin calls for the crowd to leave."],
            "source_refs": [],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 13, "name": "Thorin Ironbeard", "user_id": 7}],
        }

        with self.assertRaises(DataFirstTurnError) as raised:
            authorized_player_fact_texts(attempt, hot_context, [], _pc_control_violation)

        self.assertEqual(raised.exception.code, "unproven_player_behavior")

    def test_one_player_cannot_authorize_another_players_character(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": ["Elara steps toward the Warden and calls for a public test."],
            "source_refs": ["session_message:59"],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 12, "name": "Elara Moonwhisper", "user_id": 6}],
        }
        messages = [{
            "id": 59,
            "role": "player",
            "user_id": 8,
            "content": "Elara steps toward the Warden and calls for a public test.",
        }]

        with self.assertRaises(DataFirstTurnError) as raised:
            authorized_player_fact_texts(attempt, hot_context, messages, _pc_control_violation)

        self.assertEqual(raised.exception.code, "unproven_player_behavior")
        self.assertEqual(
            raised.exception.details["checked_sources"][0]["reason"],
            "wrong_character_owner",
        )

    def test_duplicate_character_names_resolve_by_cited_message_owner(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": ["Kaelen Shadowstep moves to the lake and studies the dark ripples."],
            "source_refs": ["session_message:73"],
        }])
        hot_context = {
            "protected_player_characters": [
                {"id": 18, "name": "Kaelen Shadowstep", "user_id": 12},
                {"id": 19, "name": "Kaelen Shadowstep", "user_id": 13},
            ],
        }
        messages = [{
            "id": 73,
            "role": "player",
            "user_id": 13,
            "content": "Kaelen moves to the lake and studies the dark ripples.",
        }]

        allowed = authorized_player_fact_texts(
            attempt, hot_context, messages, _pc_control_violation,
        )

        self.assertEqual(allowed, attempt["beats"][0]["visible_facts"])

    def test_possessive_pc_object_does_not_become_the_speaker(self):
        hot_context = {
            "protected_player_characters": [
                {"id": 17, "name": "Elara Moonwhisper", "user_id": 11},
            ],
        }
        text = (
            "The revered leader watches Elara Moonwhisper's study and says, "
            '"Read it truly, witness."'
        )

        self.assertIsNone(_pc_control_violation(text, hot_context))

    def test_cited_message_must_textually_support_the_player_behavior(self):
        attempt = speaking_attempt(beats=[{
            "id": "beat_1",
            "type": "narration",
            "visible_facts": ["Thorin attacks the Exactor with his warhammer."],
            "source_refs": ["session_message:57"],
        }])
        hot_context = {
            "protected_player_characters": [{"id": 13, "name": "Thorin Ironbeard", "user_id": 7}],
        }
        messages = [{
            "id": 57,
            "role": "player",
            "user_id": 7,
            "content": "Thorin asks the crowd to delay the vote.",
        }]

        with self.assertRaises(DataFirstTurnError) as raised:
            authorized_player_fact_texts(attempt, hot_context, messages, _pc_control_violation)

        self.assertEqual(raised.exception.code, "unproven_player_behavior")

    def test_worker_fails_closed_when_data_first_is_disabled(self):
        worker = SessionGeneratorWorker(3, 4, 5, "hello", player_message_id=12)
        with (
            patch("services.data_first_turn.data_first_enabled", return_value=False),
            patch("services.stream_manager.log_audit_event") as audit,
        ):
            with self.assertRaises(DataFirstTurnError) as raised:
                worker._try_data_first_turn(
                    SimpleNamespace(id=3),
                    SimpleNamespace(id=4),
                    SimpleNamespace(id=5),
                    [],
                    {},
                    "trace_1",
                    "trace 1",
                )

        self.assertEqual(raised.exception.code, "data_first_disabled")
        self.assertEqual(audit.call_args.args[1], "data_first_turn_failed")

    def test_worker_fails_closed_for_unsupported_combat(self):
        worker = SessionGeneratorWorker(3, 4, 5, "attack", player_message_id=12)
        with (
            patch("services.data_first_turn.data_first_enabled", return_value=True),
            patch("services.stream_manager.log_audit_event") as audit,
        ):
            with self.assertRaises(DataFirstTurnError) as raised:
                worker._try_data_first_turn(
                    SimpleNamespace(id=3),
                    SimpleNamespace(id=4),
                    SimpleNamespace(id=5),
                    [],
                    {"combat_coordinates": {"active": True}},
                    "trace_1",
                    "trace 1",
                )

        self.assertEqual(raised.exception.code, "unsupported_combat")
        self.assertEqual(audit.call_args.args[1], "data_first_turn_failed")

    def test_planner_fallback_becomes_a_durable_failure(self):
        worker = SessionGeneratorWorker(3, 4, 5, "unsupported", player_message_id=12)
        fallback_attempt = normalize_turn_attempt({
            "schema_version": SCHEMA_VERSION,
            "mode": "fallback",
            "reason": "Map movement is unsupported.",
            "beats": [],
            "open_player_choice": None,
            "max_words": 120,
            "safe_prelude": None,
            "evidence_requests": [],
            "actions": [],
            "roll_request": None,
        })
        with (
            patch("services.data_first_turn.data_first_enabled", return_value=True),
            patch("services.data_first_turn.generate_turn_attempt", return_value=fallback_attempt),
            patch.object(worker, "update_status"),
            patch("services.stream_manager.log_audit_event") as audit,
        ):
            with self.assertRaises(DataFirstTurnError) as raised:
                worker._try_data_first_turn(
                    SimpleNamespace(id=3),
                    SimpleNamespace(id=4),
                    SimpleNamespace(id=5),
                    [],
                    {},
                    "trace_1",
                    "trace 1",
                )

        self.assertEqual(raised.exception.code, "unsupported_turn")
        self.assertEqual(audit.call_args.args[1], "data_first_turn_failed")


if __name__ == "__main__":
    unittest.main()

"""Atomic persistence for an accepted visible DM turn and its staged narrative actions."""

from models import db, SessionMessage, CampaignDmResponseParts, CampaignResolverPacket
from services.audit_service import log_audit_event
from services.dm_tools import apply_deferred_narrative_action
from services.dm_turns import mark_session_dm_turn_error, mark_session_dm_turn_visible
from services.memory_resolver_schemas import validate_resolver_packet


def _selected_actions(action_buffer, commit_action_ids):
    actions = (action_buffer or {}).get('actions') if isinstance(action_buffer, dict) else []
    actions = actions if isinstance(actions, list) else []
    requested = commit_action_ids if isinstance(commit_action_ids, list) else None
    if requested is None or any(not isinstance(item, str) or not item for item in requested):
        raise ValueError('talk_to_player must provide commit_action_ids as an array of pending action IDs.')
    if len(set(requested)) != len(requested):
        raise ValueError('commit_action_ids may not contain duplicates.')
    by_id = {action.get('id'): action for action in actions if isinstance(action, dict)}
    unknown = [action_id for action_id in requested if action_id not in by_id]
    if unknown:
        raise ValueError(f'Unknown pending action IDs: {unknown}')
    return [by_id[action_id] for action_id in requested]


def commit_accepted_dm_turn(
    campaign,
    session,
    current_user,
    player_message_id,
    trace_id,
    trace_label,
    content,
    commit_action_ids,
    action_buffer,
    response_parts,
    resolver_packet=None,
):
    """Commit selected staged actions and the visible message as one transaction.

    This intentionally raises after recording the turn error if any selected action cannot be
    revalidated or persisted. Callers must not send the visible reply until this returns.
    """
    try:
        selected_actions = _selected_actions(action_buffer, commit_action_ids)
        created_proposals = []
        action_results = []
        for action in selected_actions:
            result, proposal = apply_deferred_narrative_action(campaign, session, current_user, action)
            if isinstance(result, dict) and result.get('error'):
                raise ValueError(result['error'])
            action_results.append({'pending_action_id': action['id'], 'tool_name': action['name'], 'result': result})
            if proposal is not None:
                created_proposals.append(proposal)

        ai_msg = SessionMessage(session_id=session.id, role='dm', content=content)
        db.session.add(ai_msg)
        db.session.flush()
        for proposal in created_proposals:
            proposal.message_id = ai_msg.id

        if not isinstance(response_parts, list) or not response_parts:
            raise ValueError('Accepted DM turn must include structured response parts.')
        db.session.add(
            CampaignDmResponseParts(
                campaign_id=campaign.id,
                session_id=session.id,
                dm_message_id=ai_msg.id,
                turn_id=trace_id,
                parts_json=response_parts,
            )
        )
        if isinstance(resolver_packet, dict):
            ok, err = validate_resolver_packet(resolver_packet)
            if not ok:
                raise ValueError(f"Invalid resolver_packet in accepted DM turn: {err}")
            db.session.add(CampaignResolverPacket(
                campaign_id=campaign.id,
                session_id=session.id,
                dm_message_id=ai_msg.id,
                turn_id=trace_id,
                packet_json=resolver_packet,
                status='committed',
            ))

        for action_result in action_results:
            log_audit_event(
                campaign.id,
                'dm_staged_action_committed',
                f"Committed staged DM action: {action_result['tool_name']}",
                {'session_id': session.id, **action_result},
                source='dm_tools',
                actor='session_dm',
                trace_id=trace_id,
                trace_label=trace_label,
                audit_role='tools',
                commit=False,
            )
        log_audit_event(
            campaign.id,
            'dm_output_stored',
            'Stored visible session DM response.',
            {
                'session_id': session.id,
                'player_message_id': player_message_id,
                'dm_message_id': ai_msg.id,
                'message': {'role': 'dm', 'content': content},
                'committed_action_ids': list(commit_action_ids),
            },
            source='session_messages',
            actor='session_dm',
            trace_id=trace_id,
            trace_label=trace_label,
            commit=False,
        )
        db.session.add(mark_session_dm_turn_visible(
            campaign.id,
            session.id,
            player_message_id,
            trace_id,
            status='speak',
            dm_message_id=ai_msg.id,
        ))
        db.session.commit()
        return ai_msg, created_proposals, action_results
    except Exception as err:
        db.session.rollback()
        db.session.add(mark_session_dm_turn_error(
            campaign.id,
            session.id,
            player_message_id,
            trace_id,
            repr(err),
        ))
        db.session.commit()
        raise

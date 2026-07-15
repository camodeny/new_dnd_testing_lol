import os
from pathlib import Path
from flask import current_app

from models import (
    db, Campaign, CampaignSession, Character, CharacterPlanningMessage,
    CampaignPlanningSummary, PlanningBondProposal, CampaignAuditEvent,
    LLMPlayer, LootBox, SessionDmTurn, CampaignShop, CampaignMemoryRun,
    CampaignMemoryLog, SheetProposal, CharacterClass, CharacterSkill,
    CharacterSavingThrow, CharacterProficiency, CharacterFeature,
    CharacterWeapon, CharacterEquipment, CharacterSpell, CharacterNote,
    CharacterResource, CharacterCompanion, CharacterCondition, CampaignMember,
    EncounterMap, AutomationRun, AutomationScenario, AutomationSnapshot,
    AutomationRunAuditAttempt, AutomationRunAuditorJob, AutomationRunAuditCycle,
    AutomationRunProviderCall, AutomationRunAuditResult, AutomationRunEvent
)


def delete_campaign_graph(campaign_ids, character_policy='delete', ignore_snapshot_ids=None):
    """Deletes campaigns and cleans up all related records in dependency order,
    preventing any referential integrity/foreign key issues.
    """
    if not campaign_ids:
        return

    # Check for any external snapshot references pointing to the sessions or campaigns being deleted
    session_ids = [s.id for s in CampaignSession.query.filter(CampaignSession.campaign_id.in_(campaign_ids)).all()]
    
    snap_filter = (AutomationSnapshot.source_campaign_id.in_(campaign_ids))
    if session_ids:
        snap_filter = snap_filter | (AutomationSnapshot.source_session_id.in_(session_ids))
        
    referencing_snapshots = AutomationSnapshot.query.filter(snap_filter)
    if ignore_snapshot_ids:
        referencing_snapshots = referencing_snapshots.filter(~AutomationSnapshot.id.in_(ignore_snapshot_ids))
        
    ref_snap = referencing_snapshots.first()
    if ref_snap:
        raise ValueError(f"Cannot delete campaign clone because snapshot '{ref_snap.label}' (ID {ref_snap.id}) references it.")

    # Collect Character IDs
    character_ids = [c.id for c in Character.query.filter(Character.campaign_id.in_(campaign_ids)).all()]

    # Collect EncounterMap filenames to cleanup from disk later
    maps_to_delete = EncounterMap.query.filter(EncounterMap.campaign_id.in_(campaign_ids)).all()
    filenames_to_cleanup = []
    for m in maps_to_delete:
        if m.image_filename:
            filenames_to_cleanup.append(m.image_filename)
        if m.labeled_image_filename:
            filenames_to_cleanup.append(m.labeled_image_filename)
    filenames_to_cleanup = list(set(filenames_to_cleanup))

    # Delete SheetProposals first (references session_id, character_id, message_id)
    if session_ids:
        SheetProposal.query.filter(SheetProposal.session_id.in_(session_ids)).delete(synchronize_session=False)
    if character_ids:
        SheetProposal.query.filter(SheetProposal.character_id.in_(character_ids)).delete(synchronize_session=False)

    # Delete DM turns, loot boxes, and memory runs/logs before cascades
    SessionDmTurn.query.filter(SessionDmTurn.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    LootBox.query.filter(LootBox.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    CampaignMemoryRun.query.filter(CampaignMemoryRun.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    CampaignMemoryLog.query.filter(CampaignMemoryLog.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)

    # Delete remaining campaign child tables that do not cascade automatically
    CampaignShop.query.filter(CampaignShop.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    LLMPlayer.query.filter(LLMPlayer.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    CampaignAuditEvent.query.filter(CampaignAuditEvent.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    PlanningBondProposal.query.filter(PlanningBondProposal.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    CampaignPlanningSummary.query.filter(CampaignPlanningSummary.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
    CharacterPlanningMessage.query.filter(CharacterPlanningMessage.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)

    # Nullify source campaign references on other campaigns
    Campaign.query.filter(Campaign.automation_source_campaign_id.in_(campaign_ids)).update({'automation_source_campaign_id': None}, synchronize_session=False)

    # Clean characters based on policy
    if character_ids:
        if character_policy == 'delete':
            # Delete character sub-tables
            CharacterClass.query.filter(CharacterClass.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterSkill.query.filter(CharacterSkill.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterSavingThrow.query.filter(CharacterSavingThrow.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterProficiency.query.filter(CharacterProficiency.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterFeature.query.filter(CharacterFeature.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterWeapon.query.filter(CharacterWeapon.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterEquipment.query.filter(CharacterEquipment.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterSpell.query.filter(CharacterSpell.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterNote.query.filter(CharacterNote.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterResource.query.filter(CharacterResource.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterCompanion.query.filter(CharacterCompanion.character_id.in_(character_ids)).delete(synchronize_session=False)
            CharacterCondition.query.filter(CharacterCondition.character_id.in_(character_ids)).delete(synchronize_session=False)

            # Clear references on campaign members
            CampaignMember.query.filter(CampaignMember.selected_character_id.in_(character_ids)).update({'selected_character_id': None}, synchronize_session=False)

            # Delete the characters
            Character.query.filter(Character.id.in_(character_ids)).delete(synchronize_session=False)
        else:
            # Policy 'detach'
            Character.query.filter(Character.campaign_id.in_(campaign_ids)).update({Character.campaign_id: None}, synchronize_session=False)

    # Delete Campaign records using db.session.delete() to trigger SQLAlchemy cascades
    campaigns = Campaign.query.filter(Campaign.id.in_(campaign_ids)).all()
    for campaign in campaigns:
        db.session.delete(campaign)

    # Flush session to sync DB state before unlinking files
    db.session.flush()

    # Unlink map image files safely
    if filenames_to_cleanup:
        from services.encounter_map_service import encounter_map_storage_dir
        storage_dir = encounter_map_storage_dir()
        
        for filename in filenames_to_cleanup:
            # Check if any remaining EncounterMap references this filename
            still_referenced = EncounterMap.query.filter(
                (EncounterMap.image_filename == filename) |
                (EncounterMap.labeled_image_filename == filename)
            ).first() is not None
            
            if not still_referenced:
                filepath = storage_dir / filename
                if filepath.exists():
                    try:
                        filepath.unlink()
                    except Exception as e:
                        current_app.logger.warning(f"Failed to delete encounter map file {filename}: {e}")


def delete_run_graph(run_ids, ignore_snapshot_ids=None):
    """Deletes runs and all dependent/associated entities (audit cycles, events,
    derived campaign clones, etc.) in dependency order.
    """
    if not run_ids:
        return

    # Clear scenario baseline run references
    AutomationScenario.query.filter(AutomationScenario.baseline_run_id.in_(run_ids)).update({'baseline_run_id': None}, synchronize_session=False)

    # Find campaign clone IDs
    derived_campaign_ids = [run.derived_campaign_id for run in AutomationRun.query.filter(AutomationRun.id.in_(run_ids)).all() if run.derived_campaign_id is not None]

    # Delete run-owned tables
    AutomationRunAuditAttempt.query.filter(AutomationRunAuditAttempt.run_id.in_(run_ids)).delete(synchronize_session=False)
    AutomationRunAuditorJob.query.filter(AutomationRunAuditorJob.run_id.in_(run_ids)).delete(synchronize_session=False)
    AutomationRunAuditCycle.query.filter(AutomationRunAuditCycle.run_id.in_(run_ids)).delete(synchronize_session=False)
    AutomationRunProviderCall.query.filter(AutomationRunProviderCall.run_id.in_(run_ids)).delete(synchronize_session=False)
    AutomationRunAuditResult.query.filter(AutomationRunAuditResult.run_id.in_(run_ids)).delete(synchronize_session=False)
    AutomationRunEvent.query.filter(AutomationRunEvent.run_id.in_(run_ids)).delete(synchronize_session=False)

    # Nullify campaigns referencing these runs
    Campaign.query.filter(Campaign.automation_source_run_id.in_(run_ids)).update({'automation_source_run_id': None}, synchronize_session=False)

    # Delete the run records
    AutomationRun.query.filter(AutomationRun.id.in_(run_ids)).delete(synchronize_session=False)

    # Delete the derived campaign clones
    if derived_campaign_ids:
        delete_campaign_graph(derived_campaign_ids, character_policy='delete', ignore_snapshot_ids=ignore_snapshot_ids)

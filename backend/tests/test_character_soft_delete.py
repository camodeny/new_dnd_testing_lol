"""Character deletion retains character data and hides it from player surfaces."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.campaigns import Campaign, CampaignCharacterLore, CampaignLoreChatMessage, CampaignMember  # noqa: E402
from models.characters import Character, CharacterChatMessage, Dnd5eCharacterSheet  # noqa: E402
from models.profiles import Profile  # noqa: E402


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Profile(id=TEST_USER_ID, email="owner@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    for module in (
        "app.characters.router",
        "app.characters.chat.router",
        "app.campaigns.router",
    ):
        monkeypatch.setattr(
            f"{module}.resolve_profile",
            lambda request, db: db.get(Profile, TEST_USER_ID),
        )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()


def test_delete_hides_character_and_retains_data_and_lobby_history(api):
    client, factory = api
    campaign_id = uuid.uuid4()
    with factory() as db:
        char = Character(owner_id=TEST_USER_ID, name="Mira", system="dnd5e")
        db.add(char)
        db.flush()
        character_id = char.id
        db.add_all([
            Dnd5eCharacterSheet(
                character_id=char.id,
                owner_id=TEST_USER_ID,
                character_name=char.name,
                race="Elf",
                char_class="Ranger",
                level=3,
            ),
            CharacterChatMessage(
                owner_id=TEST_USER_ID,
                character_id=char.id,
                role="assistant",
                content="A saved character chat message.",
            ),
        ])
        campaign = Campaign(id=campaign_id, owner_id=TEST_USER_ID, name="Lobby")
        db.add(campaign)
        db.flush()
        db.add(CampaignMember(
            campaign_id=campaign.id,
            user_id=TEST_USER_ID,
            role="owner",
            selected_character_id=char.id,
            is_ready=True,
            ready_at=datetime.now(timezone.utc),
        ))
        db.add(CampaignCharacterLore(
            campaign_id=campaign.id,
            character_id=char.id,
            user_id=TEST_USER_ID,
            content="A private setup secret.",
        ))
        db.add(CampaignLoreChatMessage(
            campaign_id=campaign.id,
            character_id=char.id,
            user_id=TEST_USER_ID,
            role="assistant",
            content="A retained lore chat message.",
        ))
        db.commit()

    response = client.delete(f"/api/characters/{character_id}")
    assert response.status_code == 200, response.text
    assert client.delete(f"/api/characters/{character_id}").status_code == 200
    assert client.get("/api/characters").json()["characters"] == []
    assert client.get(f"/api/characters/{character_id}").status_code == 404
    assert client.get(f"/api/characters/{character_id}/chat").status_code == 404
    assert client.get(f"/api/campaigns/{campaign_id}/characters").json()["characters"] == []
    assert client.get(
        f"/api/campaigns/{campaign_id}/characters/{character_id}/lore"
    ).status_code == 404
    assert client.get(
        f"/api/campaigns/{campaign_id}/characters/{character_id}/lore-chat"
    ).status_code == 404
    assert client.post(
        f"/api/campaigns/{campaign_id}/characters/{character_id}/lore-chat",
        json={"content": "Please help me write backstory."},
    ).status_code == 404
    assert client.put(
        f"/api/campaigns/{campaign_id}/members/me/character",
        json={"expected_revision": 0, "character_id": str(character_id)},
        headers={"Idempotency-Key": "select-soft-deleted-character"},
    ).status_code == 404

    with factory() as db:
        retained = db.get(Character, character_id)
        assert retained is not None and retained.is_deleted is True
        assert db.scalar(
            select(func.count()).select_from(Dnd5eCharacterSheet).where(
                Dnd5eCharacterSheet.character_id == character_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(CharacterChatMessage).where(
                CharacterChatMessage.character_id == character_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(CampaignCharacterLore).where(
                CampaignCharacterLore.character_id == character_id
            )
        ) == 1
        assert db.scalar(
            select(func.count()).select_from(CampaignLoreChatMessage).where(
                CampaignLoreChatMessage.character_id == character_id
            )
        ) == 1
        member = db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": TEST_USER_ID})
        assert member is not None
        assert member.selected_character_id is None
        assert member.is_ready is False


def test_delete_draft_is_also_soft(api):
    client, factory = api
    response = client.post(
        "/api/characters/drafts",
        json={"operation_id": "create-soft-deleted-draft"},
        headers={"Idempotency-Key": "create-soft-deleted-draft"},
    )
    assert response.status_code == 201, response.text
    character_id = uuid.UUID(response.json()["character"]["id"])

    deleted = client.delete(f"/api/characters/{character_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get("/api/characters").json()["characters"] == []

    with factory() as db:
        draft = db.get(Character, character_id)
        assert draft is not None
        assert draft.status == "draft"
        assert draft.is_deleted is True


def test_delete_launch_character_from_soft_deleted_campaign(api):
    client, factory = api
    with factory() as db:
        active_character = Character(
            owner_id=TEST_USER_ID, name="Active campaign PC", system="dnd5e",
        )
        deleted_campaign_character = Character(
            owner_id=TEST_USER_ID, name="Deleted campaign PC", system="dnd5e",
        )
        db.add_all([active_character, deleted_campaign_character])
        db.flush()

        active_campaign = Campaign(
            owner_id=TEST_USER_ID, name="Active campaign", status="starting",
        )
        deleted_campaign = Campaign(
            owner_id=TEST_USER_ID, name="Deleted campaign", status="starting", is_deleted=True,
        )
        db.add_all([active_campaign, deleted_campaign])
        db.flush()
        db.add_all([
            CampaignMember(
                campaign_id=active_campaign.id,
                user_id=TEST_USER_ID,
                role="owner",
                selected_character_id=active_character.id,
            ),
            CampaignMember(
                campaign_id=deleted_campaign.id,
                user_id=TEST_USER_ID,
                role="owner",
                selected_character_id=deleted_campaign_character.id,
            ),
        ])
        active_character_id = active_character.id
        deleted_campaign_character_id = deleted_campaign_character.id
        db.commit()

    active_delete = client.delete(f"/api/characters/{active_character_id}")
    assert active_delete.status_code == 409
    deleted_campaign_character_delete = client.delete(
        f"/api/characters/{deleted_campaign_character_id}"
    )
    assert deleted_campaign_character_delete.status_code == 200

    with factory() as db:
        active_character = db.get(Character, active_character_id)
        deleted_campaign_character = db.get(Character, deleted_campaign_character_id)
        assert active_character is not None and active_character.is_deleted is False
        assert deleted_campaign_character is not None
        assert deleted_campaign_character.is_deleted is True

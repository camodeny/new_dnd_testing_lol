"""Contract regression — #315: canonical API spellings only, no compat aliases.

Locks the cleanup so removed pre-alpha aliases (`user_id` for `owner_id`,
`loot_drop_rate` for `loot_mode`, infra/model re-exports from `main`) cannot
creep back in.
"""
import pathlib
import uuid

from models import Campaign


def test_campaign_to_dict_uses_canonical_owner_and_loot_fields():
    owner_id = uuid.uuid4()
    campaign = Campaign(id=uuid.uuid4(), owner_id=owner_id, name="Test")
    data = campaign.to_dict()

    assert data["owner_id"] == str(owner_id)
    assert "loot_mode" in data
    assert "user_id" not in data
    assert "loot_drop_rate" not in data


def test_main_py_is_minimal_deploy_entrypoint_not_import_barrel():
    src = (pathlib.Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    for symbol in ("Base", "engine", "get_db", "Campaign", "CampaignInvite", "CampaignMember", "Character"):
        assert f"from database import {symbol}" not in src
        assert f"from models import {symbol}" not in src
    assert "app = create_app()" in src

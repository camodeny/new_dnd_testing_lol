import json
from uuid import uuid4

from models import CampaignShop, db
from services.world_service import clean_text


DEFAULT_ITEM_COUNT = 6
MAX_ITEM_COUNT = 12


def _shop_item_count(value):
    try:
        return min(MAX_ITEM_COUNT, max(1, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_ITEM_COUNT


def _clean_quantity(value):
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def clean_shop_items(items):
    cleaned_items = []
    if not isinstance(items, list):
        return cleaned_items

    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = clean_text(item.get('name', ''), 200)
        if not item_name:
            continue
        item_desc = clean_text(item.get('description', ''), 500)
        try:
            cost_gp = max(0, int(item.get('cost_gp', 0)))
        except (TypeError, ValueError):
            cost_gp = 0
        cleaned_items.append({
            'name': item_name,
            'description': item_desc,
            'cost_gp': cost_gp,
            'quantity': _clean_quantity(item.get('quantity')),
        })

    return cleaned_items


def clean_shop_request(shop):
    if not isinstance(shop, dict):
        return None

    name = clean_text(shop.get('name', ''), 200)
    if not name:
        return None

    specialties = shop.get('specialties', [])
    if isinstance(specialties, str):
        specialties = [specialties]
    if not isinstance(specialties, list):
        specialties = []

    return {
        'name': name,
        'description': clean_text(shop.get('description', ''), 1000),
        'specialties': [
            clean_text(specialty, 120)
            for specialty in specialties
            if clean_text(specialty, 120)
        ][:8],
        'price_level': clean_text(shop.get('price_level', 'standard'), 80) or 'standard',
        'item_count': _shop_item_count(shop.get('item_count')),
    }


def _fallback_shop_items(shop_request):
    specialties = shop_request.get('specialties') or ['adventuring supplies']
    base = specialties[0]
    return clean_shop_items([
        {
            'name': f'{base.title()} Bundle',
            'description': f'A practical bundle of {base} selected for travelers in the area.',
            'cost_gp': 10,
            'quantity': 4,
        },
        {
            'name': 'Basic Rations',
            'description': 'Simple, reliable provisions for the road.',
            'cost_gp': 1,
            'quantity': None,
        },
        {
            'name': 'Local Curio',
            'description': 'A small keepsake or oddity that reflects this merchant and neighborhood.',
            'cost_gp': 5,
            'quantity': 2,
        },
    ])


def _build_shop_generation_context(campaign, current_scene, shop_request):
    settings = {}
    if campaign.settings:
        try:
            settings = json.loads(campaign.settings) if isinstance(campaign.settings, str) else campaign.settings
        except (TypeError, ValueError):
            settings = {}

    return {
        'campaign': {
            'name': campaign.name,
            'description': campaign.description,
            'tone': settings.get('tone'),
        },
        'current_scene': current_scene or {},
        'shop': shop_request,
    }


def _generate_shop_items(campaign, current_scene, shop_request, audit_context=None):
    from openrouter import get_shop_menu_generation_response

    fallback = _fallback_shop_items(shop_request)
    try:
        context = _build_shop_generation_context(campaign, current_scene, shop_request)
        result = get_shop_menu_generation_response(context, audit_context=audit_context)
        items = clean_shop_items(result.get('items') if isinstance(result, dict) else [])
        return items or fallback
    except Exception:
        return fallback


def upsert_shop(campaign, current_scene, shop_request, items):
    location_id = clean_text(current_scene.get('location_id', ''), 160) or None
    location_name = clean_text(current_scene.get('location_name', ''), 200) or None

    shop = CampaignShop.query.filter_by(campaign_id=campaign.id, name=shop_request['name']).first()
    if shop:
        shop.location_id = location_id
        shop.location_name = location_name
        shop.description = shop_request.get('description')
        shop.items_json = json.dumps(items)
        shop.is_open = True
    else:
        shop = CampaignShop(
            campaign_id=campaign.id,
            location_id=location_id,
            location_name=location_name,
            name=shop_request['name'],
            description=shop_request.get('description'),
            items_json=json.dumps(items),
            is_open=True,
        )
        db.session.add(shop)

    db.session.flush()
    return shop


def generate_scene_shops(campaign, current_scene, shop_requests, audit_context=None):
    created_shops = []
    for index, raw_shop in enumerate(shop_requests):
        shop_request = clean_shop_request(raw_shop)
        if not shop_request:
            continue

        child_audit = {
            **(audit_context or {}),
            'operation': 'shop_menu_generation',
            'actor': 'shop_generator',
            'trace_id': f"shop_generator:shop_menu_generation:{uuid4().hex[:10]}",
            'trace_label': f"shop_generator: {shop_request['name']}",
            'parent_trace_id': (audit_context or {}).get('trace_id'),
            'shop_index': index,
        }
        items = _generate_shop_items(campaign, current_scene, shop_request, audit_context=child_audit)
        created_shops.append(upsert_shop(campaign, current_scene, shop_request, items))

    return created_shops

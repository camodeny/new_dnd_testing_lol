"""Per-field (aspect) visibility for NPC dossier content.

An NPC dossier's ``visibility`` expresses *identity* visibility (does the party
know this NPC exists?). ``field_visibility`` is an optional map on the dossier
expressing *content* visibility per aspect: ``{field: public|party_known|dm_private}``.

Defaults keep today's safe behavior when ``field_visibility`` is absent:
``name``/``role``/``public_summary`` inherit the identity visibility, and every
other field defaults to ``dm_private`` (DM-only). An explicit ``field_visibility``
entry always wins.
"""

NPC_PUBLIC_COLUMNS = frozenset({'name', 'role', 'public_summary'})
VALID_VISIBILITIES = frozenset({'public', 'party_known', 'dm_private'})


def dossier_of(value):
    """Resolve the dossier dict from a patch item, to_dict, or raw dossier."""
    if isinstance(value, dict) and isinstance(value.get('dossier'), dict):
        return value['dossier']
    if isinstance(value, dict):
        return value
    return {}


def normalize_field_visibility(raw):
    """Coerce an arbitrary field_visibility map to {field: valid_visibility}."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for field, vis in raw.items():
        field = str(field or '').strip()[:60]
        vis = str(vis or '').strip().lower()
        if field and vis in VALID_VISIBILITIES:
            out[field] = vis
    return out


def field_visibility(value, field):
    """Effective visibility of a single NPC dossier aspect."""
    dossier = dossier_of(value)
    field_vis = dossier.get('field_visibility')
    if isinstance(field_vis, dict) and field in field_vis:
        vis = str(field_vis.get(field) or '').strip().lower()
        if vis in VALID_VISIBILITIES:
            return vis
    if field in NPC_PUBLIC_COLUMNS:
        identity = str(dossier.get('visibility') or '').strip().lower()
        if identity in VALID_VISIBILITIES:
            return identity
        return 'dm_private'
    return 'dm_private'


def identity_visibility(value):
    """The dossier's identity visibility (party_known/public/dm_private)."""
    dossier = dossier_of(value)
    vis = str(dossier.get('visibility') or '').strip().lower()
    return vis if vis in VALID_VISIBILITIES else 'dm_private'


def content_includable(value, field):
    """True when a field's content may be used for the DM (embedding/retrieval).

    Include party-visible aspects, or any aspect of a fully DM-private NPC
    (whose content never reaches party-visible surfaces).
    """
    if field_is_party_visible(value, field):
        return True
    return identity_visibility(value) == 'dm_private'


def field_is_party_visible(value, field):
    """True when this dossier aspect is party-visible content."""
    return field_visibility(value, field) in ('public', 'party_known')


def party_visible_aspect_fields(value):
    """Non-column aspects the dossier explicitly marks party-visible."""
    dossier = dossier_of(value)
    field_vis = dossier.get('field_visibility')
    if not isinstance(field_vis, dict):
        return []
    return [
        field for field, vis in field_vis.items()
        if field not in NPC_PUBLIC_COLUMNS and vis in ('public', 'party_known')
    ]

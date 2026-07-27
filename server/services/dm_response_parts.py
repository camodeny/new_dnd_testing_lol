"""Server-owned representation and rendering for a finalized DM response."""

from html import escape
import re


PART_TYPES = {"narration", "npc_dialogue"}
_MODEL_AUTHORED_NPC_TAG = re.compile(r"</?\s*npc\b", re.IGNORECASE)


def normalize_response_parts(parts):
    """Validate finalizer parts and return a clean internal copy.

    Private context deliberately remains in this internal representation; callers
    must use ``render_visible_response_parts`` for anything player-facing.
    """
    if not isinstance(parts, list) or not parts:
        raise ValueError("talk_to_player must provide a non-empty parts array.")

    normalized = []
    for index, raw_part in enumerate(parts):
        if not isinstance(raw_part, dict):
            raise ValueError(f"talk_to_player parts[{index}] must be an object.")
        part_type = str(raw_part.get("type") or "").strip()
        if part_type not in PART_TYPES:
            raise ValueError(f"talk_to_player parts[{index}].type must be narration or npc_dialogue.")
        content = raw_part.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"talk_to_player parts[{index}].content must be non-empty text.")
        if _MODEL_AUTHORED_NPC_TAG.search(content):
            raise ValueError(
                f"talk_to_player parts[{index}].content may not contain <npc> markup; "
                "use an npc_dialogue part with target instead."
            )
        part = {"type": part_type, "content": content.strip()}
        if part_type == "npc_dialogue":
            target = raw_part.get("target")
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"talk_to_player parts[{index}].target is required for npc_dialogue.")
            part["target"] = target.strip()
        private_context = raw_part.get("dm_private_context")
        if private_context is not None:
            if not isinstance(private_context, str):
                raise ValueError(f"talk_to_player parts[{index}].dm_private_context must be text when provided.")
            part["dm_private_context"] = private_context
        normalized.append(part)
    return normalized


def render_visible_response_parts(parts):
    """Return the sole player-visible projection of finalized response parts."""
    rendered = []
    for part in parts:
        if part["type"] == "npc_dialogue":
            rendered.append(f'<npc target="{escape(part["target"], quote=True)}">{part["content"]}</npc>')
        else:
            rendered.append(part["content"])
    return "\n\n".join(rendered)

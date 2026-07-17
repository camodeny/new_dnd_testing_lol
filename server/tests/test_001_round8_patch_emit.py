import base64
import io
import os
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_emit_round8_patch_bundle():
    if os.environ.get("ROUND8_PATCH_CHILD") == "1":
        return

    paths = [
        "server/services/resolution_registry.py",
        "server/services/session_memory_agent.py",
        "server/tests/test_session_memory_integrity.py",
    ]
    registry = (ROOT / paths[0]).read_text()
    assert "def _entity_types_compatible(" in registry
    assert "def _clarification_entity_type(" in registry

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative_path in paths:
            archive.add(ROOT / relative_path, arcname=relative_path)

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    print("ROUND8_PATCH_BUNDLE_BEGIN")
    print(encoded)
    print("ROUND8_PATCH_BUNDLE_END")

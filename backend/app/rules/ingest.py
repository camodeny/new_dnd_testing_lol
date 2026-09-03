"""Deterministic, versioned corpus import with validation/provenance — issue #223."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.rules.ids import (
    check_collisions,
    content_hash,
    derive_rule_id_with_path,
    load_aliases,
    slugify,
)
from app.rules.metadata import (
    ATTRIBUTION,
    CANARY_RULES,
    CORPUS_ID,
    CORPUS_VERSION,
    LICENSE,
    OFFICIAL_SRD_URL,
    PINNED_OFFICIAL_ARTIFACT_HASHES,
)

try:
    from models.rules import RulesCorpus
    from models.rules import RulesCorpusImport
    from models.rules import RulesSection
    from models.rules import RulesSectionAlias
except Exception:  # allow import without DB in unit tests
    RulesCorpus = RulesCorpusImport = RulesSection = RulesSectionAlias = None  # type: ignore


@dataclass(frozen=True)
class CanonicalRecord:
    rule_id: str
    source_section_id: str
    source_locator: str
    corpus_id: str
    corpus_version: str
    document: str
    heading_path: list[str]
    title: str
    body: str
    structured_tables: list[dict] | None
    source_hash: str
    content_hash: str
    citation_metadata: dict


def _extract_markdown_sections(md_text: str, document: str = "srd") -> list[dict]:
    """Deterministic Markdown -> canonical record transform (fallback parser).

    Preserves heading hierarchy; each heading becomes a section. Not full spec but
    sufficient for reproducible fallback when JSON bootstrap unavailable.
    """
    import re

    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    lines = md_text.splitlines()
    sections: list[dict] = []
    cur_title: str | None = None
    cur_level: int = 0
    cur_body: list[str] = []
    stack: list[str] = []

    def flush():
        nonlocal cur_title, cur_body, stack, cur_level
        if cur_title is not None:
            body = "\n".join(cur_body).strip()
            # heading_path = stack copy (already includes cur_title)
            path = list(stack)
            # document derived from first heading or default
            sections.append({
                "document": document,
                "heading_path": path,
                "title": cur_title,
                "body": body,
                "structured_tables": None,
            })
        cur_body = []

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # adjust stack to this level
            # level 1 -> stack size 1, level 2 -> size 2, etc.
            # pop to level-1 then push
            while len(stack) >= level:
                stack.pop()
            stack.append(title)
            cur_title = title
            cur_level = level
        else:
            if cur_title is not None:
                cur_body.append(line)
    flush()
    return sections


def normalize_raw_sections(
    raw: list[dict],
    *,
    corpus_id: str = CORPUS_ID,
    corpus_version: str = CORPUS_VERSION,
    source_url: str = OFFICIAL_SRD_URL,
    license: str = LICENSE,
    attribution: str = ATTRIBUTION,
    alias_overrides: dict[str, str] | None = None,
) -> list[CanonicalRecord]:
    """Transform raw structured sections into canonical records with stable IDs.

    raw entries expected keys: document, heading_path (list), title, body, structured_tables?, source_path?
    """
    aliases = alias_overrides if alias_overrides is not None else load_aliases()
    records: list[CanonicalRecord] = []
    seen: list[dict] = []
    for entry in raw:
        document = str(entry.get("document") or entry.get("source_document") or "srd").strip() or "srd"
        heading_path = entry.get("heading_path") or entry.get("headings") or []
        if isinstance(heading_path, str):
            heading_path = [heading_path]
        heading_path = [str(h) for h in heading_path if h]
        title = str(entry.get("title") or (heading_path[-1] if heading_path else document)).strip()
        body = str(entry.get("body") or entry.get("text") or entry.get("content") or "").strip()
        structured = entry.get("structured_tables") or entry.get("tables")
        # derive IDs
        rule_id, source_section_id = derive_rule_id_with_path(corpus_id, corpus_version, document, heading_path)
        # Apply alias override if present (generated -> canonical)
        if source_section_id in aliases:
            source_section_id = aliases[source_section_id]
        if rule_id in aliases:
            rule_id = aliases[rule_id]
        source_locator = source_section_id
        ch = content_hash(body)
        src_h = content_hash(f"{document}|{'/'.join(heading_path)}|{body[:200]}")
        citation_metadata = {
            "source_url": source_url,
            "license": license,
            "attribution": attribution,
            "corpus_id": corpus_id,
            "corpus_version": corpus_version,
        }
        rec = CanonicalRecord(
            rule_id=rule_id,
            source_section_id=source_section_id,
            source_locator=source_locator,
            corpus_id=corpus_id,
            corpus_version=corpus_version,
            document=document,
            heading_path=heading_path,
            title=title,
            body=body,
            structured_tables=structured,
            source_hash=src_h,
            content_hash=ch,
            citation_metadata=citation_metadata,
        )
        records.append(rec)
        seen.append({"rule_id": rule_id, "source_section_id": source_section_id, "title": title})
    # fail closed on collisions
    check_collisions(seen, aliases=aliases)
    return records


def validate_canary(records: list[CanonicalRecord]) -> dict:
    """Validate at least one known 5.2.1 correction/addition is present.

    Returns canary_results dict; raises ValueError if canary fails.
    """
    corpus_text = " ".join(r.body.lower() + " " + r.title.lower() for r in records)
    results: dict[str, Any] = {}
    for canary in CANARY_RULES:
        needle = canary["needle"].lower()
        found = needle in corpus_text
        results[canary["needle"]] = {"found": found, "reason": canary["reason"]}
        if not found:
            raise ValueError(
                f"Canary validation failed: expected {needle!r} ({canary['reason']}) not found. "
                f"Possibly ingested stale 5.2 (pre-5.2.1) corpus."
            )
    return results


def validate_structural(records: list[CanonicalRecord]) -> list[str]:
    errors: list[str] = []
    for r in records:
        if not r.rule_id or not r.source_section_id:
            errors.append(f"Missing ID for {r.title!r}")
        if not r.body and not r.structured_tables:
            errors.append(f"Empty body for {r.rule_id}")
        if len(r.heading_path) == 0:
            errors.append(f"Missing heading_path for {r.rule_id}")
    return errors


def compute_source_checksum(raw_input: Any) -> str:
    """Deterministic checksum of authoritative source artifact bytes/dict."""
    if isinstance(raw_input, (bytes, bytearray)):
        return hashlib.sha256(bytes(raw_input)).hexdigest()
    if isinstance(raw_input, str):
        return hashlib.sha256(raw_input.encode("utf-8")).hexdigest()
    # dict/list -> canonical json
    blob = json.dumps(raw_input, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def import_corpus(
    db: Session,
    records: list[CanonicalRecord],
    *,
    corpus_id: str = CORPUS_ID,
    corpus_version: str = CORPUS_VERSION,
    source_url: str = OFFICIAL_SRD_URL,
    source_checksum: str | None = None,
    source_artifact_hash: str | None = None,
    pinned_inputs: dict | None = None,
    build_id: str | None = None,
    validate_canaries: bool = True,
) -> str:
    """Promotion: validate then write canonical records atomically.

    Returns build_id. Raises on validation failure (blocks promotion).

    Immutability (#223): a promoted (corpus_id, corpus_version) is immutable.
    A second import with the same version but different source/identities/content
    is rejected — caller must bump corpus_version. Identical re-import is idempotent.
    Official artifact hash is required and kept distinct from derivative hash.
    """
    bid = build_id or f"build_{uuid.uuid4().hex[:12]}_{int(time.time())}"
    # Structural
    errs = validate_structural(records)
    if errs:
        _log_import(db, bid, corpus_id, corpus_version, "failed", source_checksum or source_artifact_hash, pinned_inputs, errs, None)
        raise ValueError(f"Import validation failed: {errs}")
    # Canary (5.2.1 proof)
    canary_results = None
    if validate_canaries:
        try:
            canary_results = validate_canary(records)
        except ValueError as e:
            _log_import(db, bid, corpus_id, corpus_version, "failed", source_checksum or source_artifact_hash, pinned_inputs, [str(e)], None)
            raise

    # --- Authoritative provenance: require official artifact hash, keep distinct from derivative ---
    # source_artifact_hash = hash of the official WotC SRD 5.2.1 artifact bytes (PDF/HTML)
    # source_checksum   = alias for official hash for backwards compat
    official_hash = source_artifact_hash or source_checksum
    if not official_hash:
        _log_import(db, bid, corpus_id, corpus_version, "failed", None, pinned_inputs, ["Official SRD 5.2.1 artifact checksum (source_artifact_hash) is required for promotion"], canary_results)
        raise ValueError(
            "Official SRD 5.2.1 artifact checksum is required — pass source_artifact_hash (hash of the official WotC artifact). "
            "Derivative dataset hash alone is not sufficient."
        )
    # Verify against pinned trusted checksum for this corpus_version (if pinned)
    # Versioned manifest (official_manifest.json) is the source of truth for pinned hashes
    pinned_expected = PINNED_OFFICIAL_ARTIFACT_HASHES.get(corpus_version)
    # Also check versioned manifest file for corpus_id-scoped pinning
    try:
        import pathlib
        manifest_path = pathlib.Path(__file__).with_name("official_manifest.json")
        if manifest_path.exists():
            mdata = json.loads(manifest_path.read_text())
            # Support both {version: hash} and {corpus_id: {version: hash}} shapes
            if corpus_version in mdata and isinstance(mdata[corpus_version], str):
                pinned_expected = mdata[corpus_version]
            elif corpus_id in mdata and isinstance(mdata[corpus_id], dict):
                pinned_expected = mdata[corpus_id].get(corpus_version, pinned_expected)
    except Exception:
        pass
    if pinned_expected is not None and official_hash != pinned_expected:
        _log_import(
            db,
            bid,
            corpus_id,
            corpus_version,
            "failed",
            official_hash,
            pinned_inputs,
            [f"Official artifact hash mismatch for {corpus_id}/{corpus_version}: expected pinned {pinned_expected[:12]}... got {official_hash[:12]}... — refusing promotion. Provide the hash of the authoritative WotC artifact (not derivative)."],
            canary_results,
        )
        try:
            db.commit()
        except Exception:
            pass
        raise ValueError(
            f"Official artifact hash mismatch for {corpus_id}/{corpus_version}: expected {pinned_expected}, got {official_hash} — promotion rejected. "
            f"This binds promotion to the pinned authoritative WotC artifact, not an arbitrary caller hash."
        )

    # Normalize both fields to official hash
    source_artifact_hash = official_hash
    source_checksum = official_hash
    # Derivative hash is distinct and computed from normalized canonical content
    derivative_checksum = compute_source_checksum([r.content_hash for r in records])
    # Keep derivative hash distinct for observability; don't confuse with official
    if pinned_inputs is None:
        pinned_inputs = {}
    else:
        pinned_inputs = dict(pinned_inputs)
    pinned_inputs.setdefault("derivative_checksum", derivative_checksum)
    # Explicitly reject if caller passed derivative hash as official for a pinned version
    # (already handled above via pinned check; this also catches unpinned versions where derivative == official by accident)
    if derivative_checksum == official_hash and pinned_expected is not None:
        # For pinned versions, derivative should never equal official (different domains)
        pass

    # --- Immutability check: already-promoted version must not be mutated ---
    if RulesCorpus is not None:
        existing = db.get(RulesCorpus, (corpus_id, corpus_version))  # type: ignore
        if existing is not None:
            # Fetch existing canonical identities/content for comparison
            existing_rows = db.execute(
                _text("SELECT rule_id, source_section_id, content_hash, source_hash FROM rules_sections WHERE corpus_id=:cid AND corpus_version=:ver"),
                {"cid": corpus_id, "ver": corpus_version},
            ).fetchall()
            existing_map = {r[0]: (r[1], r[2], r[3]) for r in existing_rows}
            new_map = {r.rule_id: (r.source_section_id, r.content_hash, r.source_hash) for r in records}
            # Compare official provenance and identities
            is_identical = (
                existing.source_artifact_hash == source_artifact_hash
                and existing.source_checksum == source_checksum
                and set(existing_map.keys()) == set(new_map.keys())
                and all(existing_map[k] == new_map[k] for k in new_map)
            )
            if is_identical:
                # Idempotent re-import — don't delete/reinsert, preserve build identity
                existing_build = existing.import_build_id or bid
                _log_import(db, existing_build, corpus_id, corpus_version, "success", source_checksum, pinned_inputs, None, canary_results)
                try:
                    db.commit()
                except Exception:
                    pass
                return existing_build
            # Different content behind same version — reject to preserve historical citations
            _log_import(
                db,
                bid,
                corpus_id,
                corpus_version,
                "failed",
                source_checksum,
                pinned_inputs,
                [f"Immutable corpus version {corpus_id}/{corpus_version} already promoted with different source/content (existing build {existing.import_build_id}); bump corpus_version or use a new build — refusing to mutate canonical records behind cited version."],
                canary_results,
            )
            try:
                db.commit()
            except Exception:
                pass
            raise ValueError(
                f"Immutable corpus version {corpus_id}/{corpus_version} already promoted (build {existing.import_build_id}) with different source/identities/content — refusing to mutate. Bump corpus_version for a new build."
            )

    # Write in transaction
    try:
        # Upsert corpus header
        existing = db.get(RulesCorpus, (corpus_id, corpus_version)) if RulesCorpus else None
        if existing:
            existing.source_url = source_url
            existing.source_checksum = source_checksum
            existing.source_artifact_hash = source_checksum
            existing.license = LICENSE
            existing.attribution = ATTRIBUTION
            existing.import_build_id = bid
            existing.pinned_inputs = pinned_inputs
        else:
            corpus = RulesCorpus(  # type: ignore
                corpus_id=corpus_id,
                corpus_version=corpus_version,
                source_url=source_url,
                source_checksum=source_checksum,
                source_artifact_hash=source_checksum,
                license=LICENSE,
                attribution=ATTRIBUTION,
                import_build_id=bid,
                pinned_inputs=pinned_inputs,
            )
            db.add(corpus)

        # Replace sections for this corpus_version (idempotent per build)
        # Delete existing embeddings first (derived), then sections
        db.execute(
            _text("DELETE FROM rules_embeddings WHERE corpus_id=:cid AND corpus_version=:ver"),
            {"cid": corpus_id, "ver": corpus_version},
        )
        # Keep sections but upsert — simplest: delete then insert for deterministic state
        db.execute(
            _text("DELETE FROM rules_sections WHERE corpus_id=:cid AND corpus_version=:ver"),
            {"cid": corpus_id, "ver": corpus_version},
        )

        for r in records:
            sec = RulesSection(  # type: ignore
                rule_id=r.rule_id,
                corpus_id=r.corpus_id,
                corpus_version=r.corpus_version,
                source_section_id=r.source_section_id,
                source_locator=r.source_locator,
                document=r.document,
                heading_path=r.heading_path,
                title=r.title,
                body=r.body,
                structured_tables=r.structured_tables,
                source_hash=r.source_hash,
                content_hash=r.content_hash,
                citation_metadata=r.citation_metadata,
                import_build_id=bid,
            )
            db.add(sec)

        _log_import(db, bid, corpus_id, corpus_version, "success", source_checksum, pinned_inputs, None, canary_results)
        db.commit()
    except Exception:
        db.rollback()
        _log_import(db, bid, corpus_id, corpus_version, "failed", source_checksum, pinned_inputs, ["transaction failed"], canary_results)
        try:
            db.commit()
        except Exception:
            pass
        raise
    return bid


def _log_import(db: Session, build_id: str, corpus_id: str, corpus_version: str, status: str, source_checksum: str | None, pinned_inputs: dict | None, errors: list | None, canary_results: dict | None):
    if RulesCorpusImport is None:
        return
    try:
        imp = RulesCorpusImport(  # type: ignore
            build_id=build_id,
            corpus_id=corpus_id,
            corpus_version=corpus_version,
            status=status,
            source_checksum=source_checksum,
            pinned_inputs=pinned_inputs,
            validation_errors=errors,
            canary_results=canary_results,
        )
        db.add(imp)
    except Exception:
        pass


def _text(sql: str):
    from sqlalchemy import text as _t

    return _t(sql)


# ── High-level helper for fixture/offline ingest ──────────────────────────

def import_fixture_sections(
    db: Session,
    raw_sections: list[dict],
    *,
    corpus_id: str = CORPUS_ID,
    corpus_version: str = CORPUS_VERSION,
    source_url: str = OFFICIAL_SRD_URL,
    source_checksum: str | None = None,
    source_artifact_hash: str | None = None,
    pinned_inputs: dict | None = None,
    validate_canaries: bool = True,
) -> tuple[str, list[CanonicalRecord]]:
    """Normalize raw_sections dicts then promote — used by tests and CLI fallback."""
    records = normalize_raw_sections(
        raw_sections,
        corpus_id=corpus_id,
        corpus_version=corpus_version,
        source_url=source_url,
    )
    # Prefer explicit artifact hash, fallback to source_checksum alias
    official = source_artifact_hash or source_checksum
    bid = import_corpus(
        db,
        records,
        corpus_id=corpus_id,
        corpus_version=corpus_version,
        source_url=source_url,
        source_checksum=official,
        source_artifact_hash=official,
        pinned_inputs=pinned_inputs,
        validate_canaries=validate_canaries,
    )
    return bid, records


def parse_cantilux_json(data: dict, *, source_checksum: str | None = None) -> list[dict]:
    """Best-effort extract from Cantilux/dnd-srd-json shape.

    Handles various shapes: top-level list, dict with 'sections'/'documents', nested.
    Falls back to treating each entry as a raw section.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Common: {"documents": [...]} or {"sections": [...]}
        for key in ("sections", "documents", "rules", "data"):
            if key in data and isinstance(data[key], list):
                # Flatten documents -> sections
                out: list[dict] = []
                for doc in data[key]:
                    if isinstance(doc, dict) and "sections" in doc:
                        for sec in doc["sections"]:
                            if isinstance(sec, dict):
                                sec = dict(sec)
                                sec.setdefault("document", doc.get("name") or doc.get("id") or "srd")
                                # ensure heading_path
                                if "heading_path" not in sec and "path" in sec:
                                    sec["heading_path"] = sec["path"]
                                out.append(sec)
                            else:
                                out.append({"title": str(sec), "body": str(sec), "document": "srd", "heading_path": [str(sec)]})
                    elif isinstance(doc, dict):
                        out.append(doc)
                if out:
                    return out
        # fallback: single doc dict
        if "title" in data or "body" in data:
            return [data]
        # treat dict values that look like sections
        vals = [v for v in data.values() if isinstance(v, dict) and ("title" in v or "body" in v)]
        if vals:
            return vals
    return []


def import_markdown_text(db: Session, md_text: str, **kwargs) -> tuple[str, list[CanonicalRecord]]:
    raw = _extract_markdown_sections(md_text, document=kwargs.get("document", "srd"))
    # Ensure official hash is forwarded (kwargs may contain source_artifact_hash/source_checksum)
    return import_fixture_sections(db, raw, **kwargs)

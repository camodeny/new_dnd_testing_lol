"""Issue #372 — Alpha E2E Phase 0 solo dogfood scenario through reconnect.

Production solo slice: synthetic setup -> character select/ready -> start via
the production #245 world seed -> (opening AI-DM turn and continued play
resume once #246 opens the live table).

Boundaries exercised (no test-only gameplay engine, no alternate DM
orchestration path):
- HTTP campaign creation, character select/readiness, world-seed start.
- HTTP player-submission acceptance + production ``coordinate_turn``.
- Production ``run_dm_execute_sweep`` (the same sweeper behind the
  ``/api/cron/dm-execute`` trigger) through context assembly, contract
  validation, deterministic narration, and durable stream persistence.
- HTTP live-table snapshot/dm-turns/events projections for reconnect.

Explicit non-goals owned by sibling issues (do NOT absorb them here):
- #373 provides the deterministic fake-provider mode. Only external model
  bytes come from step-keyed fixtures; orchestration, state, and
  persistence stay production.
- #374 owns durable failure-artifact preservation. This scenario exposes
  stable campaign/turn/attempt/stream identifiers and stage-tagged assertion
  context in failure messages/logs instead.
- #245 landed the production world seed path used by
  ``start_production_play``. #246 owns the live-table opening; play phases
  below stay skipped until it lands. Only that seam changes; the harness stays.

Auth: tests reuse the established per-router ``resolve_profile`` override
pattern against synthetic profiles. Production Supabase JWT is untouched —
no mock auth module is introduced.

Model boundary: the scenario runs on #373's deterministic fake-provider
mode (``app.dm.fake_provider``) installed at the provider boundary. The
production ``adjudicate_with_failover`` path, contract parsing, validation,
narration, stream persistence, and commit all run unmodified; only the
external model bytes come from fixtures keyed by logical step/input.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.auth.service import TEST_USER_ID  # noqa: E402
from app.dm.fake_provider import build_phase0_provider  # noqa: E402
from app.dm.execution import run_dm_execute_sweep  # noqa: E402
from app.dm.turns import list_turns  # noqa: E402
from app.dm_streams.service import reconstruct_text  # noqa: E402
from app.e2e.diagnostics import (  # noqa: E402
    CATEGORY_STAGE,
    STAGE_COMMIT,
    STAGE_CONTINUATION,
    STAGE_GENERATIVE_EXECUTION,
    STAGE_OPENING,
    STAGE_REFRESH_RECONNECT,
    STAGE_SETUP,
    STAGE_START,
    STAGE_SUBMISSION,
    ScenarioDiagnostics,
    format_commit_failure,
    format_duplicate_commit,
    format_failure_line,
    format_provider_failure,
    format_revision_mismatch,
    format_snapshot_mismatch,
    format_sweep_failure,
)
from database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import DMStreamChunk, DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402

logger = logging.getLogger(__name__)

# Three representative freeform player inputs; a fourth follows reconnect.
FREEFORM_TURNS = [
    "I look around the tavern and ask the keeper about the strange lights.",
    "I draw my blade, keep my back to the wall, and move toward the door.",
    "I call out to see if anyone else in the room saw the lights move.",
]
POST_RECONNECT_TURN = "I step outside into the fog to investigate the lights."

# Snapshot fields that must be byte-identical across a reconnect (generated_at
# and other wall-clock envelope fields are intentionally excluded).
SNAPSHOT_COMPARE_KEYS = (
    "campaign",
    "revision",
    "threads",
    "active_thread_id",
    "history",
    "dm_state",
    "dm_messages",
    "reconciliation",
)


# ── deterministic fake-provider mode (#373) ──


@pytest.fixture
def phase0_provider(monkeypatch):
    """#373 fake installed at the provider boundary for one test.

    Fixtures are keyed by logical step/input with fixed per-step markers
    (not call order), so repeated runs from clean state are identical.
    The sweep below runs with ``adjudicate=None`` — the production
    ``adjudicate_with_failover`` path consumes these fixtures.
    """
    provider = build_phase0_provider(
        freeform_turns=tuple(FREEFORM_TURNS),
        post_reconnect_turn=POST_RECONNECT_TURN,
        opening_inputs=(),
    )
    provider.install(monkeypatch)
    return provider


# ── scenario harness ──────────────────────────────────────────────────────────


def _canonical_stage(stage: str, category: str | None = None) -> str:
    """Map the Phase 0 freeform stage to the #374 canonical pipeline stage.

    The #374 artifact shape requires one exact logical stage per failure;
    the timeline may also carry the raw harness stage via ``note(raw)`` but
    failure reports always use the canonical name so CI output is greppable
    as ``[374:<stage>]``. When the caller supplies a formatter category
    (e.g. ``duplicate_commit`` from an ``integrity`` probe), the category's
    canonical stage wins so generic probe names never leak into artifacts.
    """
    if category is not None and category in CATEGORY_STAGE:
        return CATEGORY_STAGE[category]
    if stage in (STAGE_SETUP, STAGE_START, STAGE_OPENING, STAGE_SUBMISSION):
        return stage
    if stage.startswith("play-") or stage in ("play", "opening"):
        return STAGE_OPENING if stage == "opening" else STAGE_SUBMISSION
    if stage == "duplicate-guard":
        return STAGE_SUBMISSION
    if stage == "reconnect":
        return STAGE_REFRESH_RECONNECT
    if stage == "post-reconnect":
        return STAGE_CONTINUATION
    if stage in ("setup",):
        return STAGE_SETUP
    if stage == "diagnostics":
        # Epilogue identifier assertions — keep as an extension stage rather
        # than aliasing back to setup (which would fabricate a final setup
        # boundary in the completed-stage timeline).
        return "diagnostics"
    if stage == "integrity":
        # Generic sabotage-probe stage; the formatter detail carries the
        # canonical boundary, default to commit (revision/stream/duplicate).
        return "commit"
    return stage


class Scenario:
    """Owns one disposable Phase 0 run: stage-tagged checks plus identifiers.

    Wired to the #374 reusable diagnostics collector: every ``note()``
    mirrors a stage boundary and every ``check()`` failure records the first
    failure plus a best-effort JSON artifact to ``E2E_DIAGNOSTICS_DIR``. The
    collector never masks the original failure and never touches gameplay
    state — it only observes in-memory IDs and timelines.
    """

    def __init__(self, client: TestClient, factory, owner_id: uuid.UUID):
        self.client = client
        self.factory = factory
        self.owner_id = owner_id
        self.campaign_id: str | None = None
        self.stage = "init"
        self.last_revision = -1
        self.ids: dict = {
            "campaign_id": None,
            "thread_id": None,
            "character_id": None,
            "submission_ids": [],
            "turn_ids": [],
            "attempt_ids": [],
            "stream_ids": [],
            "revisions": {},
        }
        self.diag = ScenarioDiagnostics(scenario="phase0")
        self._open_stage: str | None = None

    def note(self, stage: str) -> None:
        self.stage = stage
        logger.info("phase0-372 stage=%s campaign_id=%s", stage, self.campaign_id)
        try:
            canonical = _canonical_stage(stage)
            if self._open_stage is not None and self._open_stage != canonical:
                self.diag.end_stage(self._open_stage)
                self._open_stage = None
            if self._open_stage is None:
                self.diag.begin_stage(canonical)
                self._open_stage = canonical
            self._sync_ids()
        except Exception:
            pass

    def _sync_ids(self) -> None:
        try:
            flat: dict = {}
            for key, value in self.ids.items():
                if key == "revisions":
                    continue
                flat[key] = list(value) if isinstance(value, list) else value
            if isinstance(self.ids.get("revisions"), dict):
                flat["revisions"] = dict(self.ids["revisions"])
            if self.campaign_id is not None:
                flat["campaign_id"] = self.campaign_id
            self.diag.record_ids(**flat)
        except Exception:
            pass

    def _record_failure(
        self,
        stage: str,
        message: str,
        *,
        category: str | None = None,
        detail: dict | None = None,
    ) -> None:
        try:
            self._sync_ids()
            canonical = _canonical_stage(stage, category)
            self.diag.fail(canonical, message, category=category, detail=detail)
            self.diag.save_artifact()
        except Exception:
            pass

    def check(
        self,
        condition: bool,
        stage: str,
        message: str,
        *,
        category: str | None = None,
        detail: dict | None = None,
    ):
        if not condition:
            self._record_failure(stage, message, category=category, detail=detail)
            canonical = _canonical_stage(stage, category)
            try:
                line = format_failure_line(
                    {
                        "stage": canonical,
                        "category": category or "assertion",
                        "message": message,
                        "ids": dict(self.ids),
                    }
                )
            except Exception:
                line = f"[374:{canonical}] {message}"
            raise AssertionError(f"[372:{stage}] {line} | ids={self.ids}")
        return True

    def sweep_failure_detail(self, outcome: dict, stage: str | None = None) -> dict:
        """Best-effort canonical detail for a failed execute sweep.

        Provider-boundary failures (missing/misbehaving generative fixture)
        route through the generative-execution formatter so the artifact
        names ``generative_execution``; all other sweep failures stay
        ``submission_execution``. Only whitelisted IDs/role/step cross into
        the artifact — the raw provider error (which echoes player input
        excerpts) is never persisted.
        """
        try:
            failed = outcome.get("failed") or []
            first = dict(failed[0]) if failed else {}
            first_error = str(first.get("error", ""))
            attempt_id = first.get("attempt_id")
            lowered = first_error.lower()
            if "no fixture for this logical request" in lowered or (
                "fake-provider" in lowered and "no fixture" in lowered
            ):
                role = "forward_dm"
                try:
                    import re

                    match = re.search(r"role\s*=\s*['\"]?([\w-]+)", first_error)
                    if match:
                        role = match.group(1)
                except Exception:
                    pass
                safe_error = RuntimeError(
                    "generative provider has no fixture for this logical request "
                    f"(role={role} step={stage})"
                )
                detail = format_provider_failure(
                    safe_error,
                    role=role,
                    step=stage,
                )
                try:
                    if attempt_id is not None:
                        detail.setdefault("metadata", {})["attempt_id"] = str(
                            attempt_id
                        )
                except Exception:
                    pass
                return detail
            return format_sweep_failure(outcome)
        except Exception:
            return {
                "category": "submission_execution",
                "stage": STAGE_SUBMISSION,
                "detail": "sweep failed",
                "metadata": {},
            }

    def check_sweep(self, outcome: dict, stage: str):
        """Assert a production execute sweep has no failures (#374 detail)."""
        if outcome.get("failed"):
            try:
                # Record failed attempt IDs before the failure report so the
                # artifact IDs include the boundary that actually broke.
                for entry in outcome.get("failed") or []:
                    aid = (entry or {}).get("attempt_id")
                    if aid is not None and str(aid) not in self.ids["attempt_ids"]:
                        self.ids["attempt_ids"].append(str(aid))
            except Exception:
                pass
            detail = self.sweep_failure_detail(outcome, stage)
            try:
                failed = outcome.get("failed") or []
                attempt_ids = [
                    str((entry or {}).get("attempt_id"))
                    for entry in failed
                    if (entry or {}).get("attempt_id") is not None
                ]
            except Exception:
                attempt_ids = []
            # Privacy-safe summary only: never persist the raw sweep
            # error/outcome (provider errors echo player input excerpts).
            message = (
                f"sweep failed: {len(attempt_ids)} failed "
                f"(attempts={attempt_ids} stage={stage})"
            )
            return self.check(
                False,
                stage,
                message,
                category=detail.get("category", "submission_execution"),
                detail=detail,
            )
        return self.check(True, stage, "sweep succeeded")

    def mark_completed(self, canonical: str) -> None:
        """Record a completed logical boundary without moving the raw stage.

        The raw harness label (``play-1``…) stays in ``self.stage`` for
        failure attribution; the canonical pipeline boundary (generative
        execution, commit) is recorded alongside so the timeline shows
        completed logical stages rather than jumping submission→reconnect.
        """
        try:
            self.diag.begin_stage(canonical)
            self.diag.end_stage(canonical)
        except Exception:
            pass

    def record_external_failure(self, exc: BaseException) -> None:
        """Record a failure that bypassed ``check()`` (teardown fallback).

        Best-effort and never masks the original exception: mirrors current
        IDs, marks the current canonical stage failed, and saves the
        artifact. Only the error class (never the raw exception text, which
        can echo player input) crosses into the shared artifact. No
        gameplay state is touched.
        """
        try:
            self._sync_ids()
            if self.diag.first_failure is not None:
                self.diag.save_artifact()
                return
            canonical = _canonical_stage(self.stage)
            error_class = type(exc).__name__
            self.diag.fail(
                canonical,
                f"unexpected failure ({error_class})",
                category="assertion",
                detail={"error_class": error_class},
            )
            self.diag.save_artifact()
        except Exception:
            pass

    def diagnostics(self) -> dict:
        return dict(self.ids)


def _resolve_test_profile(request, db):
    return db.get(Profile, TEST_USER_ID)


@pytest.fixture
def scn(monkeypatch, request):
    """Clean disposable database + HTTP client on production routers."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner_id = TEST_USER_ID
    with factory() as db:
        db.add(Profile(id=owner_id, email="phase0-solo-372@example.com"))
        db.commit()

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setenv("NODE_ENV", "test")
    for module in (
        "app.campaigns.router",
        "app.runtime.router",
        "app.snapshot.router",
        "app.dm.router",
        "app.characters.router",
    ):
        monkeypatch.setattr(
            f"{module}.resolve_profile",
            _resolve_test_profile,
            raising=False,
        )
    app.dependency_overrides[get_db] = override_db
    scenario = Scenario(TestClient(app), factory, owner_id)
    try:
        yield scenario
    except BaseException as exc:
        # Failures that bypass Scenario.check() (raw asserts, JSON/DB
        # exceptions) are thrown into the fixture at yield: record the
        # current canonical stage + artifact without masking the original.
        scenario.record_external_failure(exc)
        raise
    finally:
        try:
            # Capture the artifact after failure metadata is recorded; a
            # missing/empty diagnostics dir must never mask the test result.
            if scenario.diag.first_failure is not None:
                scenario.diag.save_artifact()
            elif scenario._open_stage is not None:
                scenario._sync_ids()
                scenario.diag.end_stage(scenario._open_stage)
                scenario._open_stage = None
        except Exception:
            pass
        app.dependency_overrides.clear()
        engine.dispose()


# ── production-boundary steps (the harness; #245/#246 replace one seam) ──────


def make_synthetic_character(scn: Scenario, name: str = "Phase0 Hero") -> str:
    """Disposable synthetic PC fixture (profile/user already synthetic)."""
    with scn.factory() as db:
        char = Character(owner_id=scn.owner_id, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(
            Dnd5eCharacterSheet(
                character_id=char.id,
                owner_id=scn.owner_id,
                character_name=name,
                race="Human",
                char_class="Fighter",
                level=1,
            )
        )
        db.commit()
        char_id = str(char.id)
    scn.ids["character_id"] = char_id
    return char_id


def setup_solo_campaign(scn: Scenario, char_id: str) -> str:
    """Create campaign + select character + ready through production endpoints."""
    scn.note("setup")
    r = scn.client.post(
        "/api/campaigns", json={"name": "Phase0 Solo 372", "required_players": 1}
    )
    scn.check(r.status_code == 200, "setup", f"campaign creation failed (status={r.status_code})")
    campaign_id = r.json()["campaign"]["id"]
    scn.campaign_id = campaign_id
    scn.ids["campaign_id"] = campaign_id

    r = scn.client.put(
        f"/api/campaigns/{campaign_id}/members/me/character",
        json={"expected_revision": 0, "character_id": char_id},
        headers={"Idempotency-Key": "phase0-select"},
    )
    scn.check(r.status_code == 200, "setup", f"character select failed (status={r.status_code})")

    r = scn.client.put(
        f"/api/campaigns/{campaign_id}/members/me/readiness",
        json={"expected_revision": 1, "ready": True},
        headers={"Idempotency-Key": "phase0-ready"},
    )
    scn.check(r.status_code == 200, "setup", f"readiness failed (status={r.status_code})")

    lobby = scn.client.get(f"/api/campaigns/{campaign_id}/lobby")
    scn.check(lobby.status_code == 200, "setup", f"lobby read failed (status={lobby.status_code})")
    scn.check(
        lobby.json()["eligibility"]["eligible"] is True,
        "setup",
        "solo lobby not eligible after select+ready",
    )
    return campaign_id


def start_production_play(scn: Scenario, *, operation_key: str) -> dict:
    """Production-start seam — #245 world seed (live-table opening is #246).

    The #355 temporary bootstrap is deleted. Seeding stages durable canon
    and moves the campaign to ``starting``; opening turns and continued play
    resume here once #246 opens the live table.
    """
    scn.note("start")
    assert scn.campaign_id is not None
    r = scn.client.post(
        f"/api/campaigns/{scn.campaign_id}/world-seed",
        json={"operation_id": operation_key},
        headers={"Idempotency-Key": operation_key},
    )
    scn.check(r.status_code == 200, "start", f"world seed failed (status={r.status_code})")
    body = r.json()
    scn.check(body["campaign"]["status"] == "starting", "start", "campaign not starting")
    scn.check((body.get("seed") or {}).get("contract_version") == 1, "start", "seed contract missing")
    scn.check((body.get("seed") or {}).get("scene_present") is True, "start", "no starting scene")
    scn.check((body.get("seed") or {}).get("clock_count", 0) >= 1, "start", "no pressure clock")
    scn.check("solo-bootstrap" not in r.text, "start", "temporary scaffold state leaked")
    return body


def submit_player_turn(scn: Scenario, text: str, key: str, client=None) -> dict:
    """One normal freeform submission through the production submissions API."""
    assert scn.campaign_id is not None
    http = client or scn.client
    r = http.post(
        f"/api/campaigns/{scn.campaign_id}/submissions",
        json={"content": text},
        headers={"Idempotency-Key": key},
    )
    scn.check(r.status_code == 201, "play", f"submission {key} failed (status={r.status_code})")
    body = r.json()
    scn.check((body.get("dm_turn") or {}).get("id"), "play", "no DM turn coordinated")
    scn.ids["submission_ids"].append(body["submission"]["id"])
    # Record the coordinated turn ID at creation so failure artifacts carry
    # it even when the commit assertions below are never reached.
    try:
        turn_id = (body.get("dm_turn") or {}).get("id")
        if turn_id is not None and str(turn_id) not in scn.ids["turn_ids"]:
            scn.ids["turn_ids"].append(str(turn_id))
    except Exception:
        pass
    return body


def drain_dm_execution(scn: Scenario, stage: str, adjudicate=None) -> dict:
    """Run the production execute sweep (cron path) in worker-like sessions.

    ``adjudicate=None`` runs the full production adjudication path
    (``adjudicate_with_failover`` through the installed #373 fake
    provider); an explicit callable overrides only the model seam (used
    by the injected-failure proof).
    """
    try:
        scn.note(stage)
    except Exception:
        pass
    assert scn.campaign_id is not None
    cid = uuid.UUID(scn.campaign_id)
    outcome: dict = {"executed": [], "failed": [], "skipped": []}
    for _ in range(6):
        with scn.factory() as db:
            outcome = run_dm_execute_sweep(
                db, limit=10, adjudicate=adjudicate, narrator="deterministic"
            )
            db.commit()
        if outcome.get("failed"):
            return outcome
        with scn.factory() as db:
            pending = [
                t
                for t in list_turns(db, cid, limit=200)
                if t.status in ("pending", "streaming", "awaiting_roll")
            ]
            prepared = (
                db.execute(
                    select(DmTurnAttempt).where(
                        DmTurnAttempt.campaign_id == cid,
                        DmTurnAttempt.status == "prepared",
                    )
                )
                .scalars()
                .all()
            )
        if not pending and not prepared:
            break
    # The sweep ran the generative-execution boundary to completion.
    scn.mark_completed(STAGE_GENERATIVE_EXECUTION)
    return outcome


def await_committed_reply(
    scn: Scenario,
    stage: str,
    turn_id: str,
    *,
    expected_reply_marker: str | None = "phase0-reply-",
    allow_silent: bool = False,
) -> str | None:
    """Assert one logical turn has exactly one committed, durable DM result."""
    try:
        scn.note(stage)
    except Exception:
        pass
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(turn_id))
        scn.check(turn is not None, stage, f"turn {turn_id} missing")
        assert turn is not None
        attempt = (
            db.get(DmTurnAttempt, turn.current_attempt_id)
            if turn.current_attempt_id
            else None
        )
        scn.check(
            turn.status == "succeeded",
            stage,
            f"turn {turn_id} not succeeded (status={turn.status})",
        )
        scn.check(attempt is not None, stage, f"turn {turn_id} has no attempt")
        assert attempt is not None
        scn.check(
            attempt.status == "succeeded",
            stage,
            f"attempt {attempt.id} not succeeded (status={attempt.status})",
        )
        if attempt.stream_id is None and allow_silent:
            contract = attempt.contract_snapshot or {}
            scn.check(
                contract.get("mode") == "silent",
                stage,
                f"attempt {attempt.id} has no stream but is not a silent result",
                category="stream_persistence",
                detail=format_commit_failure(
                    turn_id=str(turn.id),
                    attempt_id=str(attempt.id),
                    stream_id=None,
                    detail="streamless result was not an authorized silent contract",
                ),
            )
            if str(turn.id) not in scn.ids["turn_ids"]:
                scn.ids["turn_ids"].append(str(turn.id))
            if str(attempt.id) not in scn.ids["attempt_ids"]:
                scn.ids["attempt_ids"].append(str(attempt.id))
            scn.mark_completed(STAGE_COMMIT)
            return None
        scn.check(
            attempt.stream_id is not None,
            stage,
            f"attempt {attempt.id} has no persisted stream",
            category="stream_persistence",
            detail=format_commit_failure(
                turn_id=str(turn.id),
                attempt_id=str(attempt.id),
                stream_id=None,
                detail=f"attempt {attempt.id} has no persisted stream",
            ),
        )
        assert attempt.stream_id is not None
        chunks = (
            db.execute(
                select(DMStreamChunk)
                .where(DMStreamChunk.stream_id == attempt.stream_id)
                .order_by(DMStreamChunk.sequence)
            )
            .scalars()
            .all()
        )
        scn.check(
            len(chunks) >= 1,
            stage,
            "DM reply has no durable stream chunks",
            category="stream_persistence",
            detail=format_commit_failure(
                turn_id=str(turn.id),
                attempt_id=str(attempt.id),
                stream_id=str(attempt.stream_id),
                detail="DM reply has no durable stream chunks",
            ),
        )
        text = reconstruct_text(db, attempt.stream_id)
        if expected_reply_marker is not None:
            scn.check(
                expected_reply_marker in text,
                stage,
                "durable narration lacks the committed-reply marker",
                category="stream_persistence",
                detail=format_commit_failure(
                    turn_id=str(turn.id),
                    attempt_id=str(attempt.id),
                    stream_id=str(attempt.stream_id),
                    detail="durable narration lacks the committed-reply marker",
                ),
            )
        else:
            # Real model prose is deliberately nondeterministic. The durable
            # non-empty stream assertion above remains the invariant.
            scn.check(bool(text.strip()), stage, "durable narration is empty")
        if str(turn.id) not in scn.ids["turn_ids"]:
            scn.ids["turn_ids"].append(str(turn.id))
        if str(attempt.id) not in scn.ids["attempt_ids"]:
            scn.ids["attempt_ids"].append(str(attempt.id))
        if str(attempt.stream_id) not in scn.ids["stream_ids"]:
            scn.ids["stream_ids"].append(str(attempt.stream_id))
        # Durable commit boundary verified.
        scn.mark_completed(STAGE_COMMIT)
        return text


def assert_ordering_invariants(scn: Scenario, stage: str, client=None) -> int:
    """Campaign revision == domain-event sequence invariant (#188)."""
    try:
        scn.note(stage)
    except Exception:
        pass
    assert scn.campaign_id is not None
    http = client or scn.client
    r = http.get(f"/api/campaigns/{scn.campaign_id}/events")
    scn.check(r.status_code == 200, stage, f"events read failed (status={r.status_code})")
    body = r.json()
    seqs = [e["sequence"] for e in body["events"]]

    def _revision_detail(message: str) -> dict:
        try:
            return format_revision_mismatch(
                expected=list(range(1, len(seqs) + 1)),
                actual=list(seqs),
                revision=int(body.get("revision", -1)),
            )
        except Exception:
            return {
                "category": "revision_ordering",
                "stage": "commit",
                "detail": message,
                "metadata": {},
            }

    scn.check(
        seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
        stage,
        f"event sequences not strictly increasing: {seqs}",
        category="revision_ordering",
        detail=_revision_detail(f"event sequences not strictly increasing: {seqs}"),
    )
    scn.check(
        seqs == list(range(1, len(seqs) + 1)),
        stage,
        f"event sequences not contiguous from 1: {seqs}",
        category="revision_ordering",
        detail=_revision_detail(f"event sequences not contiguous from 1: {seqs}"),
    )
    scn.check(
        body["revision"] == len(seqs),
        stage,
        f"revision {body['revision']} != event count {len(seqs)} "
        "(revision==sequence invariant broken)",
        category="revision_ordering",
        detail=_revision_detail("revision != event count"),
    )
    scn.check(
        body["revision"] >= scn.last_revision,
        stage,
        f"revision went backwards: {body['revision']} < {scn.last_revision}",
    )
    scn.last_revision = body["revision"]
    scn.ids["revisions"][stage] = body["revision"]
    return body["revision"]


def assert_single_result_per_submission(
    scn: Scenario, stage: str, expected_turns: int, client=None
) -> list:
    """One accepted logical player intent -> one committed gameplay result."""
    try:
        scn.note(stage)
    except Exception:
        pass
    assert scn.campaign_id is not None
    http = client or scn.client
    r = http.get(f"/api/campaigns/{scn.campaign_id}/dm-turns")
    scn.check(r.status_code == 200, stage, f"dm-turns read failed (status={r.status_code})")
    turns = r.json()["turns"]
    scn.check(
        len(turns) == expected_turns,
        stage,
        f"expected {expected_turns} turns, found {len(turns)}",
    )
    turn_ids = [t["id"] for t in turns]
    scn.check(len(set(turn_ids)) == len(turn_ids), stage, "duplicate turn ids listed")
    consumed = [s for t in turns for s in (t["submission_ids"] or [])]
    duplicate_subs = sorted({s for s in consumed if consumed.count(s) > 1})
    scn.check(
        len(consumed) == len(set(consumed)),
        stage,
        "one submission consumed by multiple turns (duplicate gameplay)",
        category="duplicate_commit" if duplicate_subs else None,
        detail=format_duplicate_commit(
            submission_id=duplicate_subs[0] if duplicate_subs else "unknown",
            turn_ids=turn_ids,
        )
        if duplicate_subs
        else None,
    )
    subs = http.get(f"/api/campaigns/{scn.campaign_id}/submissions")
    scn.check(subs.status_code == 200, stage, f"submissions read failed (status={subs.status_code})")
    for sub in subs.json()["submissions"]:
        count = consumed.count(sub["id"])
        scn.check(
            count == 1,
            stage,
            f"submission {sub['id']} committed {count} times",
            category="duplicate_commit" if count != 1 else None,
            detail=format_duplicate_commit(
                submission_id=sub["id"],
                turn_ids=[t["id"] for t in turns if sub["id"] in (t["submission_ids"] or [])],
            )
            if count != 1
            else None,
        )
    return turns


def read_snapshot(scn: Scenario, stage: str, client=None) -> dict:
    try:
        scn.note(stage)
    except Exception:
        pass
    assert scn.campaign_id is not None
    http = client or scn.client
    r = http.get(f"/api/campaigns/{scn.campaign_id}/snapshot")
    scn.check(r.status_code == 200, stage, f"snapshot read failed (status={r.status_code})")
    return r.json()


def assert_same_authoritative_projection(
    scn: Scenario, stage: str, before: dict, after: dict
) -> None:
    try:
        scn.note(stage)
    except Exception:
        pass
    try:
        snapshot_detail = format_snapshot_mismatch(
            before, after, keys=SNAPSHOT_COMPARE_KEYS
        )
    except Exception:
        snapshot_detail = None
    for key in SNAPSHOT_COMPARE_KEYS:
        scn.check(
            before[key] == after[key],
            stage,
            f"reconnect divergence in snapshot[{key}]",
            category="reconnect_reconstruction",
            detail=snapshot_detail,
        )


def assert_player_turns_preserved(
    scn: Scenario, stage: str, expected_texts: list, contents: list
) -> None:
    """Every submitted player turn must survive reconnect.

    Privacy-safe by construction: failure messages name the turn index,
    never the raw player content (which would otherwise land verbatim in
    the shared #374 artifact and CI log).
    """
    for index, text in enumerate(expected_texts):
        scn.check(
            text in contents,
            stage,
            f"player turn lost across reconnect (index={index} of {len(expected_texts)})",
        )


# ── main scenario ─────────────────────────────────────────────────────────────


def run_phase0_solo_scenario(
    scn: Scenario,
    *,
    expected_reply_marker: str | None = "phase0-reply-",
    provider_calls=None,
    allow_silent: bool = False,
) -> None:
    """Run the one Phase 0 scenario flow for fake and opt-in real AI.

    Deterministic and generative modes require narration for every turn.
    Experimental decision mode may additionally accept a succeeded, persisted
    ``silent`` contract without inventing a stream; all other structural,
    reconnect, and continuation assertions remain shared.
    """
    # Setup: synthetic fixtures + authoritative select/ready.
    char_id = make_synthetic_character(scn)
    setup_solo_campaign(scn, char_id)

    # Start through the production #245 world-seed seam.
    opening = start_production_play(scn, operation_key="phase0-start-372")
    assert_ordering_invariants(scn, "start")
    # Live-table opening and continued play resume here once #246 lands
    # (which restores dm_turn/dm_attempt to the start response).
    pytest.skip("continued live-table play requires #246 (campaign seeds to starting)")
    opening_turn_id = (opening.get("dm_turn") or {}).get("id")

    # Opening DM turn completes through the production execution path.
    scn.note("opening")
    outcome = drain_dm_execution(scn, "opening")
    scn.check_sweep(outcome, "opening")
    opening_text = await_committed_reply(
        scn,
        "opening",
        opening_turn_id,
        expected_reply_marker=expected_reply_marker,
        allow_silent=allow_silent,
    )
    if not allow_silent:
        assert opening_text
    assert_single_result_per_submission(scn, "opening", 1)

    # Three freeform player turns, each with its committed DM reply.
    stream_texts = []
    for index, text in enumerate(FREEFORM_TURNS):
        stage = f"play-{index + 1}"
        scn.note(stage)
        submitted = submit_player_turn(scn, text, f"phase0-turn-{index + 1}")
        turn_id = submitted["dm_turn"]["id"]
        outcome = drain_dm_execution(scn, stage)
        scn.check_sweep(outcome, stage)
        reply_text = await_committed_reply(
            scn,
            stage,
            turn_id,
            expected_reply_marker=expected_reply_marker,
            allow_silent=allow_silent,
        )
        if reply_text is not None:
            stream_texts.append(reply_text)
        assert_ordering_invariants(scn, stage)
        assert_single_result_per_submission(scn, stage, 2 + index)
    scn.check(
        len(set(stream_texts)) == len(stream_texts),
        "play-3",
        "DM replies are not distinct per player turn",
    )

    # Duplicate delivery of an accepted submission must not duplicate gameplay.
    scn.note("duplicate-guard")
    replay = scn.client.post(
        f"/api/campaigns/{scn.campaign_id}/submissions",
        json={"content": FREEFORM_TURNS[0]},
        headers={"Idempotency-Key": "phase0-turn-1"},
    )
    scn.check(
        replay.status_code == 201, "duplicate-guard", f"replay failed (status={replay.status_code})"
    )
    scn.check(
        replay.headers.get("X-Idempotent-Replay") == "true",
        "duplicate-guard",
        "duplicate submission was not recognized as a replay",
    )
    assert_single_result_per_submission(scn, "duplicate-guard", 4)

    # Refresh/reconnect: a brand-new client + fresh sessions must reconstruct
    # the same authoritative transcript/state with no duplicate visible turns.
    scn.note("reconnect")
    before = read_snapshot(scn, "reconnect")
    reconnected_client = TestClient(app)
    after = read_snapshot(scn, "reconnect", client=reconnected_client)
    assert_same_authoritative_projection(scn, "reconnect", before, after)
    # The reconnected snapshot must expose every committed DM reply exactly
    # once — history only projects player submissions, DM narration lives in
    # dm_messages keyed by stream id.
    dm_messages = after.get("dm_messages") or []
    expected_visible_replies = len(scn.ids["stream_ids"])
    scn.check(
        len(dm_messages) == expected_visible_replies,
        "reconnect",
        f"expected {expected_visible_replies} committed DM replies, "
        f"found {len(dm_messages)}",
    )
    scn.check(
        len({m["id"] for m in dm_messages}) == expected_visible_replies,
        "reconnect",
        "duplicate DM messages after reconnect",
    )
    for stream_id in scn.ids["stream_ids"]:
        scn.check(
            any(m["id"] == stream_id for m in dm_messages),
            "reconnect",
            f"committed DM stream {stream_id} missing from reconnect snapshot",
        )
    contents = [m["raw_content"] for m in after["history"]["messages"]]
    assert_player_turns_preserved(scn, "reconnect", FREEFORM_TURNS, contents)
    with scn.factory() as db:
        for stream_id in scn.ids["stream_ids"]:
            scn.check(
                bool(reconstruct_text(db, uuid.UUID(stream_id)).strip()),
                "reconnect",
                f"stream {stream_id} not reconstructable after reconnect",
            )

    # Play continues after reconnect through the same production path, driven
    # by the reconnected client so the test proves a fresh session can play.
    scn.note("post-reconnect")
    submitted = submit_player_turn(
        scn, POST_RECONNECT_TURN, "phase0-turn-post", client=reconnected_client
    )
    outcome = drain_dm_execution(scn, "post-reconnect")
    scn.check_sweep(outcome, "post-reconnect")
    await_committed_reply(
        scn,
        "post-reconnect",
        submitted["dm_turn"]["id"],
        expected_reply_marker=expected_reply_marker,
        allow_silent=allow_silent,
    )
    assert_ordering_invariants(scn, "post-reconnect", client=reconnected_client)
    assert_single_result_per_submission(
        scn, "post-reconnect", 5, client=reconnected_client
    )
    grown = read_snapshot(scn, "post-reconnect", client=reconnected_client)
    scn.check(
        POST_RECONNECT_TURN in [m["raw_content"] for m in grown["history"]["messages"]],
        "post-reconnect",
        "post-reconnect turn missing from live-table snapshot",
    )

    # Stable identifiers for later diagnostics (#374) — asserted present, never hidden.
    scn.note("diagnostics")
    diag = scn.diagnostics()
    scn.check(diag["campaign_id"], "diagnostics", "campaign id not exposed")
    scn.check(diag["thread_id"], "diagnostics", "thread id not exposed")
    scn.check(diag["character_id"], "diagnostics", "character id not exposed")
    scn.check(
        len(diag["submission_ids"]) == 4, "diagnostics", "submission ids incomplete"
    )
    scn.check(len(diag["turn_ids"]) == 5, "diagnostics", "turn ids incomplete")
    scn.check(len(diag["attempt_ids"]) == 5, "diagnostics", "attempt ids incomplete")
    if allow_silent:
        scn.check(
            0 < len(diag["stream_ids"]) <= len(diag["attempt_ids"]),
            "diagnostics",
            "visible stream identifiers inconsistent with silent results",
        )
    else:
        scn.check(
            len(diag["stream_ids"]) == len(diag["attempt_ids"]),
            "diagnostics",
            "stream ids incomplete",
        )
    if provider_calls is not None:
        # #373 observability: every AI role call was satisfied by a named fixture.
        satisfied = {call["fixture_step"] for call in provider_calls}
        scn.check(
            satisfied == {"opening", "play-1", "play-2", "play-3", "post-reconnect"},
            "diagnostics",
            f"fake-provider call attribution incomplete: {sorted(satisfied)}",
        )
        scn.check(
            all(call["role"] == "forward_dm" for call in provider_calls),
            "diagnostics",
            "unexpected AI role served by the fake provider",
        )
    logger.info("phase0-372 diagnostics=%s", diag)


def test_phase0_solo_dogfood_through_reconnect_and_continued_play(scn, phase0_provider):
    run_phase0_solo_scenario(scn, provider_calls=phase0_provider.calls)


# ── intentional-break proofs: each sabotage must fail at its assertion ────────


def _run_to_opening_reply(scn: Scenario):
    """Shared prefix for break proofs: setup -> start -> committed opening."""
    char_id = make_synthetic_character(scn)
    setup_solo_campaign(scn, char_id)
    opening = start_production_play(scn, operation_key="phase0-break-start")
    # Live-table opening turns resume here once #246 lands.
    pytest.skip("opening-turn break proofs require #246 (campaign seeds to starting)")
    outcome = drain_dm_execution(scn, "opening")
    assert not outcome.get("failed"), outcome
    await_committed_reply(scn, "opening", opening["dm_turn"]["id"])
    return opening


def test_break_failing_execution_surfaces_at_dm_reply_assertion(scn, phase0_provider):
    opening = _run_to_opening_reply(scn)
    scn.note("play")

    def _boom(packet, feedback=None):
        raise RuntimeError("injected provider failure")

    submitted = submit_player_turn(scn, "I press on.", "phase0-break-exec")
    outcome = drain_dm_execution(scn, "play", adjudicate=_boom)
    scn.check(
        bool(outcome.get("failed")), "play", "sabotaged sweep unexpectedly succeeded"
    )
    with pytest.raises(AssertionError, match=r"\[372:play\]"):
        await_committed_reply(scn, "play", submitted["dm_turn"]["id"])
    # The failed turn stays observable, never silently committed.
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(submitted["dm_turn"]["id"]))
        assert turn is not None and turn.status != "succeeded"
    assert opening["dm_turn"]["id"] != submitted["dm_turn"]["id"]


def test_break_missing_stream_chunks_fail_durability_assertion(scn, phase0_provider):
    opening = _run_to_opening_reply(scn)
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(opening["dm_turn"]["id"]))
        assert turn is not None
        stream_id = db.get(DmTurnAttempt, turn.current_attempt_id).stream_id
        assert stream_id is not None
        db.execute(
            DMStreamChunk.__table__.delete().where(DMStreamChunk.stream_id == stream_id)
        )
        db.commit()
    with pytest.raises(AssertionError, match=r"\[372:opening\]"):
        await_committed_reply(scn, "opening", opening["dm_turn"]["id"])


def test_break_duplicate_turn_for_one_submission_is_detected(scn, phase0_provider):
    opening = _run_to_opening_reply(scn)
    with scn.factory() as db:
        original = db.get(DmTurn, uuid.UUID(opening["dm_turn"]["id"]))
        assert original is not None
        db.add(
            DmTurn(
                id=uuid.uuid4(),
                campaign_id=original.campaign_id,
                thread_id=original.thread_id,
                audience=original.audience,
                status="succeeded",
                source_revision=original.source_revision,
                input_set_revision=1,
                submission_ids=list(original.submission_ids or []),
            )
        )
        db.commit()
    with pytest.raises(AssertionError, match=r"\[372:integrity\]"):
        assert_single_result_per_submission(scn, "integrity", 2)


def test_break_extra_commit_breaks_reconnect_equality(scn, phase0_provider):
    _run_to_opening_reply(scn)
    scn.note("reconnect")
    before = read_snapshot(scn, "reconnect")
    submit_player_turn(scn, "An unexecuted extra line.", "phase0-break-extra")
    after = read_snapshot(scn, "reconnect")
    with pytest.raises(AssertionError, match=r"\[372:reconnect\]"):
        assert_same_authoritative_projection(scn, "reconnect", before, after)


def test_break_event_gap_breaks_ordering_invariant(scn, phase0_provider):
    _run_to_opening_reply(scn)
    from models.campaigns import CampaignDomainEvent

    with scn.factory() as db:
        events = (
            db.execute(
                select(CampaignDomainEvent)
                .where(CampaignDomainEvent.campaign_id == uuid.UUID(scn.campaign_id))
                .order_by(CampaignDomainEvent.sequence.asc())
            )
            .scalars()
            .all()
        )
        assert len(events) >= 3
        # Delete a middle event while keeping the revision: sequences are no
        # longer contiguous and revision != event count.
        db.delete(events[1])
        db.commit()
    # Tampering that breaks revision==sequence contiguity must fail here.
    with pytest.raises(AssertionError, match=r"\[372:integrity\]"):
        assert_ordering_invariants(scn, "integrity")


def test_break_missing_fixture_fails_at_provider_boundary(scn, phase0_provider):
    """#373: a removed/mismatched fixture fails loudly at the provider boundary.

    With the ``play-1`` fixture removed, the production sweep must fail
    with an actionable error naming the logical request — never a silent
    generic reply or downstream validation noise.
    """
    _run_to_opening_reply(scn)
    scn.note("play")
    phase0_provider._fixtures = [
        fixture for fixture in phase0_provider._fixtures if fixture.step != "play-1"
    ]
    submitted = submit_player_turn(scn, FREEFORM_TURNS[0], "phase0-turn-1")
    outcome = drain_dm_execution(scn, "play")
    scn.check(
        bool(outcome.get("failed")),
        "play",
        "sweep unexpectedly succeeded without a fixture",
    )
    error = outcome["failed"][0]["error"]
    scn.check(
        "fake-provider has no fixture" in error and "forward_dm" in error,
        "play",
        f"provider-boundary error is not actionable: {error!r}",
    )
    # The unmatched turn stays uncommitted, never silently narrated.
    with scn.factory() as db:
        turn = db.get(DmTurn, uuid.UUID(submitted["dm_turn"]["id"]))
        assert turn is not None and turn.status != "succeeded"
    assert not phase0_provider.calls_for_step("play-1")


def test_missing_fixture_sweep_reports_generative_execution_artifact(
    scn, phase0_provider, tmp_path, monkeypatch
):
    """Real-harness provider failure must artifact as generative_execution.

    #374 requires the generative-execution stage to be named when the
    provider boundary breaks; a generic ``submission`` report would send
    investigators to the wrong pipeline stage. The artifact must stay
    privacy-safe (no raw player input) while preserving the relevant
    turn/attempt IDs.
    """
    monkeypatch.setenv("E2E_DIAGNOSTICS_DIR", str(tmp_path))
    _run_to_opening_reply(scn)
    scn.note("play-1")
    phase0_provider._fixtures = [
        fixture for fixture in phase0_provider._fixtures if fixture.step != "play-1"
    ]
    sentinel = "SENTINEL-PRIVATE-INPUT-9f3d58a2-must-never-reach-artifacts"
    submitted = submit_player_turn(
        scn, f"I press on carrying {sentinel}.", "phase0-turn-1"
    )
    submitted_turn_id = submitted["dm_turn"]["id"]
    assert submitted_turn_id
    outcome = drain_dm_execution(scn, "play-1")
    assert outcome.get("failed"), "sweep unexpectedly succeeded without a fixture"
    failed_attempt_id = (outcome["failed"][0] or {}).get("attempt_id")
    assert failed_attempt_id

    with pytest.raises(AssertionError, match=r"\[374:generative_execution\]"):
        scn.check_sweep(outcome, "play-1")

    failure = scn.diag.first_failure
    assert failure is not None
    assert failure["stage"] == "generative_execution"
    assert failure["category"] == "generative_execution"
    assert failure["detail"]["metadata"]["decision_role"] == "forward_dm"
    assert failure["detail"]["metadata"]["fixture_step"] == "play-1"
    assert failure["detail"]["metadata"]["attempt_id"] == str(failed_attempt_id)
    assert submitted_turn_id in failure["ids"]["turn_ids"]
    assert str(failed_attempt_id) in failure["ids"]["attempt_ids"]
    assert scn.diag.timeline()[-1] == {
        "stage": "generative_execution",
        "status": "failed",
    }

    artifacts = sorted(tmp_path.glob("e2e-374-phase0-*.json"))
    assert artifacts, "provider sweep failure saved no artifact"
    import json as _json

    saved = _json.load(open(artifacts[-1]))
    assert saved["first_failure"]["stage"] == "generative_execution"
    assert saved["first_failure"]["category"] == "generative_execution"
    assert submitted_turn_id in saved["first_failure"]["ids"]["turn_ids"]
    assert str(failed_attempt_id) in saved["first_failure"]["ids"]["attempt_ids"]
    assert (
        str(failed_attempt_id)
        == saved["first_failure"]["detail"]["metadata"]["attempt_id"]
    )
    dumped = _json.dumps(saved)
    assert sentinel not in dumped, "private player input leaked into CI artifact"


def test_external_failure_bypassing_check_still_marks_stage_failed(
    scn, phase0_provider, tmp_path, monkeypatch
):
    """Exceptions outside Scenario.check() must still emit a stage artifact.

    The fixture records failures thrown into it at yield (raw asserts,
    JSON/DB exceptions); this exercises that fallback directly without
    failing the test itself. Raw exception text (which can echo player
    input) must never reach the shared artifact — only the error class.
    """
    monkeypatch.setenv("E2E_DIAGNOSTICS_DIR", str(tmp_path))
    _run_to_opening_reply(scn)
    scn.note("play-1")

    sentinel = "SENTINEL-EXTERNAL-PRIVATE-374-must-never-reach-artifacts"
    try:
        raise RuntimeError(f"simulated raw failure bypassing check() {sentinel}")
    except RuntimeError as exc:
        scn.record_external_failure(exc)

    failure = scn.diag.first_failure
    assert failure is not None
    assert failure["stage"] == "submission"
    assert failure["detail"] == {"error_class": "RuntimeError"}
    assert scn.diag.timeline()[-1] == {"stage": "submission", "status": "failed"}

    artifacts = sorted(tmp_path.glob("e2e-374-phase0-*.json"))
    assert artifacts, "external failure saved no artifact"
    import json as _json

    saved = _json.load(open(artifacts[-1]))
    assert saved["first_failure"]["stage"] == "submission"
    assert saved["first_failure"]["detail"] == {"error_class": "RuntimeError"}
    assert sentinel not in _json.dumps(saved)


def test_generic_sweep_failure_omits_raw_error_text(
    scn, phase0_provider, tmp_path, monkeypatch
):
    """Non-missing-fixture sweep failures must also stay privacy-safe."""
    monkeypatch.setenv("E2E_DIAGNOSTICS_DIR", str(tmp_path))
    _run_to_opening_reply(scn)
    scn.note("play-1")
    sentinel = "SENTINEL-GENERIC-SWEEP-PRIVATE-374-must-never-reach-artifacts"
    outcome = {
        "executed": [],
        "failed": [{"attempt_id": "a-sentinel-1", "error": sentinel}],
        "skipped": [],
    }
    with pytest.raises(AssertionError, match=r"\[374:submission\]"):
        scn.check_sweep(outcome, "play-1")
    failure = scn.diag.first_failure
    assert failure is not None
    assert failure["stage"] == "submission"
    assert failure["category"] == "submission_execution"
    assert "a-sentinel-1" in failure["ids"]["attempt_ids"]

    artifacts = sorted(tmp_path.glob("e2e-374-phase0-*.json"))
    assert artifacts, "generic sweep failure saved no artifact"
    import json as _json

    saved = _json.load(open(artifacts[-1]))
    assert sentinel not in _json.dumps(saved)


def test_phase0_timeline_records_generative_and_commit_boundaries(
    scn, phase0_provider
):
    """Real harness timeline must show completed logical stage boundaries.

    A successful opening prefix runs submission → generative execution →
    durable commit; the collector timeline must contain the completed
    generative/commit boundaries rather than jumping submission→reconnect,
    and the diagnostics epilogue must not fabricate a trailing setup stage.
    """
    _run_to_opening_reply(scn)
    completed = {
        entry["stage"] for entry in scn.diag.timeline() if entry["status"] == "completed"
    }
    assert STAGE_GENERATIVE_EXECUTION in completed
    assert STAGE_COMMIT in completed
    assert scn.diag.first_failure is None
    scn.note("diagnostics")
    assert _canonical_stage("diagnostics") == "diagnostics"


def test_ordinary_check_path_omits_player_content_from_artifact(
    scn, phase0_provider, tmp_path, monkeypatch
):
    """Ordinary Scenario.check() failures must not leak player content.

    The reconnect turn-preservation assertion previously embedded the raw
    player turn in its message; with a sentinel turn submitted then dropped
    from the compared contents, the saved artifact and log-facing report
    must name the turn index only.
    """
    monkeypatch.setenv("E2E_DIAGNOSTICS_DIR", str(tmp_path))
    _run_to_opening_reply(scn)
    scn.note("reconnect")
    sentinel = "SENTINEL-ORDINARY-CHECK-PRIVATE-374-must-never-reach-artifacts"
    with pytest.raises(AssertionError, match=r"\[374:refresh_reconnect\]"):
        assert_player_turns_preserved(scn, "reconnect", [sentinel], ["unrelated"])
    failure = scn.diag.first_failure
    assert failure is not None
    assert failure["stage"] == "refresh_reconnect"
    assert "index=0 of 1" in failure["message"]

    artifacts = sorted(tmp_path.glob("e2e-374-phase0-*.json"))
    assert artifacts, "ordinary check failure saved no artifact"
    import json as _json

    saved = _json.load(open(artifacts[-1]))
    assert sentinel not in _json.dumps(saved)
    assert sentinel not in format_failure_line(saved["first_failure"])

# Reliability fault-injection suite

This directory is the first focused slice of issue #270. It contains only
synthetic data and fake infrastructure. Fault injection is test-only and cannot
be enabled through an application route or production configuration.

## Scenarios and invariants

| Injected fault | Expected recovery | Invariants asserted |
| --- | --- | --- |
| API/domain process disappears after committing state and outbox work | A fresh relay session discovers the pending outbox row | Campaign revision, domain event, and outbox obligation all persist atomically |
| Relay disappears after queue publish but before outbox acknowledgement | The expired claim is recovered and the same stable outbox/job ID is republished | Outbox reaches `published`; two deliveries have one job ID; relay attempts remain inspectable |
| Queue redelivers the duplicate | Worker execution ledger returns its cached result | One handler call, one `turn.resolved` event, one gameplay state change, one worker attempt |
| Provider transport is transiently unavailable | Provider transport retries within policy and then succeeds | Retry hook fires once; no real network/model call is made |
| Provider returns a terminal failure | Provider transport stops immediately | No retry occurs; the terminal error remains visible to the caller |
| Fault DB URL points outside an allowlisted local disposable database | Suite aborts before connecting | Fault tooling cannot target a remote or production-named database |

Each scenario emits `FAULT_DIAGNOSTIC` JSON. Set `FAULT_ARTIFACT_PATH` to also
append the timeline to a JSONL artifact.

## Existing coverage deliberately not duplicated

- API response loss/idempotent replay: `tests/test_player_submissions.py` and
  `tests/test_idempotency.py`.
- Stale and concurrent campaign revision conflicts: `tests/test_campaign_revision.py`.
- Worker crash leases, fencing, dead-letter durability, and manual replay:
  `tests/test_worker_queue.py`.
- Telemetry failure isolation and non-billing recovery lineage:
  `tests/test_observability.py`.

Later slices can combine poison-work correction/replay and telemetry loss with
the cross-layer scenario instead of repeating those unit contracts.

## Local run

```bash
cd backend
FAULT_ARTIFACT_PATH=/tmp/fault-diagnostics.jsonl \
  python -m pytest -p no:xonsh -p no:cacheprovider tests/reliability -v
```

`FAULT_TEST_DATABASE_URL` is optional. If supplied, it is rejected unless it
targets `ci_test`, `test`, or `reliability_test` on a local host. The scheduled
workflow uses the migrated disposable `ci_test` Postgres service.

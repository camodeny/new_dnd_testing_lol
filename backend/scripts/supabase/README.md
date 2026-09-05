# DM execution scheduling

Use Supabase Cron (`pg_cron`) and `pg_net` to POST to the authenticated
`/api/cron/dm-execute` endpoint every minute. The endpoint claims at most one
prepared attempt per request. Vercel's existing daily outbox relay schedule is
unchanged; the unsupported five-minute Vercel DM schedule is removed.

After deploying #357, enable Cron and pg_net in the production Supabase project.
In Vault, create `dm_execute_base_url` with the public HTTPS backend origin and
`dm_execute_cron_secret` with the backend's `CRON_SECRET`. Run
`schedule_dm_execute.sql` in the SQL editor. Re-running updates the named job.
This is explicit environment setup, outside Alembic, so previews and disposable
CI databases never acquire production schedules or secrets.

Enable `DM_INLINE_EXECUTE=1` for immediate execution of the newly accepted
submission's own attempt. Direct player conversations do not trigger execution.
Otherwise the minute sweep provides the handoff. Provider credentials/model
must be configured on the backend, never in the cron job.

The cron job queues an asynchronous HTTP request; inspect `net._http_response`
as well as Cron history and the backend attempt status. A successful Cron run
alone does not prove that the HTTP request or DM turn succeeded. The SQL uses a
five-minute HTTP timeout. Supabase scheduling does not increase the backend
host's request-duration limit: long evidence/regeneration runs require a worker
runtime that can finish them. Point the schedule at a publicly reachable worker
backend if runs exceed the Vercel runtime limit. The private Tailscale URL is not
reachable from hosted Supabase.

Each PostgreSQL executor holds one additional connection and a transaction
advisory lock for its lifetime. Recovery tries the same lock before resetting an
expired attempt, so an active executor remains protected even beyond the
300-second recovery age. Locks release on transaction end or database disconnect;
transaction-mode poolers retain the connection for this transaction. Atomic
prepared-to-running updates also prevent duplicate execution from stale reads.

After setup, verify a normal player submission yields a persisted DM reply and
refresh reconstructs it. To remove the schedule, run
`SELECT cron.unschedule('dnd-dm-execute');`.

References: [Supabase Cron](https://supabase.com/docs/guides/cron/quickstart),
[scheduled HTTP calls and Vault](https://supabase.com/docs/guides/functions/schedule-functions).

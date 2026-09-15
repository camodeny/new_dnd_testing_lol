-- Run once in the production Supabase SQL editor after deploying the endpoint.
-- Prerequisite: enable pg_cron and pg_net in Supabase Integrations/Extensions.
-- Store adventure_closing_base_url (public HTTPS origin, no trailing slash) and
-- adventure_closing_cron_secret (matching the backend CRON_SECRET) in Supabase Vault.
-- Re-running replaces the named job instead of creating duplicates.
-- Explicit environment setup, outside Alembic, so previews and disposable
-- CI databases never acquire production schedules or secrets.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM vault.decrypted_secrets WHERE name = 'adventure_closing_base_url'
                 AND decrypted_secret LIKE 'https://%')
     OR NOT EXISTS (SELECT 1 FROM vault.decrypted_secrets WHERE name = 'adventure_closing_cron_secret'
                    AND length(decrypted_secret) > 0) THEN
    RAISE EXCEPTION 'Configure adventure_closing_base_url and adventure_closing_cron_secret in Vault first';
  END IF;
END $$;

SELECT cron.schedule(
  'dnd-adventure-closing', '*/5 * * * *',
  $job$
  SELECT net.http_post(
    url := (SELECT rtrim(decrypted_secret, '/') FROM vault.decrypted_secrets
            WHERE name = 'adventure_closing_base_url') || '/api/cron/adventure-closing',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets
                                    WHERE name = 'adventure_closing_cron_secret')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 120000
  );
  $job$
);

-- The sweep claims at most 5 pending adventures per request and executes each
-- through the idempotent worker fence; overlapping queue deliveries converge
-- on the same adventure rows via job_id (the adventure id). Closing work is
-- best-effort: failures retry but never invalidate committed completions.
-- Inspect HTTP results as well as cron history: scheduling success means the
-- request was queued, not that the closing follow-ups succeeded.
-- SELECT id, status_code, timed_out, error_msg, created
-- FROM net._http_response ORDER BY created DESC LIMIT 20;
-- SELECT jobid, status, return_message, start_time
-- FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;
-- Point the schedule at a publicly reachable backend: the private Tailscale
-- URL is not reachable from hosted Supabase. (Vercel Hobby only allows daily
-- crons, so the Vercel schedule is not used for this sweep.)
-- Rollback: SELECT cron.unschedule('dnd-adventure-closing');

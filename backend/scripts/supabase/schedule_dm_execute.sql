-- Run once in the production Supabase SQL editor after deploying the endpoint.
-- Prerequisite: enable pg_cron and pg_net in Supabase Integrations/Extensions.
-- Store dm_execute_base_url (public HTTPS origin, no trailing slash) and
-- dm_execute_cron_secret (matching the backend CRON_SECRET) in Supabase Vault.
-- Re-running replaces the named job instead of creating duplicates.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM vault.decrypted_secrets WHERE name = 'dm_execute_base_url'
                 AND decrypted_secret LIKE 'https://%')
     OR NOT EXISTS (SELECT 1 FROM vault.decrypted_secrets WHERE name = 'dm_execute_cron_secret'
                    AND length(decrypted_secret) > 0) THEN
    RAISE EXCEPTION 'Configure dm_execute_base_url and dm_execute_cron_secret in Vault first';
  END IF;
END $$;

SELECT cron.schedule(
  'dnd-dm-execute', '* * * * *',
  $job$
  SELECT net.http_post(
    url := (SELECT rtrim(decrypted_secret, '/') FROM vault.decrypted_secrets
            WHERE name = 'dm_execute_base_url') || '/api/cron/dm-execute',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets
                                    WHERE name = 'dm_execute_cron_secret')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 300000
  );
  $job$
);

-- Inspect HTTP results as well as cron history: scheduling success means the
-- request was queued, not that the DM turn succeeded.
-- SELECT id, status_code, timed_out, error_msg, created
-- FROM net._http_response ORDER BY created DESC LIMIT 20;
-- SELECT jobid, status, return_message, start_time
-- FROM cron.job_run_details ORDER BY start_time DESC LIMIT 20;
-- Rollback: SELECT cron.unschedule('dnd-dm-execute');

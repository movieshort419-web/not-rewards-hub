-- Optional safety/compatibility patch for an existing v4 schema.
-- Run only if the corresponding index/column is missing.

create unique index if not exists uq_one_active_mining_per_user
on public.mining_sessions(telegram_id)
where status = 'active';

-- The bot uses the service-side Supabase secret key, so client RLS remains enabled.
-- Do not expose the secret key to users or frontend code.

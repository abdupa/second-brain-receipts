-- Backend-only transport claims. No raw messages, file IDs or user identifiers.
BEGIN;
CREATE TABLE public.telegram_updates (
    update_id BIGINT PRIMARY KEY CHECK (update_id >= 0),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'handled', 'failed'))
);
ALTER TABLE public.telegram_updates ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.telegram_updates FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.telegram_updates FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.telegram_updates FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.telegram_updates TO service_role;
    END IF;
END $$;
COMMIT;

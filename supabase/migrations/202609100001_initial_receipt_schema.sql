-- Server-only, post-extraction data. No storage buckets or workflow RPCs yet.
BEGIN;

CREATE TABLE public.vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    normalized_name TEXT NOT NULL CHECK (normalized_name ~ '[^[:space:]]'),
    display_name TEXT NOT NULL CHECK (display_name ~ '[^[:space:]]'),
    category TEXT NOT NULL CHECK (category ~ '[^[:space:]]'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT vendors_normalized_name_key UNIQUE (normalized_name)
);

CREATE TABLE public.receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id BIGINT NOT NULL,
    vendor_name TEXT NOT NULL CHECK (vendor_name ~ '[^[:space:]]'),
    normalized_vendor_name TEXT NOT NULL CHECK (normalized_vendor_name ~ '[^[:space:]]'),
    receipt_date DATE NOT NULL,
    total_amount NUMERIC(12,2) NOT NULL CHECK (total_amount > 0 AND total_amount < 'Infinity'::numeric),
    vat_amount NUMERIC(12,2) CHECK (vat_amount >= 0 AND vat_amount <= total_amount),
    category TEXT NOT NULL CHECK (category ~ '[^[:space:]]'),
    confidence_score TEXT NOT NULL CHECK (confidence_score IN ('High', 'Medium', 'Low')),
    image_storage_key TEXT NOT NULL CHECK (image_storage_key ~ '[^[:space:]]'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT receipts_vendor_date_total_key UNIQUE
        (normalized_vendor_name, receipt_date, total_amount)
);
CREATE INDEX receipts_chat_created_idx ON public.receipts (telegram_chat_id, created_at DESC);

CREATE TABLE public.pending_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id BIGINT NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    vendor_name TEXT NOT NULL CHECK (vendor_name ~ '[^[:space:]]'),
    normalized_vendor_name TEXT NOT NULL CHECK (normalized_vendor_name ~ '[^[:space:]]'),
    receipt_date DATE NOT NULL,
    total_amount NUMERIC(12,2) NOT NULL CHECK (total_amount > 0 AND total_amount < 'Infinity'::numeric),
    vat_amount NUMERIC(12,2) CHECK (vat_amount >= 0 AND vat_amount <= total_amount),
    confidence_score TEXT NOT NULL CHECK (confidence_score IN ('High', 'Medium', 'Low')),
    image_storage_key TEXT NOT NULL CHECK (image_storage_key ~ '[^[:space:]]'),
    category TEXT CHECK (category ~ '[^[:space:]]'),
    state TEXT NOT NULL CHECK
        (state IN ('processing', 'awaiting_category', 'completed', 'failed', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at > created_at),
    CHECK (state <> 'completed' OR category IS NOT NULL)
);
CREATE UNIQUE INDEX pending_receipts_one_question_idx
    ON public.pending_receipts (telegram_chat_id, telegram_user_id)
    WHERE state = 'awaiting_category';
CREATE INDEX pending_receipts_active_lookup_idx
    ON public.pending_receipts (telegram_chat_id, telegram_user_id, state)
    WHERE state IN ('processing', 'awaiting_category');
CREATE INDEX pending_receipts_expiry_idx ON public.pending_receipts (expires_at)
    WHERE state = 'awaiting_category';

ALTER TABLE public.vendors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.vendors, public.receipts, public.pending_receipts FROM PUBLIC;
-- Supabase roles exist on Supabase; keep this migration usable on plain PostgreSQL.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON public.vendors, public.receipts, public.pending_receipts FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON public.vendors, public.receipts, public.pending_receipts FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON public.vendors, public.receipts, public.pending_receipts TO service_role;
    END IF;
END $$;
COMMIT;

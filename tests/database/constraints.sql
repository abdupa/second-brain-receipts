\set ON_ERROR_STOP on
BEGIN;
-- Test helpers live only in this rolled-back test transaction.
CREATE FUNCTION pg_temp.expect_error(statement TEXT, expected_state TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE statement;
    EXCEPTION WHEN OTHERS THEN
        IF SQLSTATE = expected_state THEN RETURN; END IF;
        RAISE;
    END;
    RAISE EXCEPTION 'Expected SQLSTATE % for %', expected_state, statement;
END $$;

INSERT INTO public.vendors (normalized_name, display_name, category)
VALUES ('abc', 'ABC', 'Custom category');
SELECT pg_temp.expect_error(
    $$INSERT INTO public.vendors (normalized_name, display_name, category)
      VALUES ('abc', 'ABC', 'Other')$$, '23505');
SELECT pg_temp.expect_error(
    $$INSERT INTO public.vendors (normalized_name, display_name, category)
      VALUES ('other', 'Other', E'\t\n')$$, '23514');

INSERT INTO public.receipts (telegram_chat_id, vendor_name, normalized_vendor_name,
    receipt_date, total_amount, vat_amount, category, confidence_score, image_storage_key, image_storage_uri)
VALUES (1, 'ABC', 'abc', '2026-09-10', 100.50, 0, 'Custom', 'High', 'opaque.jpg', 's3://test-bucket/opaque.jpg');
SELECT pg_temp.expect_error(
    $$INSERT INTO public.receipts (telegram_chat_id, vendor_name, normalized_vendor_name,
      receipt_date, total_amount, category, confidence_score, image_storage_key, image_storage_uri)
      VALUES (999, 'ABC', 'abc', '2026-09-10', 100.50, 'Other', 'High', 'other.jpg', 's3://test-bucket/other.jpg')$$, '23505');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET total_amount = 0$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET total_amount = -1$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET total_amount = 'NaN'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET total_amount = 10000000000$$, '22003');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET vat_amount = -1$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET vat_amount = 101$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET vat_amount = 'NaN'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET confidence_score = 'Maybe'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET category = E'\t'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET vendor_name = ''$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET normalized_vendor_name = ' '$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET image_storage_key = ''$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET category = NULL$$, '23502');

INSERT INTO public.pending_receipts (telegram_chat_id, telegram_user_id, vendor_name,
    normalized_vendor_name, receipt_date, total_amount, confidence_score, image_storage_key,
    state, expires_at, image_storage_uri)
VALUES (1, 2, 'ABC', 'abc', '2026-09-10', 100.50, 'Medium', 'opaque.jpg',
    'awaiting_category', now() + interval '30 minutes', 's3://test-bucket/opaque.jpg');
SELECT pg_temp.expect_error(
    $$INSERT INTO public.pending_receipts SELECT gen_random_uuid(), telegram_chat_id,
      telegram_user_id, vendor_name, normalized_vendor_name, receipt_date, total_amount,
      vat_amount, confidence_score, image_storage_key, category, state, created_at, expires_at, image_storage_uri
      FROM public.pending_receipts$$, '23505');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET state = 'queued'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET state = 'completed'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET expires_at = created_at$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET total_amount = 0$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET vat_amount = 101$$, '23514');
UPDATE public.pending_receipts SET state = 'expired';
INSERT INTO public.pending_receipts SELECT gen_random_uuid(), telegram_chat_id,
    telegram_user_id, vendor_name, normalized_vendor_name, receipt_date, total_amount,
    vat_amount, confidence_score, image_storage_key, category, 'awaiting_category',
    created_at, expires_at, image_storage_uri FROM public.pending_receipts;
UPDATE public.pending_receipts SET category = 'User supplied', state = 'completed'
WHERE state = 'awaiting_category';

SELECT pg_temp.expect_error($$UPDATE public.receipts SET image_storage_uri = NULL$$, '23502');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET image_storage_uri = 'https://example.invalid/signed?token=x'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.receipts SET image_storage_uri = 's3://test-bucket/wrong.jpg'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.pending_receipts SET image_storage_uri = 's3://test-bucket/wrong.jpg'$$, '23514');
-- Text representations used by repository projections retain all decimal digits.
DO $$
DECLARE amount TEXT;
BEGIN
    FOREACH amount IN ARRAY ARRAY['0.10', '12.30', '999999.99'] LOOP
        UPDATE public.receipts SET total_amount = amount::NUMERIC(12,2), vat_amount = NULL;
        IF (SELECT total_amount::TEXT FROM public.receipts) <> amount THEN
            RAISE EXCEPTION 'Decimal roundtrip failed';
        END IF;
    END LOOP;
END $$;

INSERT INTO public.telegram_updates(update_id) VALUES (7);
SELECT pg_temp.expect_error($$INSERT INTO public.telegram_updates(update_id) VALUES (7)$$, '23505');
SELECT pg_temp.expect_error($$INSERT INTO public.telegram_updates(update_id) VALUES (-1)$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.telegram_updates SET status='queued'$$, '23514');
SELECT pg_temp.expect_error($$UPDATE public.telegram_updates SET received_at=NULL$$, '23502');
SELECT pg_temp.expect_error($$UPDATE public.telegram_updates SET status=NULL$$, '23502');
UPDATE public.telegram_updates SET status='handled';
UPDATE public.telegram_updates SET status='failed';
DO $$
BEGIN
    IF has_table_privilege('anon', 'public.telegram_updates', 'SELECT')
       OR has_table_privilege('authenticated', 'public.telegram_updates', 'INSERT')
       OR EXISTS (SELECT 1 FROM pg_policies WHERE tablename='telegram_updates') THEN
        RAISE EXCEPTION 'Ingress events must remain backend-only';
    END IF;
    IF NOT has_table_privilege('service_role', 'public.telegram_updates', 'INSERT,SELECT,UPDATE') THEN
        RAISE EXCEPTION 'Server role needs ingress privileges';
    END IF;
END $$;
SET LOCAL ROLE service_role;
INSERT INTO public.telegram_updates(update_id) VALUES (8);
UPDATE public.telegram_updates SET status='handled' WHERE update_id=8;
RESET ROLE;
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_class WHERE oid IN
        ('public.vendors'::regclass, 'public.receipts'::regclass,
         'public.pending_receipts'::regclass, 'public.telegram_updates'::regclass)
         AND relrowsecurity) <> 4 THEN
        RAISE EXCEPTION 'RLS must be enabled on every application table';
    END IF;
    IF has_table_privilege('anon', 'public.receipts', 'SELECT')
       OR has_table_privilege('authenticated', 'public.pending_receipts', 'INSERT') THEN
        RAISE EXCEPTION 'Client roles must not access receipt data';
    END IF;
    IF NOT has_table_privilege('service_role', 'public.receipts', 'INSERT') THEN
        RAISE EXCEPTION 'Server role needs explicit DML privileges';
    END IF;
END $$;
ROLLBACK;
\echo PASS: database constraints, indexes, lifecycle checks and role privileges

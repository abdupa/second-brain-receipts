\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.expect_error(statement TEXT, expected TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
 BEGIN EXECUTE statement;
 EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE=expected THEN RETURN; END IF;
  RAISE;
 END;
 RAISE EXCEPTION 'expected error %', expected;
END $$;
CREATE FUNCTION pg_temp.pending(name TEXT, chat BIGINT DEFAULT 900008) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE new_id UUID := gen_random_uuid();
BEGIN
 INSERT INTO public.pending_receipts(id,telegram_chat_id,telegram_user_id,vendor_name,
 normalized_vendor_name,receipt_date,total_amount,vat_amount,confidence_score,image_storage_key,
 image_storage_uri,state,created_at,expires_at)
 VALUES(new_id,chat,chat,name,name,'2026-09-10',12.30,0.10,'High',new_id::TEXT||'.jpg',
 's3://test-bucket/'||new_id::TEXT||'.jpg','awaiting_category',clock_timestamp(),clock_timestamp()+interval '30 minutes');
 RETURN new_id;
END $$;
DO $$
DECLARE v_id UUID; result RECORD; existing public.pending_receipts%ROWTYPE;
BEGIN
 v_id := pg_temp.pending('memory-test');
 SELECT * INTO result FROM public.complete_pending_receipt(v_id,900008,900008,'Maintenance');
 ASSERT result.outcome='completed' AND NOT result.cleanup_allowed;
 ASSERT result.receipt->>'total_amount'='12.30' AND jsonb_typeof(result.receipt->'total_amount')='string';
 ASSERT result.receipt->>'vat_amount'='0.10';
 ASSERT result.receipt->>'category'='Maintenance';
 ASSERT result.receipt->>'id'=v_id::TEXT;
 ASSERT result.receipt->>'image_storage_uri'=result.pending->>'image_storage_uri';
 ASSERT (SELECT category FROM public.vendors WHERE normalized_name='memory-test')='Maintenance';
 ASSERT (SELECT state FROM public.pending_receipts WHERE pending_receipts.id=v_id)='completed';
 ASSERT (SELECT outcome FROM public.complete_pending_receipt(v_id,900008,900008,'Other'))='inactive';
 ASSERT (SELECT outcome FROM public.close_pending_receipt(v_id,900008,900008,'cancelled'))='inactive';
 ASSERT (SELECT count(*) FROM public.receipts WHERE normalized_vendor_name='memory-test')=1;
 -- Existing category wins, even if a competing workflow proposed a new category.
 v_id := pg_temp.pending('memory-test');
 UPDATE public.pending_receipts SET total_amount=99 WHERE pending_receipts.id=v_id;
 SELECT * INTO result FROM public.complete_pending_receipt(v_id,900008,900008,'Other');
 ASSERT result.receipt->>'category'='Maintenance';
 ASSERT (SELECT category FROM public.vendors WHERE normalized_name='memory-test')='Maintenance';
 -- Ownership failure exposes no row and performs no changes.
 v_id := pg_temp.pending('ownership');
 ASSERT (SELECT outcome FROM public.complete_pending_receipt(v_id,1,2,'Test'))='inactive';
 ASSERT (SELECT outcome FROM public.close_pending_receipt(v_id,1,2,'cancelled'))='inactive';
 ASSERT NOT EXISTS(SELECT 1 FROM public.vendors WHERE normalized_name='ownership');
 ASSERT (SELECT outcome FROM public.close_pending_receipt(v_id,900008,900008,'cancelled'))='cancelled';
 ASSERT (SELECT outcome FROM public.close_pending_receipt(v_id,900008,900008,'cancelled'))='inactive';
 -- Expired work never creates accounting data or memory.
 v_id := pg_temp.pending('expiry');
 UPDATE public.pending_receipts SET created_at=now()-interval '2 hours',expires_at=now()-interval '1 hour' WHERE pending_receipts.id=v_id;
 SELECT * INTO result FROM public.complete_pending_receipt(v_id,900008,900008,'Test');
 ASSERT result.outcome='expired' AND result.cleanup_allowed;
 ASSERT NOT EXISTS(SELECT 1 FROM public.receipts WHERE receipts.id=v_id);
 ASSERT NOT EXISTS(SELECT 1 FROM public.vendors WHERE normalized_name='expiry');
 -- Duplicate collision rolls back the vendor INSERT; separate close resolves pending.
 v_id := pg_temp.pending('duplicate');
 SELECT * INTO existing FROM public.pending_receipts WHERE pending_receipts.id=v_id;
 INSERT INTO public.receipts(telegram_chat_id,vendor_name,normalized_vendor_name,receipt_date,
 total_amount,category,confidence_score,image_storage_key,image_storage_uri)
 VALUES(900008,'duplicate','duplicate',existing.receipt_date,existing.total_amount,'Original','High',
 'other.jpg','s3://test-bucket/other.jpg');
 PERFORM pg_temp.expect_error(format('SELECT * FROM public.complete_pending_receipt(%L,900008,900008,%L)',v_id,'New'), '23505');
 ASSERT NOT EXISTS(SELECT 1 FROM public.vendors WHERE normalized_name='duplicate');
 ASSERT (SELECT state FROM public.pending_receipts WHERE pending_receipts.id=v_id)='awaiting_category';
 SELECT * INTO result FROM public.close_pending_receipt(v_id,900008,900008,'duplicate');
 ASSERT result.outcome='duplicate' AND result.cleanup_allowed AND result.pending->>'state'='failed';
 -- Same image referenced by final receipt: cleanup MUST NOT delete that object.
 v_id := pg_temp.pending('shared');
 SELECT * INTO existing FROM public.pending_receipts WHERE pending_receipts.id=v_id;
 INSERT INTO public.receipts(telegram_chat_id,vendor_name,normalized_vendor_name,receipt_date,
 total_amount,category,confidence_score,image_storage_key,image_storage_uri)
 VALUES(900008,'shared','shared',existing.receipt_date,existing.total_amount,'Original','High',
 existing.image_storage_key,existing.image_storage_uri);
 SELECT * INTO result FROM public.close_pending_receipt(v_id,900008,900008,'duplicate');
 ASSERT NOT result.cleanup_allowed;
 -- Category and Low-confidence validation run in SQL, not only Python.
 v_id := pg_temp.pending('invalid');
 PERFORM pg_temp.expect_error(format('SELECT * FROM public.complete_pending_receipt(%L,900008,900008,%L)',v_id,''),'22023');
 PERFORM pg_temp.expect_error(format('SELECT * FROM public.complete_pending_receipt(%L,900008,900008,%L)',v_id,repeat('x',101)),'22023');
 UPDATE public.pending_receipts SET confidence_score='Low' WHERE pending_receipts.id=v_id;
 PERFORM pg_temp.expect_error(format('SELECT * FROM public.complete_pending_receipt(%L,900008,900008,%L)',v_id,'Test'),'22023');
 PERFORM public.close_pending_receipt(v_id,900008,900008,'cancelled');
 ASSERT public.valid_receipt_category('Cancellation Fees');
 ASSERT public.valid_receipt_category(repeat('x',100));
 ASSERT NOT public.valid_receipt_category(E'Food\nSupplies');
 ASSERT NOT public.valid_receipt_category(' ');
 ASSERT NOT public.valid_receipt_category(' Test ');
 ASSERT NOT public.valid_receipt_category('x'||chr(8238));
END $$;
-- Inject a nonduplicate insert failure to prove the whole completion transaction rolls back.
CREATE FUNCTION pg_temp.reject_receipt() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.normalized_vendor_name='forced-error' THEN
  RAISE EXCEPTION 'forced test failure' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER pending_test_failure BEFORE INSERT ON public.receipts
 FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_receipt();
DO $$
DECLARE v_id UUID := pg_temp.pending('forced-error');
BEGIN
 PERFORM pg_temp.expect_error(format('SELECT * FROM public.complete_pending_receipt(%L,900008,900008,%L)',v_id,'New'),'23514');
 ASSERT NOT EXISTS(SELECT 1 FROM public.vendors WHERE normalized_name='forced-error');
 ASSERT NOT EXISTS(SELECT 1 FROM public.receipts WHERE receipts.id=v_id);
 ASSERT (SELECT state FROM public.pending_receipts WHERE pending_receipts.id=v_id)='awaiting_category';
END $$;
DO $$
DECLARE signature TEXT;
BEGIN
 FOREACH signature IN ARRAY ARRAY['public.complete_pending_receipt(uuid,bigint,bigint,text)',
 'public.close_pending_receipt(uuid,bigint,bigint,text)', 'public.valid_receipt_category(text)'] LOOP
  ASSERT NOT has_function_privilege('anon',signature,'EXECUTE');
  ASSERT NOT has_function_privilege('authenticated',signature,'EXECUTE');
  ASSERT has_function_privilege('service_role',signature,'EXECUTE');
  ASSERT (SELECT NOT prosecdef AND proconfig @> ARRAY['search_path=pg_catalog']
          FROM pg_proc WHERE oid=signature::regprocedure);
 END LOOP;
END $$;
SET LOCAL ROLE service_role;
SELECT * FROM public.complete_pending_receipt(gen_random_uuid(),1,1,'Test');
RESET ROLE;
SET LOCAL ROLE anon;
SELECT pg_temp.expect_error('SELECT * FROM public.complete_pending_receipt(gen_random_uuid(),1,1,''Test'')','42501');
RESET ROLE;
ROLLBACK;
\echo PASS: atomic pending completion, exact money, memory precedence, rollback, ownership, expiry, cancellation, duplicate cleanup and RPC privileges

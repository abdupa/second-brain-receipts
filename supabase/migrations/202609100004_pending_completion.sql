-- Persistent category completion. Earlier migrations remain unchanged.
BEGIN;
ALTER TABLE public.pending_receipts DROP CONSTRAINT pending_receipts_state_check;
ALTER TABLE public.pending_receipts ADD CONSTRAINT pending_receipts_state_check
 CHECK (state IN ('processing','awaiting_category','completed','failed','expired','cancelled'));

CREATE FUNCTION public.valid_receipt_category(value TEXT) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE STRICT SECURITY INVOKER SET search_path=pg_catalog AS $$
 SELECT char_length(value) BETWEEN 1 AND 100
 AND value = btrim(value, convert_from(decode('20c2a0e19a80e28080e28081e28082e28083e28084e28085e28086e28087e28088e28089e2808ae280afe2819fe38080', 'hex'),'UTF8'))
 AND translate(value, convert_from(decode('0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f7fc280c281c282c283c284c285c286c287c288c289c28ac28bc28cc28dc28ec28fc290c291c292c293c294c295c296c297c298c299c29ac29bc29cc29dc29ec29fd89ce2808be2808ce2808de2808ee2808fe280a8e280a9e280aae280abe280ace280ade280aee281a0e281a6e281a7e281a8e281a9efbbbf', 'hex'),'UTF8'), '') = value
$$;
ALTER TABLE public.vendors ADD CONSTRAINT vendors_category_valid CHECK (public.valid_receipt_category(category));
ALTER TABLE public.receipts ADD CONSTRAINT receipts_category_valid CHECK (public.valid_receipt_category(category));
ALTER TABLE public.pending_receipts ADD CONSTRAINT pending_category_valid
 CHECK (category IS NULL OR public.valid_receipt_category(category));

CREATE FUNCTION public.complete_pending_receipt(p_pending_id UUID, p_chat_id BIGINT,
 p_user_id BIGINT, p_category TEXT)
RETURNS TABLE(outcome TEXT, pending JSONB, receipt JSONB, cleanup_allowed BOOLEAN)
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE
 work public.pending_receipts%ROWTYPE;
 saved public.receipts%ROWTYPE;
 resolved_category TEXT;
BEGIN
 IF p_category IS NULL OR NOT public.valid_receipt_category(p_category) THEN
   RAISE EXCEPTION 'invalid category' USING ERRCODE='22023';
 END IF;
 SELECT * INTO work FROM public.pending_receipts p WHERE p.id=p_pending_id
   AND p.telegram_chat_id=p_chat_id AND p.telegram_user_id=p_user_id FOR UPDATE;
 IF NOT FOUND OR work.state <> 'awaiting_category' THEN
   RETURN QUERY SELECT 'inactive'::TEXT, NULL::JSONB, NULL::JSONB, false; RETURN;
 END IF;
 IF work.expires_at <= clock_timestamp() THEN
   UPDATE public.pending_receipts SET state='expired' WHERE id=work.id
     AND state='awaiting_category' RETURNING * INTO work;
   outcome := 'expired';
 ELSE
   IF work.confidence_score='Low' THEN
     RAISE EXCEPTION 'unaccepted confidence' USING ERRCODE='22023';
   END IF;
   -- Identity conflict locks the vendor; existing saved category is never replaced.
   INSERT INTO public.vendors AS v(normalized_name,display_name,category)
     VALUES(work.normalized_vendor_name,work.vendor_name,p_category)
     ON CONFLICT(normalized_name) DO UPDATE SET normalized_name=EXCLUDED.normalized_name
     RETURNING v.category INTO resolved_category;
   -- Recheck wall clock after a possible wait on vendor identity. Throwing rolls
   -- back even a newly created vendor; the caller discovers expiry on its next access.
   IF work.expires_at <= clock_timestamp() THEN
     RAISE EXCEPTION 'pending expired' USING ERRCODE='P0002';
   END IF;
   INSERT INTO public.receipts(id,telegram_chat_id,vendor_name,normalized_vendor_name,
     receipt_date,total_amount,vat_amount,category,confidence_score,image_storage_key,image_storage_uri)
   VALUES(work.id,work.telegram_chat_id,work.vendor_name,work.normalized_vendor_name,
     work.receipt_date,work.total_amount,work.vat_amount,resolved_category,work.confidence_score,
     work.image_storage_key,work.image_storage_uri) RETURNING * INTO saved;
   IF work.expires_at <= clock_timestamp() THEN
     RAISE EXCEPTION 'pending expired' USING ERRCODE='P0002';
   END IF;
   -- Any receipt uniqueness/check failure rolls back the vendor mutation above.
   UPDATE public.pending_receipts SET state='completed', category=resolved_category
     WHERE id=work.id AND state='awaiting_category' RETURNING * INTO work;
   outcome := 'completed';
   receipt := to_jsonb(saved) || jsonb_build_object('total_amount',saved.total_amount::TEXT,
                                                  'vat_amount',saved.vat_amount::TEXT);
 END IF;
 pending := to_jsonb(work) || jsonb_build_object('total_amount',work.total_amount::TEXT,
                                               'vat_amount',work.vat_amount::TEXT);
 cleanup_allowed := outcome <> 'completed' AND NOT EXISTS(
   SELECT 1 FROM public.receipts r WHERE r.image_storage_uri=work.image_storage_uri);
 RETURN NEXT;
END $$;

CREATE FUNCTION public.close_pending_receipt(p_pending_id UUID, p_chat_id BIGINT,
 p_user_id BIGINT, p_reason TEXT)
RETURNS TABLE(outcome TEXT, pending JSONB, receipt JSONB, cleanup_allowed BOOLEAN)
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
DECLARE work public.pending_receipts%ROWTYPE;
BEGIN
 IF p_reason NOT IN ('cancelled','expired','duplicate') OR p_reason IS NULL THEN
   RAISE EXCEPTION 'invalid close reason' USING ERRCODE='22023';
 END IF;
 SELECT * INTO work FROM public.pending_receipts p WHERE p.id=p_pending_id
   AND p.telegram_chat_id=p_chat_id AND p.telegram_user_id=p_user_id FOR UPDATE;
 IF NOT FOUND OR work.state <> 'awaiting_category' THEN
   RETURN QUERY SELECT 'inactive'::TEXT, NULL::JSONB, NULL::JSONB, false; RETURN;
 END IF;
 IF p_reason='duplicate' THEN
   IF NOT EXISTS(SELECT 1 FROM public.receipts r
      WHERE r.normalized_vendor_name=work.normalized_vendor_name
      AND r.receipt_date=work.receipt_date AND r.total_amount=work.total_amount) THEN
      RAISE EXCEPTION 'duplicate not found' USING ERRCODE='P0001';
   END IF;
   outcome := 'duplicate';
 ELSIF work.expires_at <= clock_timestamp() THEN outcome := 'expired';
 ELSIF p_reason='expired' THEN
   RETURN QUERY SELECT 'inactive'::TEXT, NULL::JSONB, NULL::JSONB, false; RETURN;
 ELSE outcome := 'cancelled';
 END IF;
 UPDATE public.pending_receipts SET state=CASE WHEN outcome='duplicate' THEN 'failed' ELSE outcome END
   WHERE id=work.id AND state='awaiting_category' RETURNING * INTO work;
 pending := to_jsonb(work) || jsonb_build_object('total_amount',work.total_amount::TEXT,
                                               'vat_amount',work.vat_amount::TEXT);
 cleanup_allowed := NOT EXISTS(SELECT 1 FROM public.receipts r
                               WHERE r.image_storage_uri=work.image_storage_uri);
 RETURN NEXT;
END $$;

REVOKE ALL ON FUNCTION public.valid_receipt_category(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.complete_pending_receipt(UUID,BIGINT,BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.close_pending_receipt(UUID,BIGINT,BIGINT,TEXT) FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
 FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE format('REVOKE ALL ON FUNCTION public.valid_receipt_category(TEXT), public.complete_pending_receipt(UUID,BIGINT,BIGINT,TEXT), public.close_pending_receipt(UUID,BIGINT,BIGINT,TEXT) FROM %I',role_name);
  END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
  GRANT EXECUTE ON FUNCTION public.valid_receipt_category(TEXT),
   public.complete_pending_receipt(UUID,BIGINT,BIGINT,TEXT),
   public.close_pending_receipt(UUID,BIGINT,BIGINT,TEXT) TO service_role;
 END IF;
END $$;
COMMIT;

-- Intentionally fail on populated tables: never invent a bucket for old objects.
-- Such deployments need a reviewed mapping/backfill before requiring this column.
BEGIN;
ALTER TABLE public.receipts ADD COLUMN image_storage_uri TEXT NOT NULL;
ALTER TABLE public.pending_receipts ADD COLUMN image_storage_uri TEXT NOT NULL;
ALTER TABLE public.receipts ADD CONSTRAINT receipts_s3_reference_check CHECK (
    image_storage_uri ~ '^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[^[:space:]?#]+$'
    AND substring(image_storage_uri FROM '^s3://[^/]+/(.*)$') = image_storage_key
);
ALTER TABLE public.pending_receipts ADD CONSTRAINT pending_s3_reference_check CHECK (
    image_storage_uri ~ '^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[^[:space:]?#]+$'
    AND substring(image_storage_uri FROM '^s3://[^/]+/(.*)$') = image_storage_key
);
COMMIT;

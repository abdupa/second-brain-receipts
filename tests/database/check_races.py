"""Opt-in disposable PostgreSQL check. Run only against the named local test container."""

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

CONTAINER = "second-brain-receipts-v006-db"
COMMAND = [
    "docker",
    "exec",
    "-i",
    CONTAINER,
    "psql",
    "-U",
    "postgres",
    "-Atq",
    "-v",
    "ON_ERROR_STOP=1",
    "-v",
    "VERBOSITY=verbose",
]


def sql(statement):
    return subprocess.run(COMMAND, input=statement, text=True, capture_output=True, timeout=15)


def check_race(table, statement, constraint, *, claim=False):
    predicate = "update_id=900006" if claim else "normalized_vendor_name='race-only-v005'"
    first = subprocess.Popen(
        COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        first.stdin.write(f"BEGIN;\n{statement}\n\\echo LOCK_HELD\n")
        first.stdin.flush()
        assert first.stdout.readline().strip() == "LOCK_HELD"
        with ThreadPoolExecutor(max_workers=1) as pool:
            competing = statement.rstrip(";") + " RETURNING update_id;" if claim else statement
            second = pool.submit(sql, "SET application_name='receipts-race-second';\n" + competing)
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                result = sql(
                    "SELECT count(*) FROM pg_stat_activity WHERE "
                    "application_name='receipts-race-second' AND wait_event_type='Lock';"
                )
                if result.stdout.strip() == "1":
                    waiting = True
                    break
                time.sleep(0.05)
            # Release even on a failed assertion, so the competing process can terminate.
            first.stdin.write("COMMIT;\n\\q\n")
            first.stdin.flush()
            first.wait(timeout=5)
            loser = second.result(timeout=10)
            assert waiting, "second insert did not wait on the first transaction"
            assert first.returncode == 0
            if claim:
                assert loser.returncode == 0 and not loser.stdout.strip()
            else:
                assert (
                    loser.returncode != 0 and "23505" in loser.stderr and constraint in loser.stderr
                )
            assert (
                sql(f"SELECT count(*) FROM public.{table} WHERE {predicate};").stdout.strip() == "1"
            )
        print(f"PASS: concurrent {table} inserts enforce {constraint}")
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        for stream in (first.stdin, first.stdout, first.stderr):
            stream.close()
        result = sql(f"DELETE FROM public.{table} WHERE {predicate};")
        assert result.returncode == 0


if __name__ == "__main__":
    common_columns = (
        "telegram_chat_id,vendor_name,normalized_vendor_name,receipt_date,total_amount,"
        "category,confidence_score,image_storage_key,image_storage_uri"
    )
    common_values = (
        "900005,'Race Only V005','race-only-v005','2026-09-10',12.30,"
        "'Test','High','race.jpg','s3://test-bucket/race.jpg'"
    )
    check_race(
        "receipts",
        f"INSERT INTO public.receipts ({common_columns}) VALUES ({common_values});",
        "receipts_vendor_date_total_key",
    )
    check_race(
        "pending_receipts",
        f"INSERT INTO public.pending_receipts ({common_columns},"
        "telegram_user_id,state,expires_at) "
        f"VALUES ({common_values},900005,'awaiting_category',now()+interval '30 minutes');",
        "pending_receipts_one_question_idx",
    )

    check_race(
        "telegram_updates",
        "INSERT INTO public.telegram_updates(update_id) VALUES (900006) "
        "ON CONFLICT(update_id) DO NOTHING;",
        "telegram_updates_pkey (one durable owner, competing claim ignored)",
        claim=True,
    )

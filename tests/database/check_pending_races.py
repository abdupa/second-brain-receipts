"""Real competing RPC transactions, never a live database. Run after all migrations."""

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from check_races import COMMAND, sql


def check_race(second_operation="complete", same_vendor=False):
    first_id, second_id = str(uuid4()), str(uuid4())
    name = "pending-race-" + first_id
    ids = [first_id, second_id] if same_vendor else [first_id]
    for index, pending_id in enumerate(ids):
        result = sql(f"""INSERT INTO public.pending_receipts(id,telegram_chat_id,telegram_user_id,
            vendor_name,normalized_vendor_name,receipt_date,total_amount,confidence_score,
            image_storage_key,image_storage_uri,state,expires_at)
            VALUES('{pending_id}',{910000 + index},{910000 + index},'{name}','{name}',
            '2026-09-10',{12 + index},'High','{pending_id}.jpg','s3://test-bucket/{pending_id}.jpg',
            'awaiting_category',now()+interval '30 minutes');""")
        assert result.returncode == 0, result.stderr
    first = subprocess.Popen(
        COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        first.stdin.write(
            "BEGIN; SELECT outcome FROM public.complete_pending_receipt("
            f"'{first_id}',910000,910000,'First');\n"
        )
        first.stdin.flush()
        assert first.stdout.readline().strip() == "completed"
        second_pending = second_id if same_vendor else first_id
        chat = 910001 if same_vendor else 910000
        function = (
            "complete_pending_receipt"
            if second_operation == "complete"
            else "close_pending_receipt"
        )
        category = "Second" if second_operation == "complete" else "cancelled"
        statement = (
            "SET application_name='pending-race-second'; "
            f"SELECT outcome FROM public.{function}("
            f"'{second_pending}',{chat},{chat},'{category}');"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sql, statement)
            deadline = time.monotonic() + 5
            locked = False
            while time.monotonic() < deadline:
                if (
                    sql(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "application_name='pending-race-second' AND wait_event_type='Lock';"
                    ).stdout.strip()
                    == "1"
                ):
                    locked = True
                    break
                time.sleep(0.02)
            first.stdin.write("COMMIT;\n\\q\n")
            first.stdin.flush()
            first.wait(timeout=5)
            result = future.result(timeout=10)
            assert locked and first.returncode == 0
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == ("completed" if same_vendor else "inactive")
        expected = 2 if same_vendor else 1
        assert sql(
            f"SELECT count(*) FROM public.receipts WHERE normalized_vendor_name='{name}';"
        ).stdout.strip() == str(expected)
        assert (
            sql(
                "SELECT count(*) FROM public.vendors WHERE "
                f"normalized_name='{name}' AND category='First';"
            ).stdout.strip()
            == "1"
        )
        assert sql(
            "SELECT count(*) FROM public.receipts WHERE "
            f"normalized_vendor_name='{name}' AND category='First';"
        ).stdout.strip() == str(expected)
        print(
            f"PASS: pending RPC race operation={second_operation} same_vendor={same_vendor}; "
            "row/vendor locks preserve one completion and category memory"
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        for stream in (first.stdin, first.stdout, first.stderr):
            stream.close()
        result = sql(
            f"DELETE FROM public.pending_receipts WHERE normalized_vendor_name='{name}'; "
            f"DELETE FROM public.receipts WHERE normalized_vendor_name='{name}'; "
            f"DELETE FROM public.vendors WHERE normalized_name='{name}';"
        )
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    check_race()
    check_race("cancel")
    check_race(same_vendor=True)


def check_expiry_while_locked():
    pending_id = str(uuid4())
    name = "expiry-lock-" + pending_id
    result = sql(f"""INSERT INTO public.pending_receipts(id,telegram_chat_id,telegram_user_id,
        vendor_name,normalized_vendor_name,receipt_date,total_amount,confidence_score,
        image_storage_key,image_storage_uri,state,created_at,expires_at)
        VALUES('{pending_id}',920008,920008,'{name}','{name}','2026-09-10',12,'High',
        '{pending_id}.jpg','s3://test-bucket/{pending_id}.jpg','awaiting_category',
        now()-interval '1 hour',now()+interval '30 minutes');""")
    assert result.returncode == 0, result.stderr
    first = subprocess.Popen(
        COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        first.stdin.write(
            "BEGIN; UPDATE public.pending_receipts SET "
            "expires_at=clock_timestamp()+interval '200 milliseconds' "
            f"WHERE id='{pending_id}';\n\\echo LOCKED\n"
        )
        first.stdin.flush()
        assert first.stdout.readline().strip() == "LOCKED"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                sql,
                "SELECT outcome FROM public.complete_pending_receipt("
                f"'{pending_id}',920008,920008,'Test');",
            )
            time.sleep(0.4)
            first.stdin.write("COMMIT;\n\\q\n")
            first.stdin.flush()
            first.wait(timeout=5)
            result = future.result(timeout=10)
            assert result.returncode == 0 and result.stdout.strip() == "expired", result.stderr
        assert (
            sql(
                f"SELECT count(*) FROM public.vendors WHERE normalized_name='{name}';"
            ).stdout.strip()
            == "0"
        )
        assert (
            sql(f"SELECT count(*) FROM public.receipts WHERE id='{pending_id}';").stdout.strip()
            == "0"
        )
        print("PASS: completion rechecks wall-clock expiry after waiting on a row lock")
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        for stream in (first.stdin, first.stdout, first.stderr):
            stream.close()
        assert sql(f"DELETE FROM public.pending_receipts WHERE id='{pending_id}';").returncode == 0


if __name__ == "__main__":
    check_expiry_while_locked()

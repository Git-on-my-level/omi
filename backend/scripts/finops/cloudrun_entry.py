#!/usr/bin/env python3
"""Cloud Run Job entrypoint for the daily Omi unit-cost producer.

Contract (matches the laptop cron shim, minus laptop-only paths):

  * date    = today (UTC) - 2, the GCP billing-export settlement frontier (D-2).
  * overlap = one producer run per date, globally. A GCS lease object
              (``gs://<FINOPS_LOCK_BUCKET>/locks/unit-cost/<date>.lock``) is created
              with a generation precondition; a second concurrent run loses the race
              and exits 0 quietly (Cloud Scheduler retries + manual runs must not
              double-write or double-page).
  * auth    = ADC of the job's runtime service account (FINOPS_AUTH=cloudrun).
  * failure = non-zero exit so Cloud Run Job reports the execution as failed.

Required runtime configuration:
  FINOPS_LOCK_BUCKET      GCS bucket for the daily overlap lease (name only, no secret).
  FINOPS_SECRETS_DIR      dir where Secret Manager volumes are mounted (default
                          /run/secrets/finops), containing the provider/prometheus/stripe
                          keys by file name (see cloudrun.SECRET_FILES).
Optional:
  FINOPS_WRITER_SA        expected runtime SA email (default finops-writer@based-hardware).
  FINOPS_RUN_ROOT         scratch dir for run artefacts (default /tmp/finops-runs).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cloudrun  # noqa: E402

SETTLEMENT_LAG_DAYS = 2
LOCK_TTL_HOURS = 6  # a crashed run's lease expires; content is just provenance metadata


def lease_blob(date: str):
    from google.cloud import storage

    bucket_name = os.environ.get("FINOPS_LOCK_BUCKET", "").strip()
    if not bucket_name:
        raise SystemExit("FINOPS_LOCK_BUCKET is required for overlap protection")
    client = storage.Client(project=cloudrun.PROJECT)
    return client.bucket(bucket_name).blob("locks/unit-cost/%s.lock" % date)


def acquire_lease(date: str) -> bool:
    """Create-if-absent race on a GCS object; True = this run owns the date."""
    blob = lease_blob(date)
    payload = json.dumps(
        {
            "date": date,
            "run_id": uuid.uuid4().hex[:12],
            "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "holder": cloudrun.runtime_identity(),
            "ttl_hours": LOCK_TTL_HOURS,
        },
        indent=1,
    )
    try:
        blob.upload_from_string(payload, if_generation_match=0)
        return True
    except Exception as e:  # noqa: BLE001
        # 412 = precondition failed: someone else owns the lease
        if "412" in str(e) or "Precondition" in type(e).__name__:
            return False
        raise


def takeover_if_stale(date: str) -> bool:
    """If the existing lease is older than LOCK_TTL_HOURS, replace it (crashed run)."""
    blob = lease_blob(date)
    try:
        blob.reload()
    except Exception:  # noqa: BLE001
        return acquire_lease(date)
    age_h = (dt.datetime.now(dt.timezone.utc) - blob.updated).total_seconds() / 3600.0
    if age_h < LOCK_TTL_HOURS:
        return False
    payload = json.dumps(
        {
            "date": date,
            "run_id": uuid.uuid4().hex[:12],
            "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "holder": cloudrun.runtime_identity(),
            "takeover_of_stale_lease": True,
            "ttl_hours": LOCK_TTL_HOURS,
        },
        indent=1,
    )
    try:
        blob.upload_from_string(payload, if_generation_match=blob.generation)
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    date = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=SETTLEMENT_LAG_DAYS)).isoformat()
    identity = cloudrun.assert_runtime_identity()
    sys.stderr.write("finops cloudrun producer: date=%s runtime_sa=%s\n" % (date, identity))

    if not acquire_lease(date):
        if not takeover_if_stale(date):
            sys.stderr.write("another run owns %s; exiting cleanly (overlap protection)\n" % date)
            return 0
        sys.stderr.write("took over a stale lease for %s\n" % date)

    os.environ.setdefault("FINOPS_RUN_ROOT", "/tmp/finops-runs")
    os.environ["FINOPS_AUTH"] = "cloudrun"

    run = subprocess_run(date)
    return run


def subprocess_run(date: str) -> int:
    import subprocess

    entry = HERE / "run_unit_cost.py"
    env = dict(os.environ)
    env["FINOPS_AUTH"] = "cloudrun"
    p = subprocess.run(
        [sys.executable, str(entry), "--date", date, "--load"],
        capture_output=True,
        text=True,
        timeout=3600,
        env=env,
    )
    if p.returncode != 0:
        sys.stderr.write("FINOPS FAILED for %s (rc=%d)\n" % (date, p.returncode))
        tail = [l for l in p.stderr.strip().splitlines() if l.strip()][-40:]
        sys.stderr.write("\n".join(tail) + "\n")
        return p.returncode
    sys.stdout.write(p.stdout.strip() + "\n")
    return 0


if __name__ == "__main__":
    # Preflight subcommands used by the deploy workflow's image smoke:
    #   (no args) = normal daily production run
    #   identity  = assert the runtime SA and print it (used with ADC present)
    #   --help    = usage only
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        if arg == "identity":
            sys.exit(0 if cloudrun.assert_runtime_identity() else 1)
        raise SystemExit("usage: cloudrun_entry.py [--help | identity]")
    sys.exit(main())

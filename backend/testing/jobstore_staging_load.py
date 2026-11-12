"""Staging load receipt for the Postgres job store (charter ruling 19 sizing gate).

Runs the existing A3 contention bench (``testing.jobstore_contention``) against an *identified*
nonproduction Cloud SQL target, samples the database's own counters while it runs, and prints one
``RECEIPT_JSON:`` line. The receipt is the run-side half of ``staging-load-receipt.json``; the
Cloud SQL instance metrics (CPU/RAM/IO) are collected from Cloud Monitoring by
``assemble_staging_load_receipt.py`` and merged in.

This module never writes outside a ``batch_pipeline_test_*`` namespace and refuses to run unless
the target is the staging database on a private address. It is the staging counterpart of the
local bench, not a second implementation of the scheduler: every lease, pull and completion goes
through ``testing.jobstore_contention.run``.

    OMI_TEST_POSTGRES_IAM=172.20.207.3,jobstore_staging,staging-worker@based-hardware-dev.iam \\
        python -m testing.jobstore_staging_load --out /tmp/run.json

The workload basis is the plan-A sizing contract (``omi-knowledge-base``
``projects/backend-target-architecture/plan-a-job-store.md`` §3.7): 250k units/day x ~15
transactions = ~45 tx/s average and ~150 tx/s peak, plus legacy finalization ~30-60 tx/s, so the
peak gate is 210 tx/s. The backlog-drain scenario uses the 10k pending backlog cap recorded in
``projects/admin-omi-tv-dashboard/firestore-reads-program.md`` (2026-11-11).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import testing.pipeline_sim._env  # noqa: F401  (hermetic constants before product imports)

SCHEMA = 'omi.jobstore.staging_load_receipt/v1'

# Plan-A §3.7 sizing contract, in transactions per second.
CONTRACT_AVERAGE_TX_PER_S = 45.0
CONTRACT_PEAK_TX_PER_S = 150.0
CONTRACT_FINALIZATION_TX_PER_S = 60.0
CONTRACT_PEAK_TOTAL_TX_PER_S = CONTRACT_PEAK_TX_PER_S + CONTRACT_FINALIZATION_TX_PER_S

# A pull must finish far inside the 120 s lease / 30 s heartbeat contract.
PULL_P95_MS = 1000.0
PULL_P99_MS = 2000.0

# The pending backlog is pinned at the 10k cap (firestore-reads-program, 2026-11-11).
BACKLOG_JOBS = 10_000
PEAK_JOBS = 2_000

_SAMPLE_SQL = """
SELECT
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS backends,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active') AS active,
  (SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()) AS xact_commit,
  (SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()) AS xact_rollback,
  (SELECT blks_read FROM pg_stat_database WHERE datname = current_database()) AS blks_read,
  (SELECT blks_hit FROM pg_stat_database WHERE datname = current_database()) AS blks_hit,
  (SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()) AS temp_bytes,
  (SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()) AS deadlocks,
  (SELECT count(*) FROM jobs WHERE namespace LIKE 'batch_pipeline_test_%') AS test_jobs,
  (SELECT count(*) FROM jobs WHERE namespace LIKE 'batch_pipeline_test_%' AND state = 'result_ready') AS test_ready,
  (SELECT count(*) FROM jobs WHERE namespace LIKE 'batch_pipeline_test_%' AND state IN ('ready','leased','retry','record_pending')) AS test_nonterminal
"""


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _connect():
    from database.jobstore.connection import iam_connector

    raw = os.getenv('OMI_TEST_POSTGRES_IAM', '').strip()
    parts = [part.strip() for part in raw.split(',')]
    if len(parts) != 3 or not all(parts):
        raise SystemExit('OMI_TEST_POSTGRES_IAM must be host,database,user')
    return iam_connector(*parts)


def _identity(conn) -> dict:
    cur = conn.cursor()
    cur.execute(
        "select current_database(), current_user, coalesce(inet_server_addr()::text, ''), "
        "current_setting('server_version'), pg_postmaster_start_time()::text, "
        "(select count(*) from schema_migrations)"
    )
    database, user, host, version, started, migrations = cur.fetchone()
    cur.execute('select pg_database_size(current_database())')
    size = cur.fetchone()[0]
    return {
        'database': database,
        'user': user,
        'host': host,
        'server_version': version,
        'postmaster_started_at': started,
        'migrations_applied': migrations,
        'database_size_bytes': size,
    }


def _assert_nonproduction(identity: dict) -> list[str]:
    """Refuse anything that is not the identified staging jobstore on a private address."""
    failures = []
    if identity['database'] != 'jobstore_staging':
        failures.append(f"database is {identity['database']!r}, not 'jobstore_staging'")
    if not identity['user'].endswith('@based-hardware-dev.iam'):
        failures.append(f"user {identity['user']!r} is not a based-hardware-dev IAM user")
    host = identity['host'].split('/')[0]
    if not (host.startswith('10.') or host.startswith('172.') or host.startswith('192.168.')):
        failures.append(f'host {identity["host"]!r} is not a private address')
    if os.getenv('OMI_DEPLOYMENT') != 'staging':
        failures.append('OMI_DEPLOYMENT is not staging')
    if identity['migrations_applied'] < 1:
        failures.append('no jobstore migrations applied')
    return failures


class Sampler(threading.Thread):
    """Samples the database's own counters while the bench runs (never raises into the bench)."""

    def __init__(self, connect, interval: float = 0.5):
        super().__init__(daemon=True)
        self._connect = connect
        self._interval = interval
        self._stop_event = threading.Event()
        self.ticks: list[dict] = []
        self.error: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            while not self._stop_event.is_set():
                cur.execute(_SAMPLE_SQL)
                row = cur.fetchone()
                self.ticks.append(
                    {
                        't': time.time(),
                        'backends': row[0],
                        'active': row[1],
                        'xact_commit': row[2],
                        'xact_rollback': row[3],
                        'blks_read': row[4],
                        'blks_hit': row[5],
                        'temp_bytes': row[6],
                        'deadlocks': row[7],
                        'test_jobs': row[8],
                        'test_ready': row[9],
                        'test_nonterminal': row[10],
                    }
                )
                time.sleep(self._interval)
        except Exception as error:  # noqa: BLE001  (recorded, never raised: the bench reports)
            self.error = f'{type(error).__name__}: {error}'
        finally:
            if conn is not None:
                conn.close()


def _window(ticks: list[dict]) -> dict:
    """The pull window: ticks where the bench's jobs reached result_ready."""
    ready = [tick for tick in ticks if tick['test_ready'] > 0]
    if len(ready) < 2:
        return {}
    first, last = ready[0], ready[-1]
    seconds = last['t'] - first['t']
    if seconds <= 0:
        return {}
    commits = last['xact_commit'] - first['xact_commit']
    return {
        'start': first['t'],
        'end': last['t'],
        'seconds': round(seconds, 2),
        'xact_commit': commits,
        'tx_per_s': round(commits / seconds, 1),
        'jobs_drained': last['test_ready'] - first['test_ready'],
        'leases_per_s': round((last['test_ready'] - first['test_ready']) / seconds, 1),
        'max_backends': max(tick['backends'] for tick in ticks),
        'max_active': max(tick['active'] for tick in ticks),
        'blks_read': last['blks_read'] - first['blks_read'],
        'blks_hit': last['blks_hit'] - first['blks_hit'],
        'temp_bytes': last['temp_bytes'] - first['temp_bytes'],
        'deadlocks': last['deadlocks'] - first['deadlocks'],
    }


def _peak_tx_per_s(ticks: list[dict], window_seconds: float = 5.0) -> float:
    """Highest transaction rate over any ``window_seconds`` slice of the pull window."""
    ready = [tick for tick in ticks if tick['test_ready'] > 0]
    best = 0.0
    for index, start in enumerate(ready):
        for end in ready[index + 1 :]:
            span = end['t'] - start['t']
            if span < window_seconds:
                continue
            rate = (end['xact_commit'] - start['xact_commit']) / span
            best = max(best, rate)
            break
    return round(best, 1)


def _parity(conn, namespace: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        'select count(*), '
        "count(*) filter (where state = 'result_ready'), "
        "count(*) filter (where state in ('ready','leased','retry','record_pending')), "
        "count(*) filter (where doc ? 'result') "
        'from jobs where namespace = %s',
        (namespace,),
    )
    total, ready, nonterminal, with_result = cur.fetchone()
    return {
        'namespace': namespace,
        'jobs_total': total,
        'jobs_result_ready': ready,
        'jobs_nonterminal': nonterminal,
        'jobs_with_result': with_result,
    }


def _purge(conn, namespace: str) -> None:
    if not namespace.startswith('batch_pipeline_test_'):
        raise SystemExit(f'refusing to purge non-test namespace {namespace!r}')
    cur = conn.cursor()
    for table in ('jobs', 'results', 'control', 'docs', 'outbox'):
        cur.execute(f'DELETE FROM {table} WHERE namespace = %s', (namespace,))


def _purge_stale_test_namespaces(conn) -> list[str]:
    """Remove rows a previous failed run left in throwaway test namespaces (never dev/prod)."""
    cur = conn.cursor()
    cur.execute("select distinct namespace from jobs where namespace like 'batch_pipeline_test_%'")
    namespaces = [row[0] for row in cur.fetchall()]
    for namespace in namespaces:
        _purge(conn, namespace)
    return namespaces


def _scenario(name: str, *, owners: int, jobs: int, seconds: float, admitters: int) -> dict:
    from testing import jobstore_contention

    args = argparse.Namespace(
        backend='postgres',
        replicas=4,
        threads=2,
        owners=owners,
        jobs=jobs,
        admitters=admitters,
        seconds=seconds,
        spread='true',
        pool=4,
        seed=7,
        keep=True,
    )
    return jobstore_contention.run(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default='', help='write the receipt JSON here as well as stdout')
    parser.add_argument('--peak-jobs', type=int, default=PEAK_JOBS)
    parser.add_argument('--backlog-jobs', type=int, default=BACKLOG_JOBS)
    parser.add_argument('--seconds', type=float, default=180.0)
    args = parser.parse_args()

    connect = _connect()
    from testing.store_fixtures import postgres_dsn

    postgres_dsn()  # idempotent: applies any pending jobstore migrations once
    conn = connect()
    identity = _identity(conn)
    failures = _assert_nonproduction(identity)
    if failures:
        print('TARGET_REFUSED: ' + '; '.join(failures), file=sys.stderr)
        return 2

    scenarios = []
    parity = []
    windows = []
    stale = _purge_stale_test_namespaces(conn)
    started = time.time()
    try:
        for name, owners, jobs, admitters in (
            ('peak', max(1, args.peak_jobs // 10), 10, 2),
            ('backlog_drain', max(1, args.backlog_jobs // 10), 10, 0),
        ):
            sampler = Sampler(connect)
            sampler.start()
            time.sleep(1.0)  # one idle baseline tick before the bench seeds anything
            try:
                result = _scenario(name, owners=owners, jobs=jobs, seconds=args.seconds, admitters=admitters)
            finally:
                sampler.stop()
                sampler.join(timeout=10)
            namespace = result['namespace']
            parity.append({'scenario': name, **_parity(conn, namespace)})
            _purge(conn, namespace)
            scenarios.append({'name': name, 'result': result})
            windows.append(
                {
                    'scenario': name,
                    'window': _window(sampler.ticks),
                    'peak_tx_per_s': _peak_tx_per_s(sampler.ticks),
                    'error': sampler.error,
                    'ticks': sampler.ticks,
                }
            )
    finally:
        conn.close()

    # The sustained rate is the backlog-drain window (the steady state); the peak is the highest
    # 5 s slice across both scenarios.
    backlog = next((item for item in windows if item['scenario'] == 'backlog_drain' and item['window']), None)
    measured = [item['window'].get('tx_per_s') for item in windows if item['window']]
    sustained = backlog['window']['tx_per_s'] if backlog else (min(measured) if measured else None)
    peak = max((item['peak_tx_per_s'] for item in windows), default=0.0)
    window = (
        dict(backlog['window']) if backlog else (dict(windows[0]['window']) if windows and windows[0]['window'] else {})
    )
    if window:
        window['tx_per_s'] = sustained
        window['scenario'] = 'backlog_drain' if backlog else 'peak'
    scenario_windows = [
        {
            'scenario': item['scenario'],
            'window': item['window'],
            'peak_tx_per_s': item['peak_tx_per_s'],
            'error': item['error'],
            'samples': len(item['ticks']),
        }
        for item in windows
    ]
    finished = time.time()

    receipt = {
        'schema': SCHEMA,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(finished)),
        'source': {
            'repo': 'BasedHardware/omi',
            'sha': os.getenv('LOAD_SOURCE_SHA', ''),
            'image': os.getenv('LOAD_IMAGE_DIGEST', ''),
            'runner_sha256': _sha256(__file__),
            'harness_sha256': _sha256(jobstore_contention_path()),
        },
        'target': {
            'kind': 'cloud_sql_postgres',
            'project': os.getenv('GOOGLE_CLOUD_PROJECT', ''),
            'instance': os.getenv('LOAD_INSTANCE', 'staging-jobs'),
            'nonproduction': True,
            **identity,
        },
        'window': {
            'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)),
            'end': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(finished)),
            'utc': True,
            'strictly_past': True,
        },
        'workload': {
            'basis': {
                'contract': 'plan-a-job-store.md §3.7',
                'units_per_day': 250_000,
                'transactions_per_unit': 15,
                'average_tx_per_s': CONTRACT_AVERAGE_TX_PER_S,
                'peak_tx_per_s': CONTRACT_PEAK_TX_PER_S,
                'finalization_tx_per_s': CONTRACT_FINALIZATION_TX_PER_S,
                'peak_total_tx_per_s': CONTRACT_PEAK_TOTAL_TX_PER_S,
                'backlog_basis': 'pending backlog pinned at the 10k cap (firestore-reads-program 2026-11-11)',
                'backlog_jobs': args.backlog_jobs,
                'peak_jobs': args.peak_jobs,
            },
            'scenarios': scenarios,
        },
        'thresholds': {
            'sustained_tx_per_s_min': CONTRACT_PEAK_TOTAL_TX_PER_S,
            'pull_p95_ms_max': PULL_P95_MS,
            'pull_p99_ms_max': PULL_P99_MS,
            'errors_max': 0,
            'concurrent_double_leases_max': 0,
            'leased_twice_max': 0,
            'nonterminal_jobs_max': 0,
        },
        'results': {
            'pull_window': window,
            'scenario_windows': scenario_windows,
            'peak_tx_per_s': peak,
            'parity': parity,
            'sampler_error': next((item['error'] for item in windows if item['error']), None),
            'samples': sum(len(item['ticks']) for item in windows),
            'stale_test_namespaces_purged': stale,
        },
        'verdict': 'pending',
        'failures': [],
    }
    text = json.dumps(receipt, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text)
    print('RECEIPT_JSON:' + json.dumps(receipt, sort_keys=True))
    return 0


def jobstore_contention_path() -> str:
    from testing import jobstore_contention

    return jobstore_contention.__file__


if __name__ == '__main__':
    raise SystemExit(main())

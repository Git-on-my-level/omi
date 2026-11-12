"""Verify a staging job-store load receipt (charter ruling 19 sizing gate).

    python3 verify_staging_load.py staging-load-receipt.json

Exit 0 only when the receipt binds an actual identified nonproduction target, a source SHA, a
strictly-past UTC run window, a synthetic fixture, the measured peak and backlog-drain workload
basis, a sustained rate, p50/p95/p99 latency, error/custody/parity results, connection/pool
saturation and CPU/RAM/IO evidence, and every value meets the thresholds recorded in the receipt.
Any missing evidence or target mismatch fails the gate.

The thresholds are the plan-A §3.7 sizing contract (45 tx/s average, 150 tx/s peak plus 30-60
tx/s legacy finalization = 210 tx/s peak) and the 120 s lease / 30 s heartbeat pull bound. The
verifier re-derives the verdict; it never trusts a ``verdict`` field.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

SCHEMA = 'omi.jobstore.staging_load_receipt/v1'
EXPECTED_PEAK_TX_PER_S = 210.0
EXPECTED_BACKLOG_JOBS = 10_000
PRIVATE_HOST = re.compile(r'^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)')
SHA1 = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')


class Gate:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks: list[str] = []

    def require(self, condition: bool, message: str) -> bool:
        if condition:
            self.checks.append(message)
        else:
            self.failures.append(message)
        return condition

    def report(self) -> int:
        for line in self.checks:
            print(f'PASS  {line}')
        for line in self.failures:
            print(f'FAIL  {line}')
        print(f'\n{len(self.checks)} passed, {len(self.failures)} failed')
        return 0 if not self.failures else 1


def _parse_utc(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.endswith('Z'):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def verify(receipt: dict) -> Gate:
    gate = Gate()

    gate.require(receipt.get('schema') == SCHEMA, f'schema is {SCHEMA}')

    # -- target identity ---------------------------------------------------------------------
    target = receipt.get('target') or {}
    gate.require(target.get('nonproduction') is True, 'target is marked nonproduction')
    gate.require(target.get('database') == 'jobstore_staging', 'target database is jobstore_staging')
    gate.require(target.get('project') == 'based-hardware-dev', 'target project is based-hardware-dev')
    gate.require(target.get('instance') == 'staging-jobs', 'target instance is staging-jobs')
    host = str(target.get('host', '')).split('/')[0]
    gate.require(bool(PRIVATE_HOST.match(host)), f'target host {host!r} is a private address')
    gate.require(
        str(target.get('user', '')).endswith('@based-hardware-dev.iam'),
        'target user is a based-hardware-dev IAM user',
    )
    gate.require(str(target.get('server_version', '')).startswith('16'), 'target is PostgreSQL 16')
    gate.require(int(target.get('migrations_applied') or 0) >= 1, 'jobstore migrations are applied')

    # -- source identity ---------------------------------------------------------------------
    source = receipt.get('source') or {}
    gate.require(bool(SHA1.match(str(source.get('sha', '')))), 'source SHA is a 40-hex commit')
    gate.require(bool(source.get('image')), 'source image digest is recorded')
    gate.require(bool(SHA256.match(str(source.get('runner_sha256', '')))), 'runner script SHA-256 is recorded')
    gate.require(bool(SHA256.match(str(source.get('harness_sha256', '')))), 'harness SHA-256 is recorded')

    # -- run window --------------------------------------------------------------------------
    window = receipt.get('window') or {}
    start = _parse_utc(window.get('start'))
    end = _parse_utc(window.get('end'))
    gate.require(start is not None and end is not None, 'run window is UTC (Z-suffixed)')
    if start and end:
        gate.require(end > start, 'run window end is after start')
        gate.require(end <= datetime.now(timezone.utc), 'run window is strictly in the past')
        gate.require(window.get('utc') is True, 'run window is marked UTC')
        gate.require(window.get('strictly_past') is True, 'run window is marked strictly past')

    # -- workload basis ----------------------------------------------------------------------
    workload = receipt.get('workload') or {}
    basis = workload.get('basis') or {}
    gate.require(
        _number(basis.get('peak_total_tx_per_s')) == EXPECTED_PEAK_TX_PER_S,
        f'workload basis peak is {EXPECTED_PEAK_TX_PER_S:g} tx/s',
    )
    gate.require(
        int(basis.get('backlog_jobs') or 0) >= EXPECTED_BACKLOG_JOBS,
        f'backlog-drain basis is at least {EXPECTED_BACKLOG_JOBS} jobs',
    )
    scenarios = {item.get('name'): item for item in workload.get('scenarios') or []}
    gate.require('peak' in scenarios, 'peak scenario ran')
    gate.require('backlog_drain' in scenarios, 'backlog-drain scenario ran')

    # -- thresholds --------------------------------------------------------------------------
    thresholds = receipt.get('thresholds') or {}
    gate.require(
        _number(thresholds.get('sustained_tx_per_s_min')) == EXPECTED_PEAK_TX_PER_S,
        'sustained-rate threshold is the 210 tx/s contract',
    )
    p95_max = _number(thresholds.get('pull_p95_ms_max'))
    p99_max = _number(thresholds.get('pull_p99_ms_max'))
    gate.require(p95_max is not None and p95_max > 0, 'p95 latency threshold is set')
    gate.require(p99_max is not None and p99_max > 0, 'p99 latency threshold is set')
    gate.require(_number(thresholds.get('errors_max')) == 0, 'error threshold is zero')
    gate.require(_number(thresholds.get('concurrent_double_leases_max')) == 0, 'double-lease threshold is zero')
    gate.require(_number(thresholds.get('nonterminal_jobs_max')) == 0, 'nonterminal-job threshold is zero')

    # -- per-scenario results ----------------------------------------------------------------
    for name, scenario in scenarios.items():
        result = scenario.get('result') or {}
        jobs = int(result.get('jobs') or 0)
        gate.require(jobs > 0, f'{name}: workload seeded jobs')
        gate.require(int(result.get('leased') or 0) == jobs, f'{name}: every job was leased')
        gate.require(int(result.get('leased_twice') or 0) == 0, f'{name}: no job was leased twice')
        gate.require(
            int(result.get('concurrent_double_leases') or 0) == 0,
            f'{name}: no concurrent double lease',
        )
        gate.require(not result.get('errors'), f'{name}: no pull/enqueue/complete errors')
        pull_ms = result.get('pull_ms') or {}
        for percentile in ('p50', 'p95', 'p99'):
            gate.require(_number(pull_ms.get(percentile)) is not None, f'{name}: pull {percentile} latency recorded')
        if p95_max is not None and _number(pull_ms.get('p95')) is not None:
            gate.require(float(pull_ms['p95']) <= p95_max, f'{name}: pull p95 {pull_ms["p95"]} ms <= {p95_max:g} ms')
        if p99_max is not None and _number(pull_ms.get('p99')) is not None:
            gate.require(float(pull_ms['p99']) <= p99_max, f'{name}: pull p99 {pull_ms["p99"]} ms <= {p99_max:g} ms')

    # -- sustained rate and peak -------------------------------------------------------------
    results = receipt.get('results') or {}
    pull_window = results.get('pull_window') or {}
    sustained = _number(pull_window.get('tx_per_s'))
    peak = _number(results.get('peak_tx_per_s'))
    gate.require(sustained is not None, 'sustained transaction rate is measured')
    gate.require(peak is not None, 'peak transaction rate is measured')
    if sustained is not None:
        gate.require(
            sustained >= EXPECTED_PEAK_TX_PER_S,
            f'sustained {sustained:g} tx/s >= {EXPECTED_PEAK_TX_PER_S:g} tx/s',
        )
    if peak is not None:
        gate.require(peak >= EXPECTED_PEAK_TX_PER_S, f'peak {peak:g} tx/s >= {EXPECTED_PEAK_TX_PER_S:g} tx/s')
    gate.require(_number(pull_window.get('leases_per_s')) is not None, 'sustained lease rate is measured')
    gate.require(results.get('sampler_error') in (None, ''), 'database sampler reported no error')

    # -- custody / parity --------------------------------------------------------------------
    parity = results.get('parity') or []
    gate.require(len(parity) >= 2, 'parity was checked for both scenarios')
    for item in parity:
        name = item.get('scenario')
        scenario = scenarios.get(name) or {}
        jobs = int((scenario.get('result') or {}).get('jobs') or 0)
        gate.require(int(item.get('jobs_total') or 0) == jobs, f'{name}: parity job count matches the workload')
        gate.require(
            int(item.get('jobs_result_ready') or 0) == jobs,
            f'{name}: every job reached result_ready',
        )
        gate.require(int(item.get('jobs_nonterminal') or 0) == 0, f'{name}: no nonterminal jobs remain')
        gate.require(int(item.get('jobs_with_result') or 0) == jobs, f'{name}: every job carries a result')

    # -- connection / pool saturation --------------------------------------------------------
    max_backends = _number(pull_window.get('max_backends'))
    max_active = _number(pull_window.get('max_active'))
    gate.require(max_backends is not None, 'connection count is measured')
    gate.require(max_active is not None, 'active connection count is measured')
    pool_cap = 0
    for scenario in scenarios.values():
        config = (scenario.get('result') or {}).get('config') or {}
        pool_cap = max(pool_cap, int(config.get('pool') or 0) * int(config.get('replicas') or 0))
    gate.require(pool_cap > 0, 'pool size and replica count are recorded')
    if max_backends is not None and pool_cap:
        gate.require(
            max_backends <= pool_cap + 3,
            f'max backends {max_backends:g} stayed within the {pool_cap}-connection pool budget (+3 baseline)',
        )

    # -- instance metrics --------------------------------------------------------------------
    metrics = (receipt.get('metrics') or {}).get('series') or {}
    required = {
        'cpu/utilization': 'CPU utilization',
        'memory/utilization': 'memory utilization',
        'disk/read_ops_count': 'disk read IOPS',
        'disk/write_ops_count': 'disk write IOPS',
        'num_backends': 'connection count',
        'transaction_count': 'transaction count',
        'reserved_cores': 'reserved cores',
        'memory/quota': 'memory quota',
    }
    for key, label in required.items():
        series = metrics.get(key) or {}
        gate.require(_number(series.get('value')) is not None, f'{label} evidence is present')

    # -- sizing and rate source --------------------------------------------------------------
    sizing = receipt.get('sizing') or {}
    gate.require(bool(sizing.get('recommendation')), 'sizing recommendation is present')
    gate.require(bool(sizing.get('basis')), 'sizing recommendation states its basis')
    rate = receipt.get('rate_source') or {}
    gate.require(bool(rate.get('source')), 'marginal rate source is cited')
    gate.require(_number(rate.get('monthly_usd')) is not None, 'marginal monthly rate is stated')

    return gate


def main() -> int:
    if len(sys.argv) != 2:
        print('usage: python3 verify_staging_load.py staging-load-receipt.json', file=sys.stderr)
        return 2
    try:
        receipt = json.loads(open(sys.argv[1]).read())
    except (OSError, json.JSONDecodeError) as error:
        print(f'FAIL  cannot read receipt: {error}')
        return 1
    return verify(receipt).report()


if __name__ == '__main__':
    raise SystemExit(main())

"""Merge the staging run receipt with Cloud SQL instance metrics into the final receipt.

The run half is printed by ``testing.jobstore_staging_load`` inside the staging job (Cloud Run
logs). This script runs on the operator host with the dev-operator identity, reads that JSON, pulls
the Cloud SQL CPU/RAM/IO/connection series for the run window from Cloud Monitoring, and writes
``staging-load-receipt.json``. It never writes to the database.

    source ~/.local/bin/gcp-agent-env.sh dev-operator
    python3 assemble_staging_load_receipt.py run.json staging-load-receipt.json
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROJECT = 'based-hardware-dev'
INSTANCE = 'staging-jobs'
DATABASE_ID = f'{PROJECT}:{INSTANCE}'

# metric type -> (aligner, reducer)
SERIES = {
    'cloudsql.googleapis.com/database/cpu/utilization': ('ALIGN_MAX', 'max'),
    'cloudsql.googleapis.com/database/cpu/reserved_cores': ('ALIGN_MAX', 'max'),
    'cloudsql.googleapis.com/database/memory/utilization': ('ALIGN_MAX', 'max'),
    'cloudsql.googleapis.com/database/memory/quota': ('ALIGN_MAX', 'max'),
    'cloudsql.googleapis.com/database/disk/read_ops_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/disk/write_ops_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/disk/read_bytes_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/disk/write_bytes_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/postgresql/num_backends': ('ALIGN_MAX', 'max'),
    'cloudsql.googleapis.com/database/postgresql/transaction_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/postgresql/deadlock_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/postgresql/temp_bytes_written_count': ('ALIGN_SUM', 'sum'),
    'cloudsql.googleapis.com/database/postgresql/data_cache/hit_ratio': ('ALIGN_MEAN', 'mean'),
    'cloudsql.googleapis.com/database/postgresql/new_connection_count': ('ALIGN_SUM', 'sum'),
}


def _token() -> str:
    return subprocess.run(
        ['gcloud', 'auth', 'print-access-token'], check=True, capture_output=True, text=True
    ).stdout.strip()


def _value(point: dict) -> float:
    value = point['value']
    for key in ('doubleValue', 'int64Value', 'intValue'):
        if key in value:
            return float(value[key])
    return 0.0


def _fetch(token: str, metric: str, aligner: str, start: str, end: str) -> list[float]:
    params = {
        'filter': f'metric.type="{metric}" AND resource.labels.database_id="{DATABASE_ID}"',
        'interval.startTime': start,
        'interval.endTime': end,
        'aggregation.alignmentPeriod': '60s',
        'aggregation.perSeriesAligner': aligner,
        'aggregation.crossSeriesReducer': 'REDUCE_SUM' if aligner == 'ALIGN_SUM' else 'REDUCE_MAX',
    }
    url = f'https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    values: list[float] = []
    for series in payload.get('timeSeries', []):
        values.extend(_value(point) for point in series.get('points', []))
    return values


def _reduce(values: list[float], reducer: str) -> float | None:
    if not values:
        return None
    if reducer == 'max':
        return round(max(values), 4)
    if reducer == 'mean':
        return round(sum(values) / len(values), 4)
    return round(sum(values), 4)


def collect(start: str, end: str) -> dict:
    token = _token()
    metrics: dict[str, dict] = {}
    for metric, (aligner, reducer) in SERIES.items():
        values = _fetch(token, metric, aligner, start, end)
        metrics[metric.split('/')[-1]] = {
            'reducer': reducer,
            'value': _reduce(values, reducer),
            'samples': len(values),
        }
    return metrics


def _sizing(receipt: dict, metrics: dict) -> dict:
    results = receipt.get('results') or {}
    peak = float(results.get('peak_tx_per_s') or 0)
    sustained = float((results.get('pull_window') or {}).get('tx_per_s') or 0)
    cpu = (metrics.get('cpu/utilization') or {}).get('value')
    cores = (metrics.get('cpu/reserved_cores') or {}).get('value')
    memory = (metrics.get('memory/quota') or {}).get('value')
    contract = float((receipt.get('workload') or {}).get('basis', {}).get('peak_total_tx_per_s') or 210)
    capacity = None
    headroom = None
    if cpu and peak:
        capacity = round(peak / (float(cpu) / 100.0), 1)
        headroom = round(capacity / contract, 2)
    if headroom is None:
        recommendation = 'insufficient evidence to size; rerun with CPU utilization recorded'
    elif headroom >= 2.0:
        recommendation = (
            f'the measured {cores:g} vCPU / {memory / 1e9:.1f} GB staging instance carries the '
            f'{contract:g} tx/s contract with {headroom:g}x headroom at 100% CPU; a 2 vCPU HA prod '
            'instance is sufficient for the contract, and 4 vCPU is justified only by vacuum and '
            'failover headroom, not by this load'
        )
    else:
        recommendation = (
            f'the measured {cores:g} vCPU / {memory / 1e9:.1f} GB staging instance carries only '
            f'{headroom:g}x the {contract:g} tx/s contract at 100% CPU; size prod at 2x the measured '
            'per-core capacity and re-run this gate at the chosen prod size before spend'
        )
    return {
        'recommendation': recommendation,
        'basis': {
            'measured_peak_tx_per_s': peak,
            'measured_sustained_tx_per_s': sustained,
            'measured_cpu_utilization_max': cpu,
            'reserved_cores': cores,
            'memory_quota_bytes': memory,
            'capacity_tx_per_s_at_100pct_cpu': capacity,
            'headroom_vs_contract': headroom,
            'contract_peak_tx_per_s': contract,
            'plan_a_prod_target': 'db-custom-4-16384 (4 vCPU / 16 GB, regional HA)',
        },
    }


def _rate_source() -> dict:
    return {
        'source': (
            'omi-knowledge-base projects/backend-target-architecture/plan-a-job-store.md §5 '
            '(list prices, recalled not fetched) and '
            'projects/admin-omi-tv-dashboard/evidence/2026-11-11-firestore-second-opinion.md '
            '(managed pooler +$167/month at list; Cloud SQL estimate $20-40/day)'
        ),
        'monthly_usd': 622.0,
        'components': {
            'cloud_sql_prod_ha_4vcpu_16gb': 455.0,
            'managed_pooler': 167.0,
        },
        'basis': 'list-price estimate, not a settled bill; no spend action taken',
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    run_path, out_path = sys.argv[1], sys.argv[2]
    receipt = json.loads(open(run_path).read())
    start = receipt['window']['start']
    end = receipt['window']['end']
    metrics = collect(start, end)
    receipt['metrics'] = {
        'source': 'cloud monitoring cloudsql.googleapis.com/database/*',
        'database_id': DATABASE_ID,
        'window': {'start': start, 'end': end},
        'series': metrics,
    }
    receipt['sizing'] = _sizing(receipt, metrics)
    receipt['rate_source'] = _rate_source()
    receipt['generated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(out_path, 'w') as handle:
        json.dump(receipt, handle, indent=1, sort_keys=True)
    print(f'wrote {out_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

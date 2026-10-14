"""The prepared batch cutover must admit only David, without record/WAL writes."""

from pathlib import Path

import pytest
import yaml
from config import transcription_final_pass as flags

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('host', ['backend', 'backend-sync', 'pusher'])
def test_prepared_cohort_is_david_only_and_shadow_only(monkeypatch, host):
    manifest = yaml.safe_load((ROOT / 'backend/deploy/runtime_env.yaml').read_text())['environments']['prod']
    binding = manifest['gke']['pusher']['env'] if host == 'pusher' else manifest['cloud_run']['services'][host]['env']
    keys = (
        'HOSTED_DIARIZATION_API_URL',
        'TRANSCRIPTION_FINAL_PASS_DIARIZER',
        'TRANSCRIPTION_FINAL_PASS_BACKEND',
        'TRANSCRIPTION_FINAL_PASS_UID_ALLOWLIST',
        'TRANSCRIPTION_FINAL_PASS_PERCENT',
        'TRANSCRIPTION_FINAL_PASS_AUTHORITATIVE',
        'TRANSCRIPTION_FINAL_PASS_SYNC_ENABLED',
    )
    selected = {key: binding[key]['value'] for key in keys}
    for key, value in selected.items():
        monkeypatch.setenv(key, value)
    uid = 'vi7SA9ckQCe4ccobWNxlbdcNdC23'
    assert selected['TRANSCRIPTION_FINAL_PASS_UID_ALLOWLIST'] == uid
    assert selected['TRANSCRIPTION_FINAL_PASS_PERCENT'] == '0'
    assert flags.enabled_for(uid)
    assert not flags.enabled_for('synthetic-outside-cohort')
    assert not flags.authoritative_for(uid) and not flags.sync_enabled_for(uid)
    assert selected['TRANSCRIPTION_FINAL_PASS_BACKEND'] == 'pyannote'
    assert selected['HOSTED_DIARIZATION_API_URL'] == 'http://diarizer-batch.omi.me:80'
    assert binding['TRANSCRIPTION_SHADOW_UID_ALLOWLIST']['value'] == uid
    assert binding['TRANSCRIPTION_SHADOW_PERCENT']['value'] == '0'
    assert binding['TRANSCRIPTION_SHADOW_DAILY_AUDIO_HOURS']['value'] == '1'
    if host == 'pusher':
        chart = yaml.safe_load((ROOT / 'backend/charts/pusher/prod_omi_pusher_values.yaml').read_text())
        chart_values = {entry['name']: entry.get('value') for entry in chart['env']}
        assert {key: chart_values[key] for key in keys} == selected


def test_prepared_cohort_does_not_enable_backfill_integration_or_dev():
    manifest = yaml.safe_load((ROOT / 'backend/deploy/runtime_env.yaml').read_text())['environments']
    for host in ('backend-sync-backfill', 'backend-integration'):
        env = manifest['prod']['cloud_run']['services'][host]['env']
        assert env.get('TRANSCRIPTION_FINAL_PASS_DIARIZER', {}).get('value', 'legacy') == 'legacy'
    for host in ('backend', 'backend-sync'):
        env = manifest['dev']['cloud_run']['services'][host]['env']
        assert env.get('TRANSCRIPTION_FINAL_PASS_DIARIZER', {}).get('value', 'legacy') == 'legacy'

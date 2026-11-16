# Canonical flag-key ledger and single-date design (PA-1116-01)

Source: `BasedHardware/omi` at `origin/main` = `92b697098eb6ff200165cf005577c916aeab189f`
(2026-11-16 11:42:36 +0700, `feat(app): transcribe on the phone for every free user past the cloud cap (#25438)`).
Analysis date: 2026-11-16. No product/runtime/workflow/`.github` change; a small analysis
generator is included so the source-key extraction and output bundle can be reproduced.

Artifacts in this bundle:

| file | contents |
| --- | --- |
| `flag-key-ledger.csv` | normalized full ledger, one row per key (1,048 keys) |
| `flag-key-ledger.json` | same rows keyed by flag name, with pinned SHA and `as_of` |
| `ledger-stats.json` | source counts and explicit issue/owner mapping-gap counts |
| `build_ledger.py` | source-key validator and deterministic bundle generator |

## 1. Universe and reproduction

The ledger universe is the union of every key named in the generated registry and every
name in the CI budget:

| source | keys |
| --- | --- |
| `backend/docs/feature-flag-registry.md` table rows and undeclared bullets | 963 |
| `backend/config/flag_budget.json` `names` | 314 |
| `config/feature-flags.yaml` `flags` (registry source) | 777 |
| `config/feature-flags.yaml` `ignore` | 50 |
| `config/feature-flags.yaml` `retired` | 136 |
| **union, deduplicated** | **1,048** |

`flag-key-ledger.csv` is the reviewed annotation manifest. The generator reads the
registry YAML and budget JSON from the pinned Git object, verifies that every YAML key
appears in the generated registry, verifies exact one-row-per-key coverage, marks absent
issue and accountable-owner evidence as explicit gaps, then emits CSV, JSON, and stats.
It does not infer a workspace issue from an owner token or treat a sweep/desk label as an
implementing lane. A source gap means no direct citation was found in the pinned files; it
does not assert that no related workspace issue exists.

Reproduce from a checkout that contains the pinned Git object:

```bash
python3 analysis/flag-key-ledger/build_ledger.py \
  --manifest analysis/flag-key-ledger/manifest.csv \
  --output-dir ./flag-ledger-output \
  --as-of 2026-11-16
```

The script reads `92b697098eb6ff200165cf005577c916aeab189f` with `git show`, independent
of the checkout's current `HEAD`. PyYAML is pinned in `backend/requirements.txt`.

Each key appears exactly once in the ledger. A key that appears in several registry
sections (for example a flag row plus its ramp-plan row plus an undeclared-env bullet)
carries all of them in `registry_sections`; it is not duplicated.

Ledger columns: `key`, `registry_sections`, `lifecycle`, `decision`, `terminal`,
`review_by`, `ramp_done_by`, `extension_until`, `due_kind`, `due_date`, `due_status`,
`budget_owner`, `budget_review_by`, `budget_status`, `budget_entry_kind`,
`registry_owner`, `owner_class`, `owner_class_basis`, `canonical_owner`,
`canonical_owner_basis`, `canonical_issue`, `mapping_basis`, `proposed_disposition`,
`existing_ruling`, `rollback`, `rollback_basis`, `lead2_owner_row`, `lead2_mentioned`,
`code_readers`, `evidence`.

`evidence` names the registry line (`backend/docs/feature-flag-registry.md:<line>`), the
budget entry, the registry source entry, and concrete code readers found by a literal
one-pass scan of `backend/**/*.py`, `app/lib/**/*.dart`, `app/**/*.swift`,
`web/src/**/*.{ts,tsx}`. `code_readers` retains all literal matches; the evidence string
shows a bounded excerpt.

Added provenance columns are `canonical_issue_status`, `canonical_issue_basis`,
`canonical_issue_sources`, `candidate_owner_label`, `owner_resolution_status`, and
`canonical_owner_gap`. They keep the source's candidate label visible while distinguishing
an accountable lane from an unassigned baseline or a work-stream-only owner.

### Findings at a glance

| class | keys |
| --- | --- |
| standing lane owner in source | 180 |
| obsolete lane (`lead2`, `cl2-*`, `unowned`) | 48 |
| sweep or role label (`auditfix`, `backlog-refactor`, `two-lane lead`, desks) | 567 |
| no owner (budget-seeded baseline / retired / ignore / unattributed) | 253 |
| accountable owner resolved in normalized ledger | 185 |
| explicit owner evidence gap in normalized ledger | 681 |
| owner not applicable (retired or ignored) | 182 |
| direct issue IDs mapped from source | 28 |
| no direct issue citation in pinned source | 1,020 |
| rows where the owner or ramp expander names `lead2` | 14 |
| rows mentioning `lead2` anywhere (history/ruling text) | 145 |

Disposition proposals: 483 keep, 162 decide by due date, 136 already retired, 85 decide,
35 graduate, 24 retire, 50 classified non-flag, 6 retire with legacy path, 21 relabel then
keep, 1 deadline reached.

## 2. Obsolete lane names confer no authority

`lead2` is the authoritative example in PA-1116-01: it retains an approval gate but is not
an activation authority and must not be read as the implementing owner. The same reasoning
applies to the other closed coordination lanes (`cl2-coordkit`, `cl2-cost`, `cl2-fixnow`,
`cl2-deploy`) and to `unowned`. `auditfix`, `backlog-refactor`, `two-lane lead` and the
desks remain visible in `candidate_owner_label` and are flagged
`owner_class=sweep-or-role`; they do not name an accountable implementing lane. The
normalized `canonical_owner` is `UNRESOLVED` for these rows. The 40 prior unresolved rows
and 74 budget-seeded rows with no assigned owner are also marked with a specific owner
gap. Retired and ignore rows are marked not applicable.

### Every row that names `lead2` in an authority position (14)

| key | where `lead2` appears | registry owner | canonical owner | disposition |
| --- | --- | --- | --- | --- |
| `BATCH_PIPELINE_PROJECTION_SOURCE_DELETED_ENABLED` | owner | `cl2-cost / lead2` | batch record lane | relabel, keep |
| `BATCH_PIPELINE_SERVING_PROJECTION_SWEEP` | owner | `lead2` | omi-pipeline | relabel, keep |
| `BATCH_PIPELINE_SERVING_SWEEP_PAGE_ROWS` | owner | `lead2` | omi-pipeline | relabel, keep |
| `BATCH_PIPELINE_SERVING_TICK_SECONDS` | owner | `lead2` | omi-pipeline | relabel, keep |
| `BATCH_PROJECTION_FINISH_BACKOFF_ENABLED` | ramp expander | `firestore-lead` | firestore-lead | decide by 2026-12-20; expander must be a named lane |
| `QUIET_EMPTY_EXIT_ENABLED` | ramp expander | `batch-pipeline (dazheng)` | batch-pipeline (dazheng) | decide; rename expander |
| `RECORD_GAP_FILL_PERCENT` | ramp expander | `auditfix` | UNRESOLVED | decide; rename expander |
| `RECORD_RETAINED_ACK_PERCENT` | ramp expander | `omi-pipeline` | omi-pipeline | decide; rename expander |
| `RECORD_TOKEN_LEASE_MODE` | ramp expander | `record-token-lease` | record-token-lease | decide; rename expander |
| `RECORD_TOKEN_LEASE_MODE_SERVE_PERCENT` | ramp expander | `record-token-lease` | record-token-lease | decide; rename expander |
| `RECORD_TOKEN_LEASE_MODE_SERVE_UID_SHA256S` | ramp expander | `record-token-lease` | record-token-lease | decide; rename expander |
| `DREAM_NO_PROGRESS_STOP_MODE` | retired reason | — | n/a (retired) | already retired |
| `DREAM_NO_PROGRESS_STOP_MODE_SERVE_PERCENT` | retired reason | — | n/a (retired) | already retired |
| `MEETING_NOTES_EPISODE_CLAIMS_ENABLED` | retired reason | — | n/a (retired) | already retired |

`OMI_FLAGS_SOURCE` is a fifteenth row: its summary records `lead2 95e1f1`, but its owner
is `cl2-coordkit` (also closed). It has no canonical lane in either ledger and is
`UNRESOLVED`; the summary is historical evidence, not authority.

## 3. Registry/budget date divergence

Both ledgers carry dates, and they disagree. This is the concrete defect behind the
"single-date" requirement, and it is not only `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED`.

### 3.1 The named mismatch

`SYNC_FUTURE_CAPTURE_CLAMP_ENABLED`:

- registry: `review_by: 2026-11-15`, ramp `done_by: 2026-11-15`
- budget: `review_by: 2026-11-22`, with `reason: "2026-11-16 renewed: review_by expiry
  failed the flag-budget guard for every branch; enabling waits on the product decision
  how recovered future-dated recordings appear on Home; quality lead keeps or deletes by
  2026-11-22"`
- commit `9b8d09156b` (2026-11-16) renewed only `backend/config/flag_budget.json` and the
  budget test expectation; `config/feature-flags.yaml` and the generated registry were not
  updated.

Mechanism, stated without asserting a future CI failure count: the registry checker
computes `effective_due = max(review_by, extension.until)` and treats `due + 7 days` as the
grace break. With `due = 2026-11-15`, the break is `2026-11-23`. As of 2026-11-16 the row
is inside the grace window (`FLAG_OVERDUE_COUNT n=1`), and the budget guard is satisfied by
the renewed date. The two ledgers now hold different dates for one key.

### 3.2 All 12 keys where both ledgers carry a date and the dates differ

| key | registry `review_by` | registry extension | budget `review_by` | proposed single date |
| --- | --- | --- | --- | --- |
| `BATCH_ALLOWANCE_HOLD_RETRY_SECONDS` | 2026-11-22 | — | 2026-12-14 | 2026-12-14 |
| `BATCH_PIPELINE_GUARDED_LONG_RECOVERY_MAX_SECONDS` | 2026-11-22 | — | 2026-11-30 | 2026-11-30 |
| `BATCH_PIPELINE_MAX_OWNER_DRAINS` | 2026-11-22 | — | 2026-12-05 | 2026-12-05 |
| `BATCH_PIPELINE_PROJECTION_PUMP_ENABLED` | 2026-11-22 | — | 2026-12-05 | 2026-12-05 |
| `BATCH_PIPELINE_PUMP_SWEEP_PAGES` | 2026-11-22 | — | 2026-12-05 | 2026-12-05 |
| `BATCH_PIPELINE_PUMP_TICK_SECONDS` | 2026-11-22 | — | 2026-12-05 | 2026-12-05 |
| `BATCH_PIPELINE_RECORD_PROTOCOL_READY` | 2026-11-15 | 2026-11-22 | 2026-12-14 | 2026-12-14 |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED` | 2026-11-16 | — | 2026-12-15 | 2026-12-15 |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED_SERVE_PERCENT` | 2026-11-16 | — | 2026-12-15 | 2026-12-15 |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED_SERVE_UID_SHA256S` | 2026-11-16 | — | 2026-12-15 | 2026-12-15 |
| `SERVER_BATCH_UNSUPPORTED_LANGUAGES` | 2026-11-22 | — | 2026-11-30 | 2026-11-30 |
| `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED` | 2026-11-15 | — | 2026-11-22 | **David decision required** |

121 further keys carry a budget `review_by` but no registry date. That is structural, not a
bug: the registry forbids `review_by` on `ops_kill` and `config_switch` rows, while the
budget requires a `review_by` on every above-budget exception. The single-date design has
to reconcile the two semantics explicitly.

### 3.3 Expired and overdue as of 2026-11-16

No registry row is beyond the 7-day grace and no budget entry is expired on this date.

Inside the grace window (registry):

| key | due | breaks | owner | decision |
| --- | --- | --- | --- | --- |
| `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED` | 2026-11-15 | 2026-11-23 | Q12-AD / sync capture recovery | pending |

Due today (registry), all within grace until 2026-11-23:

| key | due | owner | decision |
| --- | --- | --- | --- |
| `AUDIO_CUSTODY_FRAMED_PERCENT` | 2026-11-16 | auditfix | pending |
| `OMI_INDEXED_LIST_READS_ENABLED` | 2026-11-16 | auditfix | graduate |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED` | 2026-11-16 | quality-12h/Q12-A | pending |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED_SERVE_PERCENT` | 2026-11-16 | quality-12h/Q12-A | pending |
| `PUSHER_RECORD_CLOSE_NOTIFY_ENABLED_SERVE_UID_SHA256S` | 2026-11-16 | quality-12h/Q12-A | pending |
| `QUIET_EMPTY_EXIT_ENABLED` | 2026-11-16 | batch-pipeline (dazheng) | pending |

Budget entries due within seven days (the budget has no grace; it fails when
`review_by < today`):

| key | review_by | budget owner |
| --- | --- | --- |
| `SYNC_EARLY_CAPTURE_CLAMP_ENABLED` | 2026-11-17 | earlyclamp / two-lane transcription |
| `BATCH_HELD_POLL_SKIP_ENABLED` | 2026-11-18 | firestore-lead |
| `EPISODE_EMPTY_MEMBER_CLEANUP_ENABLED` | 2026-11-22 | quality-lead |
| `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED` | 2026-11-22 | Q12-AD / sync capture recovery |
| `BATCH_PIPELINE_PROTECTED_SHARE_ENABLED` | 2026-11-23 | Pipeline / SCA-1894 |
| `BATCH_PIPELINE_PROTECTED_SHARE_PERCENT` | 2026-11-23 | Pipeline / SCA-1894 |
| `SYNC_BACKFILL_WORKER_API_URL` | 2026-11-23 | infracost / syncsplit |
| `SYNC_BACKFILL_WORKER_AUDIENCE` | 2026-11-23 | infracost / syncsplit |

Additional date debt already reported by the registry checker on this date, recorded here
as counts only because it is not expiry: 113 dark default-off rows older than 14 days, 50
grandfathered pending/undecided rollout rows without a ramp, and 33 unramped `auditfix`
rows at the owner cap.

## 4. Single-date ledger design

### 4.1 Principle

One key, one owner, one date, one disposition, one rollback statement. The date is stored
once; every validator reads it from that record instead of carrying its own copy.

The registry keeps its richer ramp structure (`ramp.cohorts`, `ramp.done_by`,
`extension.until`) because the ramp plan is a different object from the review deadline.
The ledger stores the **effective** date the validators already compute:

```
due = max(review_by, extension.until, ramp.done_by when the ramp applies)
```

`due_source` names the single field that produced it. A key may have exactly one
`due_source`. If two sources carry different dates, the ledger build fails.

### 4.2 Ledger record

```json
{
  "schema": 1,
  "as_of": "2026-11-16",
  "keys": {
    "SYNC_FUTURE_CAPTURE_CLAMP_ENABLED": {
      "owner": "Q12-AD / sync capture recovery",
      "owner_class": "lane",
      "due": "2026-11-22",
      "due_source": "review_by",
      "lifecycle": "rollout",
      "decision": "pending",
      "terminal": "on",
      "disposition": "decide",
      "ruling": "decision=pending; terminal=on; product decision on future-dated recordings on Home",
      "rollback": "false restores typed invalid_capture_time on new admissions"
    }
  }
}
```

Rules enforced by the ledger test:

1. every key in the registry or budget has exactly one record;
2. `owner` is a standing lane token, never `lead2`, `cl2-*`, `unowned`, or an account;
3. `due` is the maximum of the declared dates and equals `due_source`'s field;
4. `due_source` is exactly one of `review_by`, `extension.until`, `ramp.done_by`,
   `budget.review_by`, `seeded` (no date);
5. `ops_kill`/`config_switch` records may carry a budget date but must not invent a
   registry `review_by`;
6. `disposition` is one of retire, graduate, keep, decide, relabel, retire-with-legacy;
7. a `decision=kill|kill_after_readout` record must name a rollback that preserves the
   current default until the code path is deleted.

### 4.3 Migration for the 12 divergent keys

1. Pick the later date (table 3.2), except `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED`, which
   needs David's ruling.
2. For keys whose later date came from a budget renewal, express the change in
   `config/feature-flags.yaml` with the existing `extension` object (`until`, `reason`,
   `by`, `granted`) rather than moving `review_by` directly; the checker already refuses a
   later date without an extension.
3. Regenerate `backend/docs/feature-flag-registry.md` with
   `python3 scripts/render_feature_flag_registry.py`.
4. Rebuild the ledger; the build must report zero `due_conflict`.

### 4.4 Proposed validator change (concrete diff, not applied)

`backend/tests/unit/test_flag_budget.py`:

```diff
@@
 def problems(read: dict[str, list[str]], data: dict, today: datetime.date) -> list[str]:
     names: dict[str, dict] = data['names']
     retired: dict[str, dict] = data.get('retired', {})
     out = unlisted(read, names)
+    ledger = json.loads((BACKEND_DIR / 'config' / 'flag_ledger.json').read_text())['keys']
@@
         else:
             owner = str(entry.get('owner', '')).strip()
-            try:
-                review_by = datetime.date.fromisoformat(entry.get('review_by', ''))
-            except (TypeError, ValueError):
-                review_by = None
+            # Single date: the budget entry never carries its own date.
+            record = ledger.get(name, {})
+            owner = owner or str(record.get('owner', '')).strip()
+            try:
+                review_by = datetime.date.fromisoformat(record.get('due', ''))
+            except (TypeError, ValueError):
+                review_by = None
+            if 'review_by' in entry:
+                out.append(f'{name} carries a second date; move it to config/flag_ledger.json')
             if not owner or review_by is None:
                 out.append(f'{name} is above the budget and needs an "owner" and a "review_by" date')
             elif review_by < today:
                 out.append(f'{name} passed review_by {review_by} ({owner}): renew it with a reason or delete the flag')
```

Budget entry shape after migration:

```diff
     "SYNC_FUTURE_CAPTURE_CLAMP_ENABLED": {
       "owner": "Q12-AD / sync capture recovery",
-      "review_by": "2026-11-22",
       "reason": "2026-11-16 renewed: ..."
     },
```

`scripts/check_feature_flag_registry.py`:

```diff
@@
-        for kind, due in due_dates(entry):
+        ledger_record = LEDGER['keys'].get(key, {})
+        if ledger_record.get('due') != effective_due(entry).isoformat():
+            errors.append(
+                f"{key}: registry due {effective_due(entry)} disagrees with "
+                f"config/flag_ledger.json due {ledger_record.get('due')}"
+            )
+        for kind, due in due_dates(entry):
```

`scripts/render_feature_flag_registry.py` gains `--ledger` (or a sibling
`scripts/render_flag_ledger.py`) that writes `backend/config/flag_ledger.json` from
`config/feature-flags.yaml` plus the budget. The ledger is a generated artifact; a test
asserts it is current, the same way the registry render is checked today.

### 4.5 Changed-key-only expiry (proposal only, not recommended as-is)

Today an unchanged expired row fails every branch, which is the date-red failure SCA-2179
had to fix by hand. Changed-key-only expiry would fail only when the diff touches the key's
date, owner, decision, or disposition, and warn on unchanged expired rows. It reduces
unrelated-branch red, but it also lets debt rot silently, so the proposal keeps an absolute
halt line after which every branch fails again.

```diff
--- a/scripts/check_feature_flag_registry.py
+++ b/scripts/check_feature_flag_registry.py
@@
-            elif current_date >= breaks:
+            elif current_date >= breaks:
                 beyond_grace_rows.add(key)
+                if key not in changed_keys and current_date < breaks + timedelta(days=CHANGED_KEY_HALT_DAYS):
+                    warnings.append(
+                        f"WARNING: {kind} overdue beyond grace but unchanged in this diff: "
+                        f"{key} (due {due}, owner {owner}); fails after {breaks + timedelta(days=CHANGED_KEY_HALT_DAYS)}"
+                    )
+                    continue
                 if entry["decision"] in {"graduate", "ship", "kill", "kill_after_readout"} and (
```

with `CHANGED_KEY_HALT_DAYS = 30` and `changed_keys` computed from
`git diff --name-only` plus a YAML path diff. The budget test gets the same gate:
`problems(..., changed=changed_budget_keys)` skips unchanged expired entries for 30 days.

Failure modes this proposal must be tested against (tests below): an unchanged expired key
is not silently accepted forever; a changed expired key still fails; a key whose date moved
later without an extension still fails; an expired `kill` row with live code still fails.

## 5. Failure-mode tests (proposed, not committed)

```python
# backend/tests/unit/test_flag_ledger.py
import datetime, json
from pathlib import Path
import pytest

LEDGER = json.loads((Path(__file__).parents[2] / 'config' / 'flag_ledger.json').read_text())
REGISTRY = Path(__file__).parents[2] / 'docs' / 'feature-flag-registry.md'
BUDGET = json.loads((Path(__file__).parents[2] / 'config' / 'flag_budget.json').read_text())


def test_every_key_has_exactly_one_record():
    keys = LEDGER['keys']
    assert len(keys) == len(set(keys))


def test_registry_and_budget_keys_are_in_the_ledger():
    import re
    doc = REGISTRY.read_text()
    reg_keys = set(re.findall(r'^\|\s*`([^`]+)`', doc, flags=re.M))
    reg_keys |= set(re.findall(r'^- `([^`]+)`', doc, flags=re.M))
    missing = (reg_keys | set(BUDGET['names'])) - set(LEDGER['keys'])
    assert not missing, f'keys missing from config/flag_ledger.json: {sorted(missing)}'


def test_due_is_single_and_matches_source():
    for key, rec in LEDGER['keys'].items():
        assert rec['due_source'] in {'review_by', 'extension.until', 'ramp.done_by',
                                     'budget.review_by', 'seeded'}, key
        if rec['due_source'] == 'seeded':
            assert rec.get('due') in (None, ''), key
        else:
            datetime.date.fromisoformat(rec['due'])


def test_no_owner_is_an_obsolete_lane_or_account():
    forbidden = {'lead2', 'unowned', 'dazheng'}
    for key, rec in LEDGER['keys'].items():
        tokens = {t.strip().lower() for t in rec['owner'].replace('/', ',').split(',')}
        assert not (tokens & forbidden), f'{key}: {rec["owner"]}'


def test_ops_kill_does_not_gain_a_registry_date():
    for key, rec in LEDGER['keys'].items():
        if rec['lifecycle'] in {'ops_kill', 'config_switch'}:
            assert rec['due_source'] != 'review_by', key


@pytest.mark.parametrize('key', sorted(LEDGER['keys']))
def test_kill_decision_names_a_rollback(key):
    rec = LEDGER['keys'][key]
    if rec['decision'] in {'kill', 'kill_after_readout'}:
        assert rec.get('rollback'), key
```

```python
# backend/tests/unit/test_flag_expiry_changed_keys.py  (proposal only)
import datetime
from scripts.check_feature_flag_registry import expiry_findings

TODAY = datetime.date(2026, 12, 1)


def test_unchanged_expired_key_warns_but_does_not_fail():
    errors, warnings = expiry_findings(
        {'STALE': {'lifecycle': 'rollout', 'decision': 'pending', 'owner': 'lane',
                   'review_by': '2026-11-01', 'fail': 'closed', 'created': '2026-10-01'}},
        today=TODAY, changed=set(), present=set(), halt_days=30)
    assert not errors
    assert any('STALE' in w for w in warnings)


def test_unchanged_expired_key_fails_after_halt_line():
    errors, _ = expiry_findings(
        {'STALE': {'lifecycle': 'rollout', 'decision': 'pending', 'owner': 'lane',
                   'review_by': '2026-10-01', 'fail': 'closed', 'created': '2026-09-01'}},
        today=TODAY, changed=set(), present=set(), halt_days=30)
    assert any('STALE' in e for e in errors)


def test_changed_expired_key_fails_immediately():
    errors, _ = expiry_findings(
        {'STALE': {'lifecycle': 'rollout', 'decision': 'pending', 'owner': 'lane',
                   'review_by': '2026-11-01', 'fail': 'closed', 'created': '2026-10-01'}},
        today=TODAY, changed={'STALE'}, present=set(), halt_days=30)
    assert any('STALE' in e for e in errors)


def test_moved_date_without_extension_still_fails():
    errors = []
    check_diff_rules(
        {'flags': [{'key': 'K', 'review_by': '2026-11-01'}]},
        {'flags': [{'key': 'K', 'review_by': '2026-12-01'}]},
        errors)
    assert any('moved later without an extension' in e for e in errors)


def test_kill_row_with_live_reader_still_fails():
    errors, _ = expiry_findings(
        {'KILLED': {'lifecycle': 'rollout', 'decision': 'kill', 'owner': 'lane',
                    'review_by': '2026-11-01', 'fail': 'closed', 'created': '2026-10-01'}},
        today=TODAY, changed=set(), present={'KILLED'}, halt_days=30)
    assert any('still reads it' in e for e in errors)
```

## 6. Where David's ruling is required

Each item comes with the concrete change above; none is a request to investigate further.

| # | ruling | concrete change | failure-mode tests |
| --- | --- | --- | --- |
| R1 | adopt the single-date ledger as CI authority | new `backend/config/flag_ledger.json`, diffs 4.4, regenerated registry | `test_flag_ledger.py` (5.1) |
| R2 | changed-key-only expiry with a 30-day halt line, or keep current all-branch expiry | diff 4.5 | `test_flag_expiry_changed_keys.py` (5.2) |
| R3 | name accountable owners for the 48 obsolete-lane rows (4 naming `lead2`, 5 naming a `cl2-*` lane, 40 naming `unowned`; one row carries both `cl2-cost` and `lead2`) | owner strings only; no code | `test_no_owner_is_an_obsolete_lane_or_account` |
| R4 | schedule deletion for the 24 `kill`/`kill_after_readout` decision rows | follow-up code PRs | `test_kill_decision_names_a_rollback` |
| R5 | resolve `SYNC_FUTURE_CAPTURE_CLAMP_ENABLED`: keep 2026-11-22 and add the registry extension, or revert the budget date and decide now | one extension object + regenerated registry + budget alignment | `test_due_is_single_and_matches_source` |
| R6 | set the product answer for how recovered future-dated recordings appear on Home (blocks R5's enablement) | product ruling; no code in this task | n/a |

## 7. Gaps and non-claims

- 681 keys have an explicit owner evidence gap: 567 source labels are work streams or
  desks, 40 remain unresolved after the prior fallbacks, and 74 budget-seeded rows have
  no assigned owner. The ledger records the source label, code readers where found, and
  the reason no accountable lane can be assigned without additional evidence. Four of
  the prior 40 unresolved rows have no code reader at the pinned SHA
  (`VITE_ENABLE_GMAIL_SESSION`, `desktop-rating-prompt-disabled`,
  `free-tier-kill-switch-v1`, `negative_feedback_remediation_kill`).
- 1,020 keys have no direct SCA issue identifier in the pinned registry, YAML, or budget.
  `canonical_issue_basis` records that evidence gap and explicitly does not claim that no
  related workspace issue exists. The 28 explicit source mappings remain listed.
- `code_readers` is a literal scan; it proves the name appears, not that the read is a live
  admission path. The registry row and the YAML notes are the authority for that.
- No SERVING line, production number, flag value, workflow, or `.github` file was read or
  changed at runtime. All evidence is repository text at the pinned SHA.

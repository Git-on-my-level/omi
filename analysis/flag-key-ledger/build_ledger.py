#!/usr/bin/env python3
"""Validate a flag ledger against the pinned source key universe and emit bundles.

The CSV manifest is the reviewed annotation input. This script independently
rebuilds the key universe from the registry YAML and budget JSON, checks the
manifest's one-row-per-key coverage, makes unsupported issue/owner assignments
explicit gaps, and writes deterministic CSV, JSON, and stats outputs.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import subprocess
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by CLI invocation
    raise SystemExit("PyYAML is required; install the backend requirements") from exc


PINNED_SHA = "92b697098eb6ff200165cf005577c916aeab189f"
ROOT = Path(__file__).resolve().parents[2]
REGISTRY_YAML = "config/feature-flags.yaml"
REGISTRY_MD = "backend/docs/feature-flag-registry.md"
BUDGET_JSON = "backend/config/flag_budget.json"
ISSUE_ID = re.compile(r"\bSCA-\d+\b")
NEW_COLUMNS = (
    "canonical_issue_status",
    "canonical_issue_basis",
    "canonical_issue_sources",
    "candidate_owner_label",
    "owner_resolution_status",
    "canonical_owner_gap",
)


def fail(message: str) -> None:
    raise SystemExit(message)


def git_show(sha: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{sha}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def issue_ids_in(value: object) -> set[str]:
    return set(ISSUE_ID.findall(json.dumps(value, ensure_ascii=False)))


def source_key_sets(
    sha: str,
) -> tuple[set[str], set[str], set[str], dict[str, dict[str, set[str]]]]:
    spec = yaml.safe_load(git_show(sha, REGISTRY_YAML))
    yaml_rows = [row for section in ("flags", "ignore", "retired") for row in spec[section]]
    yaml_keys = {str(row["key"]) for row in yaml_rows}
    budget = json.loads(git_show(sha, BUDGET_JSON))["names"]
    budget_keys = set(map(str, budget))
    issue_sources: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(set)
    )

    for row in yaml_rows:
        key = str(row["key"])
        if ids := issue_ids_in(row):
            issue_sources[key][REGISTRY_YAML].update(ids)
    for key, entry in budget.items():
        if ids := issue_ids_in(entry):
            issue_sources[str(key)][BUDGET_JSON].update(ids)

    rendered = git_show(sha, REGISTRY_MD)
    missing_from_registry = {key for key in yaml_keys if f"`{key}`" not in rendered}
    if missing_from_registry:
        fail(
            "generated registry is missing YAML keys: "
            + ", ".join(sorted(missing_from_registry)[:20])
        )
    source_keys = yaml_keys | budget_keys
    for line in rendered.splitlines():
        ids = issue_ids_in(line)
        if not ids:
            continue
        for token in re.findall(r"`([^`]+)`", line):
            if token in source_keys:
                issue_sources[token][REGISTRY_MD].update(ids)
    return yaml_keys, budget_keys, source_keys, issue_sources


def load_manifest(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "key" not in reader.fieldnames:
            fail("manifest CSV must have a 'key' column")
        columns = list(reader.fieldnames)
        rows = [{name: (value or "") for name, value in row.items()} for row in reader]
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        key = row["key"].strip()
        row["key"] = key
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        fail("duplicate ledger keys: " + ", ".join(sorted(set(duplicates))[:20]))
    return columns, rows


def normalize_rows(
    rows: list[dict[str, str]],
    columns: list[str],
    issue_sources: dict[str, dict[str, set[str]]],
) -> tuple[list[str], list[dict[str, str]]]:
    output_columns = list(columns)
    for column in NEW_COLUMNS:
        if column not in output_columns:
            output_columns.append(column)

    normalized: list[dict[str, str]] = []
    for source in rows:
        row = dict(source)
        key = row["key"]

        direct_sources = issue_sources.get(key, {})
        issue_ids = sorted({issue for ids in direct_sources.values() for issue in ids})
        annotated_ids = set(ISSUE_ID.findall(row.get("canonical_issue", "")))
        if annotated_ids - set(issue_ids):
            fail(
                f"{key}: annotated issue IDs {sorted(annotated_ids - set(issue_ids))} "
                "are not directly present in pinned per-key source evidence"
            )
        if issue_ids:
            row["canonical_issue"] = "; ".join(issue_ids)
            row["canonical_issue_status"] = "mapped"
            source_names = sorted(direct_sources)
            row["canonical_issue_sources"] = "; ".join(source_names)
            row["canonical_issue_basis"] = "Direct SCA issue citations found in: " + ", ".join(source_names)
        else:
            row["canonical_issue"] = ""
            row["canonical_issue_status"] = "source-gap"
            row["canonical_issue_sources"] = ""
            row["canonical_issue_basis"] = (
                "No direct SCA issue identifier appears in the per-key registry, YAML, "
                "or budget records at the pinned SHA. This does not assert that no "
                "related workspace issue exists."
            )

        candidate = row.get("candidate_owner_label", "").strip()
        if not candidate:
            current_owner = row.get("canonical_owner", "").strip()
            candidate = current_owner if current_owner and current_owner != "UNRESOLVED" else ""
        if not candidate:
            candidate = row.get("registry_owner", "").strip()
        row["candidate_owner_label"] = candidate

        owner_class = row.get("owner_class", "")
        prior_basis = row.get("canonical_owner_basis", "").strip()
        prior_owner = row.get("canonical_owner", "").strip()
        retired_or_ignore = (
            row.get("lifecycle") == "retired"
            or row.get("budget_entry_kind") == "ignore"
            or prior_owner.startswith("n/a (")
        )
        seeded_without_owner = "seeded baseline" in prior_owner.lower() or (
            "budget seeded entry; ownership never assigned" in prior_basis
        )

        if retired_or_ignore:
            row["owner_resolution_status"] = "not-applicable"
            row["canonical_owner_gap"] = ""
        elif owner_class == "sweep-or-role":
            row["canonical_owner"] = "UNRESOLVED"
            row["canonical_owner_basis"] = (
                f"Source owner label '{candidate}' is a sweep/role label, not an "
                "accountable implementation lane. No owner inferred."
            )
            row["owner_resolution_status"] = "source-gap"
            row["canonical_owner_gap"] = (
                "The source names a work stream or desk, not an accountable "
                "implementing lane; code-reader evidence does not establish ownership."
            )
        elif seeded_without_owner:
            row["canonical_owner"] = "UNRESOLVED"
            row["canonical_owner_basis"] = (
                "Budget baseline explicitly records that ownership was never assigned."
            )
            row["owner_resolution_status"] = "source-gap"
            row["canonical_owner_gap"] = (
                "The budget-seeded baseline has no assigned owner in the source."
            )
        elif prior_owner == "UNRESOLVED":
            row["owner_resolution_status"] = "source-gap"
            row["canonical_owner_gap"] = prior_basis or (
                "No accountable implementation lane is identified by the available evidence."
            )
        elif prior_owner:
            row["owner_resolution_status"] = "resolved"
            row["canonical_owner_gap"] = ""
        else:
            row["canonical_owner"] = "UNRESOLVED"
            row["canonical_owner_basis"] = "No accountable owner is recorded in source."
            row["owner_resolution_status"] = "source-gap"
            row["canonical_owner_gap"] = "No accountable owner is recorded in source."

        for field in ("evidence", "proposed_disposition", "existing_ruling", "rollback"):
            if not row.get(field, "").strip():
                fail(f"{key}: required field '{field}' is empty")
        if not row["canonical_issue_basis"].strip():
            fail(f"{key}: issue mapping needs an explicit source or gap basis")
        if not row["owner_resolution_status"] or not row["canonical_owner_basis"].strip():
            fail(f"{key}: owner mapping needs a resolution status and evidence basis")
        normalized.append(row)

    normalized.sort(key=lambda row: row["key"])
    return output_columns, normalized


def write_outputs(
    output_dir: Path,
    columns: list[str],
    rows: list[dict[str, str]],
    yaml_keys: set[str],
    budget_keys: set[str],
    sha: str,
    as_of: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "flag-key-ledger.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    keyed_rows = {row["key"]: row for row in rows}
    json_path = output_dir / "flag-key-ledger.json"
    json_path.write_text(
        json.dumps(
            {
                "sha": sha,
                "as_of": as_of,
                "source_counts": {
                    "yaml_registry_keys": len(yaml_keys),
                    "budget_names": len(budget_keys),
                    "union_keys": len(keyed_rows),
                },
                "keys": keyed_rows,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    stats = {
        "sha": sha,
        "as_of": as_of,
        "total_keys": len(rows),
        "yaml_registry_keys": len(yaml_keys),
        "budget_names": len(budget_keys),
        "source_union_keys": len(yaml_keys | budget_keys),
        "canonical_issue_status": dict(
            sorted(collections.Counter(row["canonical_issue_status"] for row in rows).items())
        ),
        "owner_resolution_status": dict(
            sorted(collections.Counter(row["owner_resolution_status"] for row in rows).items())
        ),
        "owner_class": dict(sorted(collections.Counter(row["owner_class"] for row in rows).items())),
        "disposition": dict(
            sorted(collections.Counter(row["proposed_disposition"] for row in rows).items())
        ),
        "unresolved_owner": sum(row["canonical_owner"] == "UNRESOLVED" for row in rows),
    }
    (output_dir / "ledger-stats.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="reviewed CSV annotation input")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for generated artifacts")
    parser.add_argument("--as-of", required=True, help="analysis date in YYYY-MM-DD form")
    parser.add_argument("--expected-sha", default=PINNED_SHA)
    args = parser.parse_args()

    check = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{args.expected_sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if check.returncode:
        fail(f"source commit {args.expected_sha} is not available in this checkout")

    sha = args.expected_sha
    yaml_keys, budget_keys, source_keys, issue_sources = source_key_sets(sha)
    columns, rows = load_manifest(args.manifest)
    ledger_keys = {row["key"] for row in rows}
    missing = source_keys - ledger_keys
    extra = ledger_keys - source_keys
    if missing or extra:
        fail(
            f"manifest/source mismatch: {len(missing)} missing, {len(extra)} extra; "
            f"missing={sorted(missing)[:20]}, extra={sorted(extra)[:20]}"
        )

    output_columns, normalized = normalize_rows(rows, columns, issue_sources)
    write_outputs(args.output_dir, output_columns, normalized, yaml_keys, budget_keys, sha, args.as_of)
    print(f"validated {len(normalized)} keys at {sha}; wrote outputs to {args.output_dir}")


if __name__ == "__main__":
    main()

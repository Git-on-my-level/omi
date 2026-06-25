#!/usr/bin/env python3
"""Guard the desktop backend prod promotion workflow.

Prod desktop backend deploys are intentionally tied to an explicit stable
macOS release. This check is deliberately text-based and narrow: it fails on
the risky regressions we have already seen, without requiring PyYAML in CI.
"""

from pathlib import Path


WORKFLOW = Path(".github/workflows/desktop_promote_prod.yml")


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def require(needle: str, text: str, message: str) -> None:
    if needle not in text:
        fail(message)


def main() -> int:
    text = WORKFLOW.read_text()

    require("on:\n  release:", text, "prod promotion must be triggered by GitHub release events")
    require("types: [published, edited]", text, "release trigger must cover publish and stable-channel edits")
    require("workflow_dispatch:", text, "manual retry path must remain available")
    require("required: true", text, "manual retries must require an explicit stable release tag")

    forbidden_triggers = [
        "\n  schedule:",
        "\n  push:",
        "\n  pull_request:",
        "\n  pull_request_target:",
    ]
    for trigger in forbidden_triggers:
        if trigger in text:
            fail(f"desktop backend prod promotion must not use automatic trigger {trigger.strip()}")

    require("EVENT_NAME: ${{ github.event_name }}", text, "workflow must distinguish release events from manual retries")
    require('if [ "$EVENT_NAME" = "release" ]; then', text, "release events must use the edited/published release")
    require("Manual prod promotion requires a stable v*-macos release tag.", text, "manual runs must require an explicit tag")
    require("grep -qE '^v.+-macos$'", text, "prod deploys must be limited to macOS desktop release tags")
    require("channel:[[:space:]]*stable", text, "prod deploys must require an exact channel: stable metadata line")
    require("Deploy Desktop Backend to Production", text, "guard should cover the prod deploy workflow")

    if "gh release list" in text:
        fail("prod promotion must not scan old releases; deploy only the event/manual target")

    print("desktop prod promotion policy OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

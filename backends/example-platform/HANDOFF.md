# Handoff — where `backends/example-platform` comes from, and what it is missing

Written 2026-08-17 by the team that authors this tree, for whoever works on v5 next
(human or agent). It is a statement of fact about two repositories, not a proposal.

## 1. Upstream of this directory

    remote   git@github.com:Git-on-my-level/omi-platform.git
    branch   codex/track3-backend-integration
    HEAD     ec814345b4   (2026-08-17)

That branch is where this code is written and verified. This directory is a copy of
it, imported here by `9d7df07dd0` ("Add 'backends/example-platform/' from commit
'5e6bcb7398'"). To see the source of anything in here, read it there.

## 2. The two commits this branch adds, and why they matter

The import took `5e6bcb7398`. Upstream was two commits further along at the time:

| commit (upstream) | what it fixes |
|---|---|
| `c68ea40ce0` | Replays thinking-mode `reasoning_content` on the tool follow-up request, and stops a stray round-1 tool call from discarding an answer already streamed to the user. |
| `ec814345b4` | Stops a completed stream being classified as failed because the provider emitted a cost trailer *after* `data: [DONE]`. |

**Without both, chat fails on any model that calls a tool.** That is not a corner
case — it is every memory-backed turn. The symptom is content streaming into the UI
and then being replaced by "The chat provider is unavailable." Verified against
DeepSeek v4 Flash and GLM.

The second one is worth reading even if you never run this backend, because it is a
parser-disagreement bug and the same shape can exist in any SSE consumer:

    data: [DONE]

    data: {"choices":[],"cost":"0"}

Two readers over the same bytes. One kept reading past the terminator; the other
treated anything after it as invalid. The stream was complete and correct. The fix is
`allowDataAfterDone` on the tool-loop request path — the flag the non-tool path was
already setting. Post-`[DONE]` records are parsed but not applied: no late content, no
late tool calls. See `apps/service/chat/gateway-sse.test.ts`, which pins both readers
to the same input.

Related invariant, since the log table would otherwise mislead you:
`empty_stream` and `empty_content` **cannot fire** while `retryEmptyDone: true`. They
remain in source so the invariant is still named if the retry is turned off. They are
not reachable reasons today.

### Provider configuration

Env names only — never a value, in a log, a commit, or a shell history:

    OMI_BENCH_OPENAI_API_KEY     OMI_BENCH_OPENAI_BASE_URL     OMI_BENCH_OPENAI_MODEL
    OMI_LOCAL_MODEL_GATEWAY_PORT OMI_LOCAL_MODEL_GATEWAY_TOKEN
    GLM_API_KEY / ZAI_API_KEY    (first non-empty wins)

## 3. What this copy is missing — read this before you trust a green result

`bf6451b617` and `b5a729566c` removed **~165,000 lines** across **68 paths** from the
import. That was a deliberate scope decision and this document does not argue with it.
It has one consequence you need to know:

**The verification apparatus is gone. Nothing in this directory can reproduce the
evidence that this code is correct.**

Specifically absent:

- `integration/lanes.mjs` — the L0–L4 ladder runner. Every claim upstream makes about
  this backend is an L-number and a receipt from this file.
- `integration/control-acceptance/` and `integration/evidence/` — the acceptance walk
  that clicks real controls in a real shell and emits a closed vocabulary of outcome
  tokens (`chat=streamed-and-persisted`, `mic=transcript-rendered`,
  `screen=frame-rendered`, `chat.memory=retrieved-and-streamed`, eleven `nav.*`).
  L3 is a 15-control walk taking ~260s.
- `integration/dev-stack.sh`, `integration/dev-app.sh`, `integration/doctor.sh` — the
  only supported ways to bring the stack up. The gateway comment in
  `integration/local-model-gateway.mjs` was edited here to drop its `dev-stack.sh`
  reference; upstream that reference is live and correct.
- `scripts/pre-commit`, `scripts/pre-push`, `scripts/install-git-hooks.sh`,
  `scripts/failure-class`, `scripts/performance-baseline.mjs` — the gates that ran
  before anything landed upstream.
- `integration/lib/receipts.mjs`, `provenance.mjs`, `run-report.mjs`,
  `stack-port-lease.ts` — receipts, provenance, and the loopback port leasing that
  lets concurrent verification runs coexist.
- `frontend/` — **entirely absent, 0 files.** The macOS Swift shell, the surfaces
  package, and every UI fence live upstream and were not imported.
- `.github/`, `spikes/`, `docs/verification.md`, `docs/running-locally.md`,
  `docs/accessibility.md`, `docs/performance-baseline.md`, `docs/ui-harness.md`.

So: `bun test` in this directory exercises the units. It does not and cannot tell you
that Chat streams and persists, that Listen renders a transcript, or that the app
opens. If you need that verdict, run the ladder upstream.

## 4. Full drift between this copy and upstream `ec814345b4`

Measured, not estimated. **Nothing was added to our code here** — the copy is a strict
subset of upstream plus twelve edited files.

- 68 paths in upstream, absent here (section 3).
- 0 paths present here, absent upstream.
- 12 files differ in content:

      AGENTS.md
      apps/service/README.md
      bunfig.toml                                    (dropped the frontend/ ignore)
      docs/architecture.md
      docs/chat-provenance.md
      docs/network-fence-proposal.md
      drivers/postgres/firebase-authorized-memory-service-app.ts
      drivers/postgres/firebase-authorized-memory-service-app.test.ts
      integration/local-model-gateway.mjs            (comment only)
      integration/local-model-gateway.test.ts
      integration/local-test-gateway.test.mjs
      package.json

Nine of those are docs, config, or comment edits that follow from the prune.

**One is a source refactor and it is the thing that will bite you:**
`drivers/postgres/firebase-authorized-memory-service-app.ts`, where the options
validator was rewritten from an `allowed` set to a `required` tuple. As far as we can
tell it is behaviour-preserving — same accepted key set, same required keys, same
descriptor check. But it means this file has now diverged, and the *next* import from
upstream will conflict there rather than fast-forwarding. If the refactor is worth
keeping, send it upstream and we will take it; then both trees agree and future
imports stay clean.

To regenerate this drift report yourself at any time:

    git -C <omi-platform> archive <upstream-sha> | tar -x -C /tmp/upstream
    diff -rq /tmp/upstream <v5>/backends/example-platform

## 5. How to take the next update

The two commits on this branch were applied as patches restricted to this subtree, not
as a subtree merge:

    git -C <omi-platform> format-patch -o /tmp/p <last-imported-sha>..<new-sha>
    git am -p1 --directory=backends/example-platform /tmp/p/*.patch

Use this, not `git subtree pull`. A subtree merge would resurrect all 68 pruned paths
and undo the decision in section 3.

Authorship is preserved by `git am`, which is deliberate: these commits are David
Zhang's and the messages carry the reasoning that justified them.

## 6. Who owns what, as of today

- **This directory (the backend + its verification):** authored upstream at
  `Git-on-my-level/omi-platform`. Send changes there, or tell us and we will pull
  them in. Changes made only here will be overwritten or will conflict.
- **`packages/contracts`, `native-core`, `react-native`, `apps/backend-worker`:** v5.
  Upstream consumes the contract; it does not author it.
- **Desktop:** genuinely contested. v5 has `react-native/macos`. Upstream has a Swift
  shell with a 15-control acceptance walk behind it. Both target the same surface and
  one of them is going to be discarded. That is a decision for the people involved,
  not something either tree should settle by continuing to build.

## 7. One correction to the v5 README

`README.md` says the example backend "lives at
`/Users/undivisible/projects/omi-platform-integration` on
`codex/track3-backend-integration`". The branch name is right; the path is one
machine's checkout. The durable address is the remote in section 1.

# This tree is frozen — it moved to the product monorepo

Frozen 2026-08-14 at fork commit `022056c2c7` (branch `core/foundation`).

The frontend rewrite now lives at **`Git-on-my-level/omi-platform`** under
`frontend/` (mounted with full history — `git log --follow` reaches this
tree's commits). Landed on trunk `codex/track3-backend-integration` at
`e66d20af544801a14cd541b8ea6ffca805dd1c95` per ruling 8 in the workspace's
`data/run-2026-08-14-unified/DAVID-UNIFICATION-DECISION.md`.

Do not commit here. Changes to the rewrite go to `omi-platform/frontend/`.
This fork keeps `app/` (Flutter live product) and `backend/` (Python live
backend) for upstream sync until the traffic flip completes.

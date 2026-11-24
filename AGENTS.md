# wxread maintenance instructions

Read docs/MAINTENANCE.md, docs/RUNBOOK.md and docs/ARCHITECTURE.md before changing this repository. For UI changes also read docs/DESIGN.md and compare docs/design/final-A-1280.png / final-A-390.png. The user's current instructions take precedence; docs/PLAN.md and older review reports are historical context.

Preserve the three core requirements:
- GitHub Actions runs reading/renewal and GitHub Pages hosts the console; no daily local service.
- Scan login captures usable read parameters automatically; normal users never import cURL.
- Every completed daily batch sends a Feishu summary, including successful reads.

Implementation:
- Keep native HTML/CSS/ES modules and existing crypto formats. Do not introduce a server, UI framework or build pipeline without a task that calls for it.
- Write encrypted data only to the state branch, default wxread-state. CAS transforms must preserve concurrent edits, new login credentials and run idempotency.
- Keep full wr_skey, numeric payload fields and zero values. Save rotated cookies even on failed responses.
- Keep bounded retry/recovery, account isolation and persisted progress. Stop requests must match account and run_id.
- Keep read/warm mutually exclusive and shard budgets below Actions limits. Default schedule is Beijing 01:07 and six-hour renewal; page switches and workflow behavior must agree.
- Default browser credentials are ephemeral; opt-in remembering lasts 24 hours. Abort callbacks after disconnect, preserve editing focus, and never treat an unreadable receipt as success.
- Keep Feishu daily summaries distinct from other channels' exception alerts. Notification errors must not erase reading results.

Validation:
- Follow docs/MAINTENANCE.md section 4 and the checks in .github/workflows/tests.yml.
- Test visible changes in a real browser at 1280 and 390 (320 where relevant), including keyboard interaction and failed/pending states.
- Distinguish fixtures, real local WeRead, GitHub state writes, notification API acceptance and recipient receipt. Do not claim real integration from stubs or captured fields alone.
- Run relevant regression tests while implementing and all required checks before handoff; do not add tests that merely mirror trivial presentation changes.
- Update usage/maintenance/architecture docs affected by the final behavior. Keep historical PLAN/DESIGN intact.

Safety of user data:
- Inspect Git status and preserve existing user changes. Do not reset/clean/force-push to solve routine conflicts.
- Never commit/print .qr-cookies.json, cURL dumps, tokens, webhook secrets, .secrets/ or private debug artifacts.
- Normal upgrades must not regenerate keys, reinitialize control or overwrite/delete wxread-state.
- There is no automatic full key rotation or cross-repository migration; require a concrete, tested migration/restore procedure if requested.

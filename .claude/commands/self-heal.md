---
description: Run one full autonomous detect→fix→deploy→verify pass on rom-finder prod. Drives prod-health-monitor + the specialist agents. Built for /loop continuous operation.
---

You are executing ONE iteration of the rom-finder self-healing loop.

**Authorization:** FULL-AUTO. You may edit code, update CLAUDE.md, commit, push to `main`, and trigger
deploys without asking. The guardrails in step 7 are the only brakes — respect them absolutely.

**Cross-iteration state:** read/write `.claude/self-heal-state.json` (create if missing). Track per-symptom
attempt counts and the last action, so repeated failures can trip the runaway guard. Keep it small.

## 1. Observe
Spawn the `prod-health-monitor` agent. Take its structured findings as the source of truth for prod state.

## 2. Decide
- No actionable findings → write a one-line "healthy" report including the running SHA, update state, and
  STOP this iteration (the loop re-checks later). Do nothing else.
- Otherwise pick the SINGLE highest-severity actionable finding. **One fix per iteration** — never bundle
  unrelated changes into one push.

## 3. Diagnose + fix (route by the finding's `fix_route`)
- `hash_unverified` → spawn `ra-verify-debugger` with the example hash+system; implement its recommended
  fix (typically `rahasher.py`, `ra_client.py`, or the Dockerfile RAHasher step). If it concludes the dump
  genuinely isn't in RA's list (not a bug), record that and STOP — that needs a different/better dump, not a
  code change.
- `scheduler_stale` → diagnose `app/services/scheduler.py` + lifespan + TZ. Confirm the container didn't
  just restart (the monitor should have ruled that out). Fix the real cause.
- `deploy_drift` → spawn `deploy-verifier`. If the image built but never deployed, run
  `gh workflow run docker-publish.yml` to rebuild+re-fire the webhook. No code fix needed for pure drift.
- `log_error` / `behavior_mismatch` → reproduce, locate the responsible router/service, fix it.
Apply edits with Edit/Write. Per the project workflow rule, update the relevant CLAUDE.md(s) in the same change.

## 4. Review
Spawn `project-code-reviewer` on the working diff. Resolve every **Must-fix** before shipping. Don't ship a
fix that introduces a new convention violation.

## 5. Ship
- Commit with a clear message + the `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` footer.
- Push to `main`. **Never** force-push or rewrite history. Pushing auto-builds and auto-deploys via CI →
  webhook → `redeploy.sh`.

## 6. Verify the fix actually landed
- Spawn `deploy-verifier`: confirm the new SHA is live (running APP_VERSION == your pushed SHA).
- Re-run the relevant `prod-health-monitor` probe for the specific symptom and confirm it CLEARED. A push is
  not "done" until prod proves the symptom is gone.

## 7. Guardrails — STOP and write a report INSTEAD of acting if any apply:
- The fix would add/alter a DB column or `_MIGRATIONS`, delete/migrate data, or change auth, secrets, or
  deploy config → too risky unattended; hand it to the user with your proposed diff.
- The same symptom has survived **3 fix attempts** across iterations (per state file) → stop thrashing;
  escalate to the user with everything you tried.
- The diagnosis is ambiguous, or the monitor couldn't reach prod → report, don't guess-fix.
- Your push turns CI red / breaks the build → immediately `git revert` the offending commit and push the
  revert (this restores prod), then report. Never leave main broken.

## 8. Report (every iteration)
Summarize: what the monitor found, the one thing you changed (files + commit SHA, or "none"), the deploy
result, whether the symptom is confirmed cleared on prod, and what the next iteration should watch. End the
report with the live APP_VERSION / commit SHA (user preference: always surface the running version).

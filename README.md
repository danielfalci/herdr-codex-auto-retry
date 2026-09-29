# Codex Auto Retry for Herdr

This Herdr plugin watches Codex panes for an explicit usage-limit message with a reset time. It waits for that time, then sends a short continuation prompt into the *same interactive pane* (via `herdr pane send-text` / `send-keys`) so the original session picks the task back up itself. If the continued task reaches another usage limit, the worker schedules the next reset too.

The plugin does not raise or bypass Codex limits; normal Codex permissions and sandbox settings still apply. A continuation that needs interactive approval, or that the plugin can't confirm actually restarted the task, stops and is logged as needing manual intervention rather than being retried silently.

**Design change (2026-09-28):** earlier versions spawned a second, headless `codex exec resume --all <session>` process while the original interactive pane stayed open. Codex CLI treats the interactive pane as the session's owner, so that second process consistently hit a "session locked by another writer" error, and a bug then recorded that failure as `finished: true`, which the monitor read as "reset already handled" — blocking any further automatic retry without telling you why. This version resumes only through the original pane and never spawns a second writer for a live session. If the pane that hit the limit is no longer known (closed, or the plugin was restarted without state), the worker cancels instead of falling back to a headless resume of a session nobody is watching.

## Requirements

- Herdr 0.7.5 or newer
- Python 3.10 or newer
- Codex CLI available as `codex` on `PATH`, authenticated, and with a resumable session

Linux and macOS are declared. Windows is not supported by this version.

## Install

For a local checkout (development changes are used directly by Herdr):

```sh
herdr plugin link .
herdr plugin action invoke local.codex-auto-retry.start
```

The plugin starts its monitor automatically when Herdr restores its server session. The explicit `start` command is needed after a new local link because Herdr does not run startup hooks at link time.

After publication, install it with:

```sh
herdr plugin install <owner>/herdr-codex-auto-retry
```

## Actions

- `local.codex-auto-retry.start` starts the background monitor.
- `local.codex-auto-retry.status` shows monitor state and recent events.
- `local.codex-auto-retry.stop` stops the monitor and pending waits.
- `local.codex-auto-retry.diagnose` reads current Codex panes and reports eligibility without resuming anything.

The monitor checks waiting Codex panes (`idle`, `done`, `blocked`, or `unknown`) every 12 seconds. Herdr uses `blocked` for approval/question dialogs; a quota error can leave Codex `idle`. It reads unwrapped terminal text and only schedules a resume for an explicit usage-limit notice with a parseable reset time. Resets that passed within the last 24 hours are eligible; older notices are ignored. A time-only reset uses today, and an observed reset is cached using a screen hash so polling does not move relative deadlines.

It tracks the same session ID and pane and stops after eight consecutive quota windows as a runaway guard. Only one live worker is allowed per session. Before sending, the worker re-checks that the pane still shows the same session, is not already `working`, and is not displaying what looks like an approval/question prompt (a best-effort heuristic — not yet validated against a live Codex TUI screen). After sending, it polls the pane for confirmation that work actually resumed (status becomes `working`), rather than trusting that the send command succeeded.

Each worker records an explicit outcome instead of a single ambiguous "finished" flag: `resumed` (confirmed), `cancelled` (pane closed, session changed, or already working), or `needs_intervention` (an approval prompt is blocking it, it could not confirm the resume after retrying, or an unexpected error occurred). Only a `resumed` outcome for the exact reset that was handled suppresses a future automatic retry for that same notice; a `needs_intervention` outcome is not silently retried either, so a failure stays visible instead of masquerading as handled. A genuinely new usage-limit notice (a different reset time) is still scheduled normally even after an earlier resume or failure.

Logs and per-session retry state are kept in Herdr's plugin state directory. The plugin does not save pane transcripts or Codex output. Logs record scheduling and inspection decisions only when they change. Observation files contain only a screen hash and the parsed reset timestamp.

## Missing Herdr session metadata

On Linux, when `agent_session` is absent, the plugin identifies the unique interactive Codex process with the matching `HERDR_PANE_ID`. The retry key includes the terminal ID, PID, and kernel process start time. Ambiguous matches are rejected, and a quota notice must still be present immediately before submission. This fallback tracks the process, not a Codex conversation UUID; switching conversations inside the same process is not independently identifiable. macOS still requires Herdr session metadata.

## Known gaps

- The approval/question-dialog detector (`is_approval_dialog`) and the confirmation poll are heuristics written from the diagnosis, not verified against a live Codex TUI screen. They may need adjustment after a real quota-reset cycle.
- The plugin does not yet detect text the user has already typed into the pane's input box before sending a continuation; sending could interleave with an in-progress manual edit. Not addressed in this pass — see the diagnostic doc.
- The worker PID file has no process-identity check beyond `kill(pid, 0)`, and a reset time with no date could be reinterpreted after midnight. Neither is confirmed to have caused an incident; noted for future hardening.

## Incident during rollout (2026-09-28)

Restarting the monitor after this rewrite exposed two more bugs, found only by restarting against a real, in-use pane (not by the mocked test suite, which can't see real scrollback). Both are fixed and covered by regression tests, but are recorded here because they were serious near-misses:

- **Legacy state files.** A worker-state file written by the pre-0.3.0 code (`finished: true`, no `phase`) was not recognized by the new schema, so `launch_worker` treated it as "no record" and rescheduled an hours-old notice against a pane the user was actively using. Fixed: any state file without a `phase` key is now refused outright (`"legacy worker state; clear it manually before retrying"`), never treated as clear-to-schedule.
- **Stale notice still in the polling window.** `parse_reset` picked the *last* usage-limit notice found in the last 100 lines even when a large amount of later screen content (completed work, a new prompt) had appeared after it — i.e. even when the session had clearly moved on. Fixed: a notice is now ignored if more than `STALE_TRAILING_CHARS` (300) characters of other content follow it on screen, since that means it is history, not a live block.

Two automated resume attempts fired against a real pane before these fixes landed; both were caught and killed before `send_continuation` (or before Enter) took effect, confirmed by reading the pane afterward, but this is why the plugin should not be restarted against live sessions again without re-running `diagnose` first (read-only) to confirm it comes back quiet.

## Development

Run the standard-library tests with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use synthetic terminal messages and a mocked Herdr pane API; they do not spend quota or resume a live session.

## 0.3.0 validation

Regression tests cover idle quota errors, overdue resets, time-only dates, latest notices, unwrapped reads, relative deadline stability, duplicate workers, read-only diagnosis, approval-dialog detection, pre-send readiness checks (closed pane, changed session, already working, pending approval), post-send confirmation (resumed, cancelled, new limit, unconfirmed), chained reset scheduling, stale-notice suppression after a confirmed resume, and rescheduling for a genuinely new reset after a prior resume or failure. Tests mock Herdr and Codex and do not consume quota. A real quota-reset cycle still needs validation under normal use, per the diagnostic doc's scope: not to be done without the user's explicit sign-off since it consumes quota and can resume a real task.

# Codex Auto Retry for Herdr

This Herdr plugin watches Codex panes for an explicit usage-limit message with a reset time. It waits for that time, then resumes the same Codex session with a short continuation prompt. If the continued task reaches another usage limit, the worker schedules the next reset too.

The plugin does not raise or bypass Codex limits. It uses the existing Codex CLI authentication and `codex exec resume`; normal Codex permissions and sandbox settings still apply. A continuation that needs interactive approval may stop and need your input.

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

It resumes the same session ID and working directory and stops after eight consecutive quota windows as a runaway guard. Only one live worker is allowed per session. Before running, the worker cancels if the originating pane closed, switched sessions, or is already working. Retrying runs in a background `codex exec resume` process; its output does not appear in the original interactive pane. Normal permissions apply, and interactive approvals can require manual intervention.

Logs and per-session retry state are kept in Herdr's plugin state directory. The plugin does not save pane transcripts or Codex output. Logs record scheduling and inspection decisions only when they change. Observation files contain only a screen hash and the parsed reset timestamp.

## Development

Run the standard-library tests with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use synthetic terminal messages and mocked Codex subprocesses; they do not spend quota or resume a live session.

## 0.2.0 validation

Regression tests cover idle quota errors, overdue resets, time-only dates, latest notices, unwrapped reads, relative deadline stability, duplicate workers, read-only diagnosis, and cancellation when a session is already working. Tests mock Codex and do not consume quota. A real quota-reset cycle still needs validation under normal use.

# Codex Auto Retry for Herdr

This Herdr plugin watches Codex panes for an explicit usage-limit message with a reset time. It waits for that time, then resumes the same Codex session with a short continuation prompt. If the continued task reaches another usage limit, the worker schedules the next reset too.

The plugin does not raise or bypass Codex limits. It uses the existing Codex CLI authentication and `codex exec resume`; normal Codex permissions and sandbox settings still apply. A continuation that needs interactive approval may stop and need your input.

## Requirements

- Herdr 0.7.5 or newer
- Python 3.10 or newer
- Codex CLI available as `codex` on `PATH`, authenticated, and with a resumable session

Linux and macOS are declared. Windows is not supported by this version.

## Install

For a local checkout:

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

The monitor checks blocked Codex panes every 12 seconds. It only schedules a resume when the pane contains a recognized usage-limit message and a parseable reset time. It resumes the same session ID and working directory and stops after eight consecutive quota windows as a runaway guard.

Logs and per-session retry state are kept in Herdr's plugin state directory. The plugin does not save pane transcripts or Codex output.

## Development

Run the standard-library tests with:

```sh
python3 -m unittest discover -s tests -v
```

The tests use synthetic terminal messages and mocked Codex subprocesses; they do not spend quota or resume a live session.

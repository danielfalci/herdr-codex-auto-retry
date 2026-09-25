#!/usr/bin/env python3
"""Herdr plugin: resume Codex sessions after an explicit usage-limit reset."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


POLL_SECONDS = 12
RESET_GRACE_SECONDS = 30
CONTINUE_PROMPT = (
    "Continue the interrupted task from the last safe point. "
    "Check what is already complete before repeating any work."
)


def state_dir() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_STATE_DIR", Path.home() / ".local/state/herdr/codex-auto-retry"))


def log(message: str) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with (directory / "codex-auto-retry.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")


def herdr(*args: str, timeout: int = 10) -> str:
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    result = subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"herdr {' '.join(args[:2])} exited {result.returncode}")
    return result.stdout


def pane_list() -> list[dict[str, Any]]:
    payload = json.loads(herdr("pane", "list", timeout=15))
    result = payload.get("result", payload)
    return result.get("panes", [])


def pane_text(pane_id: str) -> str:
    return herdr("pane", "read", pane_id, "--source", "recent", "--lines", "100", timeout=10)


def session_key(pane: dict[str, Any]) -> str | None:
    session = pane.get("agent_session") or {}
    value = session.get("value")
    return str(value) if value else None


def localize(value: dt.datetime) -> dt.datetime:
    return value.astimezone()


def parse_reset(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Parse only a reset time appearing near an explicit Codex usage-limit notice."""
    now = now or dt.datetime.now().astimezone()
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", text)
    clean = re.sub(r"\s+", " ", clean)
    limit = re.search(r"(?:you(?:'|’)ve hit|hit|reached|exceeded).{0,100}(?:usage|rate|session|weekly).{0,40}limit|(?:usage|rate|session|weekly) limit.{0,80}(?:hit|reached|try again)", clean, re.I)
    if not limit:
        return None
    nearby = clean[limit.start():limit.start() + 400]

    iso = re.search(r"\b20\d{2}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?", nearby)
    if iso:
        token = iso.group(0).replace("Z", "+00:00")
        try:
            value = dt.datetime.fromisoformat(token)
            return localize(value if value.tzinfo else value.replace(tzinfo=now.tzinfo))
        except ValueError:
            pass

    relative = re.search(r"(?:try again|reset(?:s)?|available)\s+(?:in\s+)?(?:(\d+)\s*h(?:ours?)?\s*)?(?:(\d+)\s*m(?:in(?:utes?)?)?\s*)?(?:(\d+)\s*s(?:ec(?:onds?)?)?)?", nearby, re.I)
    if relative and any(relative.groups()):
        hours, minutes, seconds = (int(part or 0) for part in relative.groups())
        return now + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)

    absolute = re.search(
        r"(?:try again|reset(?:s)?|available)(?:\s+at)?\s+"
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
        r"\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\s+\d{1,2}:\d{2}\s*[AP]M|"
        r"\d{1,2}:\d{2}\s*[AP]M(?:\s+[A-Z]{2,5})?)",
        nearby,
        re.I,
    )
    if absolute:
        raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", absolute.group(1), flags=re.I)
        for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d %I:%M %p", "%B %d %I:%M %p", "%I:%M %p"):
            candidate = re.sub(r"\s+(?!AM\b|PM\b)[A-Z]{2,5}$", "", raw.strip(), flags=re.I)
            try:
                value = dt.datetime.strptime(candidate, fmt)
                if "%Y" not in fmt:
                    value = value.replace(year=now.year)
                    value = value.replace(tzinfo=now.tzinfo)
                    if value < now - dt.timedelta(minutes=2):
                        value = value.replace(year=value.year + 1) if "%b" in fmt or "%B" in fmt else value + dt.timedelta(days=1)
                else:
                    value = value.replace(tzinfo=now.tzinfo)
                return value
            except ValueError:
                continue
    return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def start() -> int:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pidfile = directory / "monitor.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            if pid_alive(pid):
                print(f"Codex auto retry monitor already running (pid {pid}).")
                return 0
        except (ValueError, OSError):
            pass
        pidfile.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "monitor"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(20):
        time.sleep(0.05)
        if pidfile.exists():
            print(f"Codex auto retry monitor started (pid {pidfile.read_text().strip()}).")
            return 0
        if proc.poll() is not None:
            break
    print("Could not start the monitor; see the Herdr plugin log.", file=sys.stderr)
    return 1


def worker_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return state_dir() / f"worker-{safe}.json"


def launch_worker(pane: dict[str, Any], reset_at: dt.datetime) -> None:
    sid = session_key(pane)
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    if not sid or not cwd or not Path(cwd).is_dir():
        return
    record = worker_path(sid)
    if record.exists():
        try:
            previous = json.loads(record.read_text(encoding="utf-8"))
            if previous.get("source_reset_at", previous.get("reset_at")) == reset_at.isoformat():
                pid = previous.get("pid")
                if pid and pid_alive(int(pid)):
                    return
                if previous.get("finished"):
                    return
        except (ValueError, OSError, TypeError):
            pass
    command = [sys.executable, str(Path(__file__).resolve()), "resume", sid, str(Path(cwd)), reset_at.isoformat()]
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    record.write_text(json.dumps({"pid": proc.pid, "reset_at": reset_at.isoformat(), "source_reset_at": reset_at.isoformat(), "phase": "waiting", "started_at": dt.datetime.now().astimezone().isoformat()}), encoding="utf-8")
    log(f"scheduled Codex session {sid} for {reset_at.isoformat()}")


def monitor() -> int:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pidfile = directory / "monitor.pid"
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    log("monitor started")
    try:
        while True:
            try:
                for pane in pane_list():
                    if pane.get("agent") != "codex" or pane.get("agent_status") != "blocked":
                        continue
                    try:
                        screen = pane_text(str(pane["pane_id"]))
                        reset_at = parse_reset(screen)
                        if reset_at and reset_at > dt.datetime.now().astimezone():
                            launch_worker(pane, reset_at)
                    except Exception as exc:
                        log(f"could not inspect Codex pane {pane.get('pane_id', '?')}: {type(exc).__name__}")
            except Exception as exc:
                log(f"pane scan failed: {type(exc).__name__}")
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        pidfile.unlink(missing_ok=True)
        log("monitor stopped")
    return 0


def resume_session(session_id: str, cwd: str, reset_iso: str) -> int:
    reset_at = dt.datetime.fromisoformat(reset_iso)
    source_reset_iso = reset_iso
    state = worker_path(session_id)
    try:
        cmd = ["codex", "exec", "resume", "--all", session_id, CONTINUE_PROMPT]
        for cycle in range(8):
            wait_for = max(0, (reset_at - dt.datetime.now().astimezone()).total_seconds()) + RESET_GRACE_SECONDS
            log(f"waiting {int(wait_for)}s before resuming session {session_id}")
            state.write_text(json.dumps({"pid": os.getpid(), "reset_at": reset_at.isoformat(), "source_reset_at": source_reset_iso, "phase": "waiting"}), encoding="utf-8")
            time.sleep(wait_for)
            for attempt in range(7):
                state.write_text(json.dumps({"pid": os.getpid(), "reset_at": reset_at.isoformat(), "phase": "resuming"}), encoding="utf-8")
                result = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=None, check=False)
                output = result.stdout + "\n" + result.stderr
                next_reset = parse_reset(output)
                if next_reset and next_reset > dt.datetime.now().astimezone() + dt.timedelta(seconds=RESET_GRACE_SECONDS):
                    reset_at = next_reset
                    reset_iso = reset_at.isoformat()
                    log(f"Codex session {session_id} reached another usage limit; scheduled its next reset")
                    state.write_text(json.dumps({"pid": os.getpid(), "reset_at": reset_iso, "source_reset_at": source_reset_iso, "phase": "waiting"}), encoding="utf-8")
                    break
                if result.returncode == 0:
                    log(f"Codex session {session_id} resumed and finished (exit 0)")
                    state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
                    return 0
                lower = output.lower()
                if not any(marker in lower for marker in ("already in use", "active writer", "locked by another", "thread is currently active")):
                    log(f"Codex resume for session {session_id} exited {result.returncode}; inspect Codex output")
                    state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
                    return result.returncode
                if attempt < 6:
                    delay = min(60, 5 * (2 ** attempt))
                    log(f"Codex session {session_id} is still open in another writer; retrying in {delay}s")
                    state.write_text(json.dumps({"pid": os.getpid(), "reset_at": reset_at.isoformat(), "source_reset_at": source_reset_iso, "phase": "waiting"}), encoding="utf-8")
                    time.sleep(delay)
            else:
                log(f"Codex session {session_id} stayed open in another writer; automatic resume stopped")
                state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
                return 1
        log(f"Codex session {session_id} reached the automatic retry cap")
        state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
        return 1
    except Exception as exc:
        log(f"Codex resume for session {session_id} failed: {type(exc).__name__}")
        state.write_text(json.dumps({"reset_at": reset_iso, "finished": True}), encoding="utf-8")
        return 1


def stop() -> int:
    pidfile = state_dir() / "monitor.pid"
    if not pidfile.exists():
        print("Codex auto retry monitor is not running.")
        return 0
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        stopped = 0
        for worker in state_dir().glob("worker-*.json"):
            try:
                item = json.loads(worker.read_text(encoding="utf-8"))
                worker_pid = int(item.get("pid", 0))
                if item.get("phase") == "waiting" and worker_pid and pid_alive(worker_pid):
                    os.killpg(worker_pid, signal.SIGTERM)
                    stopped += 1
            except (ValueError, OSError, TypeError, json.JSONDecodeError):
                continue
        print(f"Codex auto retry monitor stopped; cancelled {stopped} pending resume(s).")
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        print("Codex auto retry monitor was already stopped.")
    return 0


def status() -> int:
    pidfile = state_dir() / "monitor.pid"
    running = False
    if pidfile.exists():
        try:
            running = pid_alive(int(pidfile.read_text(encoding="utf-8").strip()))
        except (ValueError, OSError):
            pass
    print(f"Codex auto retry monitor: {'running' if running else 'stopped'}")
    log_path = state_dir() / "codex-auto-retry.log"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-5:]:
            print(line)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"start", "ensure"}:
        return start()
    if argv[0] == "monitor":
        return monitor()
    if argv[0] == "stop":
        return stop()
    if argv[0] == "status":
        return status()
    if argv[0] == "resume" and len(argv) == 4:
        return resume_session(argv[1], argv[2], argv[3])
    print("Usage: codex_auto_retry.py [start|stop|status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

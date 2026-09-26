#!/usr/bin/env python3
"""Herdr plugin: resume Codex sessions after an explicit usage-limit reset."""

from __future__ import annotations

import datetime as dt
import hashlib
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
MAX_OVERDUE_SECONDS = 24 * 60 * 60
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
    return herdr("pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", "100", timeout=10)


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
    limits = list(re.finditer(r"(?:you(?:'|’)ve hit|hit|reached|exceeded).{0,100}?(?:usage|rate|session|weekly).{0,40}?limit|(?:usage|rate|session|weekly) limit.{0,80}?(?:hit|reached|try again)", clean, re.I))
    if not limits:
        return None
    limit = limits[-1]
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
                parse_candidate, parse_fmt = candidate, fmt
                if "%Y" not in fmt and ("%b" in fmt or "%B" in fmt):
                    parse_candidate, parse_fmt = f"{candidate} 2000", f"{fmt} %Y"
                value = dt.datetime.strptime(parse_candidate, parse_fmt)
                if "%b" not in fmt and "%B" not in fmt:
                    value = value.replace(year=now.year, month=now.month, day=now.day, tzinfo=now.tzinfo)
                elif "%Y" not in fmt:
                    value = value.replace(year=now.year)
                    value = value.replace(tzinfo=now.tzinfo)
                    # Choose the nearest year; an overdue reset must stay overdue.
                    candidates = []
                    for year in (now.year - 1, now.year, now.year + 1):
                        try:
                            candidates.append(value.replace(year=year))
                        except ValueError:
                            continue
                    value = min(candidates, key=lambda candidate: abs((candidate - now).total_seconds()))
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


def launch_worker(pane: dict[str, Any], reset_at: dt.datetime) -> str:
    sid = session_key(pane)
    cwd = pane.get("cwd") or pane.get("foreground_cwd")
    if not sid or not cwd or not Path(cwd).is_dir():
        return "missing session ID or valid working directory"
    record = worker_path(sid)
    if record.exists():
        try:
            previous = json.loads(record.read_text(encoding="utf-8"))
            pid = previous.get("pid")
            if not previous.get("finished") and pid and pid_alive(int(pid)):
                return "worker already active"
            if previous.get("source_reset_at", previous.get("reset_at")) == reset_at.isoformat():
                if previous.get("finished"):
                    return "reset already handled"
        except (ValueError, OSError, TypeError):
            pass
    command = [sys.executable, str(Path(__file__).resolve()), "resume", sid, str(Path(cwd)), reset_at.isoformat(), str(pane.get("pane_id", ""))]
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    record.write_text(json.dumps({"pid": proc.pid, "reset_at": reset_at.isoformat(), "source_reset_at": reset_at.isoformat(), "phase": "waiting", "started_at": dt.datetime.now().astimezone().isoformat()}), encoding="utf-8")
    log(f"scheduled Codex session {sid} for {reset_at.isoformat()}")
    return "scheduled"


def inspect_pane(pane: dict[str, Any], *, dry_run: bool = False, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now().astimezone()
    status = pane.get("agent_status")
    if status not in {"blocked", "idle", "done", "unknown"}:
        return f"not waiting ({status})"
    sid = session_key(pane)
    if not sid:
        return "missing session ID"
    screen = pane_text(str(pane["pane_id"]))
    digest = hashlib.sha256(re.sub(r"\s+", " ", screen).encode()).hexdigest()
    record = worker_path(sid)
    observation = record.with_name(record.name.replace("worker-", "observation-", 1))
    reset_at = None
    try:
        cached = json.loads(observation.read_text(encoding="utf-8"))
        if cached.get("digest") == digest:
            reset_at = dt.datetime.fromisoformat(cached["reset_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if reset_at is None:
        reset_at = parse_reset(screen, now)
        if reset_at is not None and not dry_run:
            observation.parent.mkdir(parents=True, exist_ok=True)
            observation.write_text(json.dumps({"digest": digest, "reset_at": reset_at.isoformat()}), encoding="utf-8")
    if reset_at is None:
        return "no recognized usage-limit reset"
    if (now - reset_at).total_seconds() > MAX_OVERDUE_SECONDS:
        return "reset older than 24 hours; ignored"
    if dry_run:
        return f"eligible; reset {reset_at.isoformat()}"
    return launch_worker(pane, reset_at)


def diagnose() -> int:
    for pane in pane_list():
        if pane.get("agent") == "codex":
            print(f"{pane['pane_id']}: {inspect_pane(pane, dry_run=True)}")
    return 0


def monitor() -> int:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pidfile = directory / "monitor.pid"
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    log("monitor started")
    decisions: dict[str, str] = {}
    try:
        while True:
            try:
                for pane in pane_list():
                    if pane.get("agent") != "codex":
                        continue
                    try:
                        pane_id = str(pane["pane_id"])
                        decision = inspect_pane(pane)
                        if decisions.get(pane_id) != decision:
                            log(f"Codex pane {pane_id}: {decision}")
                            decisions[pane_id] = decision
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


def resume_session(session_id: str, cwd: str, reset_iso: str, pane_id: str = "") -> int:
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
                if pane_id:
                    pane = next((item for item in pane_list() if item.get("pane_id") == pane_id), None)
                    if pane is None or session_key(pane) != session_id or pane.get("agent_status") == "working":
                        log(f"Codex session {session_id}: automatic resume cancelled; pane closed, changed session, or already working")
                        state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
                        return 0
                state.write_text(json.dumps({"pid": os.getpid(), "reset_at": reset_at.isoformat(), "source_reset_at": source_reset_iso, "phase": "resuming"}), encoding="utf-8")
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
        state.write_text(json.dumps({"reset_at": reset_iso, "source_reset_at": source_reset_iso, "finished": True}), encoding="utf-8")
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
    if argv[0] == "diagnose":
        return diagnose()
    if argv[0] == "resume" and len(argv) in {4, 5}:
        return resume_session(*argv[1:])
    print("Usage: codex_auto_retry.py [start|stop|status|diagnose]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

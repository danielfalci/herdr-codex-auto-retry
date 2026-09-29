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
MAX_CHAIN_HOPS = 8
STALE_TRAILING_CHARS = 300
MAX_SEND_ATTEMPTS = 5
CONFIRM_TIMEOUT_MS = 20000
POST_START_CHECKS = 20
POST_START_POLL_SECONDS = 0.5
EXPIRED_RESET_BACKOFF_SECONDS = 120
CONTINUE_PROMPT = (
    "Continue the interrupted task from the last safe point. "
    "Check what is already complete before repeating any work."
)
ACTIVE_WORKER_PHASES = {"waiting", "sending"}
APPROVAL_DIALOG_PATTERN = re.compile(
    r"\ballow\b[^\n?]{0,80}\?|\bapprove\b|\[y/n\]|\d\.\s*yes\b.{0,60}\d\.\s*no\b",
    re.I | re.S,
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


def pane_lookup(pane_id: str) -> dict[str, Any] | None:
    for pane in pane_list():
        if str(pane.get("pane_id")) == pane_id:
            return pane
    return None


def process_key(pane: dict[str, Any], proc_root: Path = Path("/proc")) -> str | None:
    """Linux fallback: bind retries to one Codex process in this terminal.

    Never infer identity from cwd (multiple sessions can share it). PID and
    kernel start time prevent a restarted process from inheriting a retry.
    """
    if pane.get("agent") != "codex" or not pane.get("terminal_id") or not pane.get("pane_id"):
        return None
    matches = []
    for proc in proc_root.glob("[0-9]*"):
        try:
            if (proc / "comm").read_text().strip() != "codex":
                continue
            env = (proc / "environ").read_bytes().split(b"\0")
            expected = f"HERDR_PANE_ID={pane['pane_id']}".encode()
            if expected not in env:
                continue
            # Exclude app-server and exec children inheriting the same pane.
            args = (proc / "cmdline").read_bytes().split(b"\0")[1:]
            if any(arg in {b"app-server", b"exec", b"mcp-server"} for arg in args):
                continue
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            matches.append(f"process-{pane['terminal_id']}-{proc.name}-{fields[19]}")
        except (OSError, ValueError, IndexError):
            continue
    return matches[0] if len(matches) == 1 else None


def session_key(pane: dict[str, Any]) -> str | None:
    session = pane.get("agent_session") or {}
    value = session.get("value")
    return str(value) if value else process_key(pane)


def is_approval_dialog(screen: str) -> bool:
    """Heuristic: an approval/question prompt, not a plain quota notice.

    Best-effort pattern match; not verified against a live Codex TUI screen.
    Refine after real-session validation (see diagnostic doc).
    """
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", screen)
    return bool(APPROVAL_DIALOG_PATTERN.search(clean))


def ready_to_send(pane_id: str, session_id: str) -> tuple[bool, str]:
    pane = pane_lookup(pane_id)
    if pane is None:
        return False, "pane closed"
    if session_key(pane) != session_id:
        return False, "pane now shows a different session"
    if pane.get("agent_status") == "working":
        return False, "pane already working"
    screen = pane_text(pane_id)
    if session_id.startswith("process-") and parse_reset(screen) is None:
        return False, "quota notice no longer present"
    if is_approval_dialog(screen):
        return False, "pane is waiting on an approval or question"
    return True, "ready"


def _agent_prompt_call(pane_id: str, text: str, *, timeout_ms: int) -> dict[str, Any]:
    """Run `herdr agent prompt` directly (not through `herdr()`) so a
    rejection or timeout's error payload can be inspected instead of just
    raising on a non-zero exit code.
    """
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    result = subprocess.run(
        [binary, "agent", "prompt", pane_id, text, "--wait", "--until", "working", "--until", "blocked", "--timeout", str(timeout_ms)],
        capture_output=True,
        text=True,
        timeout=(timeout_ms / 1000) + 15,
        check=False,
    )
    try:
        return json.loads(result.stdout or result.stderr or "{}")
    except ValueError:
        return {}


def send_and_confirm(
    pane_id: str,
    session_id: str,
    prompt: str,
    *,
    timeout_ms: int = CONFIRM_TIMEOUT_MS,
) -> tuple[str, dt.datetime | None]:
    """Submit the continuation through `herdr agent prompt`.

    That command sends text and Enter as one ordered, bracketed-paste-aware
    submission and waits for the pane to settle into `working` or `blocked`
    (or times out) — this replaces an earlier version that sent raw
    `pane send-text` + `send-keys enter`, which did not reliably submit in
    the Codex TUI and left the prompt sitting unsent in the input box on
    every retry.

    Returns (outcome, next_reset): "resumed" (work started), "cancelled"
    (pane closed or switched session), "needs_intervention" (herdr itself
    rejected the submission because the pane is blocked on an approval or
    question), "new_limit" (another usage-limit notice appeared, with its
    parsed reset time), or "unconfirmed" (submitted but no change observed
    before the timeout).
    """
    payload = _agent_prompt_call(pane_id, prompt, timeout_ms=timeout_ms)
    error_code = (payload.get("error") or {}).get("code")
    if error_code == "agent_not_found":
        return "cancelled", None
    if error_code == "agent_blocked":
        return "needs_intervention", None

    pane = pane_lookup(pane_id)
    if pane is None or session_key(pane) != session_id:
        return "cancelled", None
    status = pane.get("agent_status")
    # `working` may only be the request starting; a quota rejection can
    # return the TUI to idle immediately. Observe that transition before
    # recording this reset as handled.
    for _ in range(POST_START_CHECKS):
        if status != "working":
            break
        time.sleep(POST_START_POLL_SECONDS)
        pane = pane_lookup(pane_id)
        if pane is None or session_key(pane) != session_id:
            return "cancelled", None
        status = pane.get("agent_status")
    if status == "working":
        return "resumed", None
    if status == "blocked":
        return "needs_intervention", None
    screen = pane_text(pane_id)
    next_reset = parse_reset(screen)
    if next_reset is not None:
        return "new_limit", next_reset
    return "unconfirmed", None


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
    trailing = clean[limit.start() + 400:]
    if len(trailing.strip()) > STALE_TRAILING_CHARS:
        # Something substantial happened on screen after this notice: the
        # session has moved on, so treat it as history, not a live block.
        return None

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
    pane_id = str(pane.get("pane_id") or "")
    if not sid or not cwd or not Path(cwd).is_dir():
        return "missing session ID or valid working directory"
    if not pane_id:
        return "missing pane ID"
    reset_iso = reset_at.isoformat()
    record = worker_path(sid)
    if record.exists():
        try:
            previous = json.loads(record.read_text(encoding="utf-8"))
        except (ValueError, OSError, TypeError):
            previous = None
        if previous is not None:
            if "phase" not in previous:
                # Pre-0.3.0 state file (used "finished" instead of "phase").
                # Its outcome is unknown under the current schema, so do not
                # infer eligibility from it; require a human to clear it.
                return "legacy worker state; clear it manually before retrying"
            pid = previous.get("pid")
            if previous.get("phase") in ACTIVE_WORKER_PHASES and pid and pid_alive(int(pid)):
                return "worker already active"
            if previous.get("last_reset_handled") == reset_iso:
                return "reset already handled"
            if previous.get("phase") == "needs_intervention" and previous.get("source_reset_at") == reset_iso:
                return "needs manual intervention"
    command = [sys.executable, str(Path(__file__).resolve()), "resume", sid, str(Path(cwd)), reset_iso, pane_id]
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    record.write_text(json.dumps({
        "pid": proc.pid,
        "session_id": sid,
        "pane_id": pane_id,
        "reset_at": reset_iso,
        "source_reset_at": reset_iso,
        "phase": "waiting",
        "started_at": dt.datetime.now().astimezone().isoformat(),
    }), encoding="utf-8")
    log(f"scheduled Codex session {sid} for {reset_iso}")
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
    """Wait for the reset, then continue the session through its own pane.

    This sends the continuation prompt into the original interactive pane
    (via `herdr pane send-text` / `send-keys`) instead of spawning a second
    `codex exec resume` writer against the same session, which is what
    caused the persistent lock conflict this fix addresses. A pane is
    required: if the originating pane is unknown, the worker cancels rather
    than falling back to a headless resume of a session nobody supervises.
    """
    reset_at = dt.datetime.fromisoformat(reset_iso)
    source_reset_iso = reset_iso
    last_quota_reset_iso = reset_iso
    state = worker_path(session_id)

    def write(phase: str, **extra: Any) -> None:
        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "session_id": session_id,
            "pane_id": pane_id,
            "source_reset_at": source_reset_iso,
            "reset_at": reset_at.isoformat(),
            "phase": phase,
            "updated_at": dt.datetime.now().astimezone().isoformat(),
        }
        payload.update(extra)
        state.write_text(json.dumps(payload), encoding="utf-8")

    if not pane_id:
        log(f"Codex session {session_id}: no pane to resume through; automatic resume skipped")
        write("cancelled", reason="no pane_id")
        return 1

    try:
        for _hop in range(MAX_CHAIN_HOPS):
            wait_for = max(0, (reset_at - dt.datetime.now().astimezone()).total_seconds()) + RESET_GRACE_SECONDS
            log(f"waiting {int(wait_for)}s before resuming session {session_id}")
            write("waiting")
            time.sleep(wait_for)

            advance_hop = False
            for attempt in range(MAX_SEND_ATTEMPTS):
                ready, reason = ready_to_send(pane_id, session_id)
                if not ready:
                    if reason in {"pane closed", "pane now shows a different session", "pane already working", "quota notice no longer present"}:
                        log(f"Codex session {session_id}: automatic resume cancelled; {reason}")
                        write("cancelled", reason=reason)
                        return 0
                    log(f"Codex session {session_id}: {reason}; needs manual intervention")
                    write("needs_intervention", reason=reason)
                    return 1

                write("sending")
                outcome, next_reset = send_and_confirm(pane_id, session_id, CONTINUE_PROMPT)

                if outcome == "resumed":
                    log(f"Codex session {session_id} resumed via pane {pane_id}")
                    write("resumed", last_reset_handled=last_quota_reset_iso)
                    return 0
                if outcome == "cancelled":
                    log(f"Codex session {session_id}: automatic resume cancelled; pane closed or changed session after send")
                    write("cancelled", reason="pane closed or changed session after send")
                    return 0
                if outcome == "new_limit" and next_reset is not None:
                    last_quota_reset_iso = next_reset.isoformat()
                    now = dt.datetime.now().astimezone()
                    if next_reset <= now:
                        # Displayed minute can stay unchanged after its reset.
                        # Retry with bounded backoff instead of marking success
                        # or sending repeatedly at the expired timestamp.
                        backoff = min(600, EXPIRED_RESET_BACKOFF_SECONDS * (2 ** _hop))
                        reset_at = now + dt.timedelta(seconds=backoff)
                        log(f"Codex session {session_id}: quota still unavailable after displayed reset; backing off {backoff}s")
                    else:
                        reset_at = next_reset
                    log(f"Codex session {session_id} reached another usage limit; scheduled its next reset")
                    write("waiting")
                    advance_hop = True
                    break
                if outcome == "needs_intervention":
                    log(f"Codex session {session_id}: herdr rejected the submission; pane is blocked on an approval or question")
                    write("needs_intervention", reason="pane blocked on an approval or question during send")
                    return 1

                # unconfirmed: no visible change after sending
                if attempt < MAX_SEND_ATTEMPTS - 1:
                    delay = min(60, 5 * (2 ** attempt))
                    log(f"Codex session {session_id}: resume unconfirmed; retrying in {delay}s")
                    write("waiting")
                    time.sleep(delay)
                    continue
                log(f"Codex session {session_id}: could not confirm the resume after {MAX_SEND_ATTEMPTS} attempts")
                write("needs_intervention", reason="could not confirm resume")
                return 1

            if advance_hop:
                continue

        log(f"Codex session {session_id} reached the automatic retry cap")
        write("needs_intervention", reason="reached chain-hop cap")
        return 1
    except Exception as exc:
        log(f"Codex resume for session {session_id} failed: {type(exc).__name__}")
        write("needs_intervention", reason=f"unexpected error: {type(exc).__name__}")
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
                if item.get("phase") in ACTIVE_WORKER_PHASES and worker_pid and pid_alive(worker_pid):
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

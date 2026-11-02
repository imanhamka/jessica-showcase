#!/usr/bin/env python3
"""
WhatsApp Agent launcher — Python PTY wrapper for claude-whatsapp.
Provides fake TTY, auto-confirms dialogs, manages context lifecycle.
"""
import os
import re
import pty
import subprocess
import time
import select
import struct
import termios
import fcntl
import sys
import signal
import json
import shutil
import threading
from datetime import datetime, timedelta

# Portable paths — derived from script location / env, no hardcoded user.
HOME = os.environ.get("HOME") or os.path.expanduser("~")
AGENT_DIR = os.environ.get("WA_AGENT_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("WA_STATE_DIR") or f"{AGENT_DIR}/.whatsapp"
LOG = f"{STATE_DIR}/logs/agent-pty.log"
DEBUG = f"{STATE_DIR}/logs/agent-debug.log"
SESSION_STATE = f"{STATE_DIR}/session-state.md"
PID_FILE = f"{STATE_DIR}/server.pid"
MAX_DEBUG_MB = 10
COMPACT_THRESHOLD = 75   # send /compact at this % context
MAX_COMPACTS = 2         # after this many compacts, restart fresh instead
TIMEOUT_THRESHOLD = 300  # 5min of no output = stuck
USAGE_WARN_THRESHOLD = 80  # notify owner once session usage hits this %
DEFAULT_NOTIFY_JID = os.environ.get("WA_OWNER_JID", "")  # owner WhatsApp JID (fallback)
SESSION_MAX_AGE = int(os.environ.get("WA_SESSION_MAX_AGE", "1800"))  # 30min — periodic fresh restart
SESSION_HARD_MAX = int(os.environ.get("WA_SESSION_HARD_MAX", "7200"))  # 2h hard cap — restart even if busy
FLUSH_GRACE = int(os.environ.get("WA_FLUSH_GRACE", "420"))  # 7min for agent to flush; /clear regardless after that
SWAP_GRACE = 30            # interactive swap (owner swap command) — short flush bound, user is waiting
RESUME_REQUEST = f"{STATE_DIR}/tmp/.swap-request"  # WA writes here (owner swap commands)
BUSY_SENTINEL = f"{STATE_DIR}/tmp/.busy"             # agent touches this while long bg work runs
BUSY_STALE = 1800          # ignore busy marker older than this (work likely dead)
RESUME_TARGET = f"{STATE_DIR}/resume-target"          # persisted target: spawn resumes this session
REFRESH_NOW = f"{STATE_DIR}/tmp/.refresh"         # touch to trigger a refresh immediately

def load_prompt(name, **kw):
    """Supervisor prompts live in a private prompts/ directory, not in this repo."""
    path = os.path.join(AGENT_DIR, "prompts", f"{name}.txt")
    with open(path, encoding="utf-8") as f:
        return f.read().strip().format(**kw)

CLEAR_RECOVERY_PROMPT = load_prompt("after_clear")


def read_resume_target():
    """Persisted resume target consumed at spawn. None = normal Jessica session."""
    try:
        with open(RESUME_TARGET) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def apply_swap(req):
    """Persist (or clear) the resume target so the next spawn picks it up."""
    action = req.get("action")
    if action == "resume":
        try:
            with open(RESUME_TARGET, "w") as f:
                json.dump({"session_id": req["session_id"], "cwd": req["cwd"]}, f)
            log(f"Swap: resume target -> {req['session_id']} ({req['cwd']})")
        except (OSError, KeyError) as e:
            log(f"Swap: failed to write resume target: {e}")
    elif action == "reset":
        try:
            if os.path.exists(RESUME_TARGET):
                os.remove(RESUME_TARGET)
            log("Swap: reset to normal Jessica session")
        except OSError as e:
            log(f"Swap: failed to reset: {e}")


def pending_work():
    """True if session-state.md declares unfinished work (pending: != none/blank).
    Reliable defer signal that does NOT depend on the agent touching .busy —
    a live subagent that forgot the marker must not get its work killed."""
    try:
        with open(SESSION_STATE) as f:
            for line in f:
                low = line.strip().lower()
                if low.startswith("pending:"):
                    val = low.split(":", 1)[1].strip().strip('"').strip("'")
                    return val not in ("", "none", "-", "takde", "tiada")  # "takde"/"tiada" = Malay for "none"
    except OSError:
        pass
    return False


def notify_chat_id():
    """Owner JID to notify on usage/session events: env -> config.env -> default."""
    cid = os.environ.get("WA_CHAT_ID")
    if cid:
        return cid.strip().strip('"').strip("'")
    try:
        with open(f"{AGENT_DIR}/config.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("WA_CHAT_ID"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return DEFAULT_NOTIFY_JID


def cleanup_pid_file():
    """Remove PID file on graceful shutdown."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            log("Cleanup: removed server.pid")
    except OSError as e:
        log(f"Cleanup: failed to remove server.pid: {e}")


def signal_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT."""
    signal_name = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT", signal.SIGHUP: "SIGHUP"}.get(signum, f"SIG{signum}")
    log(f"Received {signal_name} — graceful shutdown")
    cleanup_pid_file()
    sys.exit(0)


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}\n")


def dbg(data):
    try:
        if os.path.exists(DEBUG) and os.path.getsize(DEBUG) > MAX_DEBUG_MB * 1024 * 1024:
            os.rename(DEBUG, DEBUG + ".old")
        with open(DEBUG, "ab") as f:
            f.write(data)
    except OSError:
        pass


def kill_proc(proc, grace=10):
    """Terminate a child, escalating SIGTERM -> SIGKILL.

    A Claude session stuck in stop hooks ignores SIGTERM. Without escalation the
    "graceful restart" leaves a zombie holding the WhatsApp lock and the agent
    stops replying. Wait `grace` seconds, then SIGKILL.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        log(f"PID {proc.pid} ignored SIGTERM after {grace}s — sending SIGKILL")
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"SIGKILL failed for PID {proc.pid}: {e}")


def write_session_state(reason, extra=None):
    """Write a restart-reason marker so next session can recover."""
    try:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        with open(SESSION_STATE, "w") as f:
            f.write(f"restart_reason: {reason}\n")
            f.write(f"restart_time: {ts}\n")
            if extra:
                f.write(f"{extra}\n")
    except OSError:
        pass


def parse_reset_seconds(text):
    """
    Parse the reset time from rate limit dialog text.
    Handles formats like: 'resets 10pm', 'resets 10:30pm', 'resets midnight'
    Returns seconds to sleep. Defaults to 3600 if unparseable.
    """
    tz_offset = 8  # Asia/Kuala_Lumpur = UTC+8

    m = re.search(r'resets\s+(midnight|noon|(\d{1,2})(?::(\d{2}))?\s*(am|pm))',
                  text, re.IGNORECASE)
    if not m:
        return 3600  # default 1 hour

    now_utc = datetime.utcnow()
    now_local = now_utc + timedelta(hours=tz_offset)

    word = m.group(1).lower()
    if word == "midnight":
        reset_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if reset_local <= now_local:
            reset_local += timedelta(days=1)
    elif word == "noon":
        reset_local = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        if reset_local <= now_local:
            reset_local += timedelta(days=1)
    else:
        hour = int(m.group(2))
        minute = int(m.group(3)) if m.group(3) else 0
        ampm = m.group(4).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        reset_local = now_local.replace(hour=hour, minute=minute, second=30, microsecond=0)
        if reset_local <= now_local:
            reset_local += timedelta(days=1)

    delta = (reset_local - now_local).total_seconds()
    return max(60, int(delta))


def rotate_logs():
    for logfile in [f"{STATE_DIR}/logs/system.log", LOG]:
        try:
            if os.path.exists(logfile) and os.path.getsize(logfile) > 50 * 1024 * 1024:
                os.rename(logfile, logfile + ".old")
                log(f"Rotated {logfile}")
        except OSError:
            pass


class TimeoutWatchdog(threading.Thread):
    """Monitors for connection timeouts (408 errors). Triggers restart if stuck >5min."""
    def __init__(self, proc, timeout=TIMEOUT_THRESHOLD):
        super().__init__(daemon=True)
        self.proc = proc
        self.timeout = timeout
        self.last_output = time.time()
        self.stop_event = threading.Event()
        self.connection_errors = 0

    def mark_activity(self):
        """Called when output is received — resets timeout."""
        self.last_output = time.time()
        self.connection_errors = 0

    def mark_connection_error(self):
        """Called when 408 or connection error detected."""
        self.connection_errors += 1
        if self.connection_errors >= 3:
            log(f"Watchdog: {self.connection_errors} connection errors — triggering restart")
            self.trigger_restart()
            self.connection_errors = 0

    def trigger_restart(self):
        """Kill process and cleanup for restart."""
        try:
            log("Watchdog: triggering graceful restart due to connection timeout")
            write_session_state("watchdog_timeout")
            cleanup_pid_file()
            kill_proc(self.proc)
        except Exception as e:
            log(f"Watchdog: failed to terminate: {e}")

    def run(self):
        """Keep thread alive for the connection-error path.

        Idle (no output) is NOT stuck — when no WhatsApp message arrives the agent
        is correctly silent. Restarting on idle caused a restart loop that spammed
        [SESSION BARU]. Real stuck states are caught by mark_connection_error (408 x3).
        """
        while not self.stop_event.is_set():
            self.stop_event.wait(10)


def main():
    os.makedirs(f"{STATE_DIR}/logs", exist_ok=True)
    os.makedirs(f"{STATE_DIR}/tmp", exist_ok=True)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)

    rotate_logs()
    log("Starting Claude WhatsApp agent...")

    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 50, 220, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Resolve claude binary: env override -> ~/.local/bin -> PATH lookup
    claude_bin = (
        os.environ.get("CLAUDE_BIN")
        or (f"{HOME}/.local/bin/claude" if os.path.exists(f"{HOME}/.local/bin/claude") else None)
        or shutil.which("claude")
        or f"{HOME}/.local/bin/claude"
    )

    env = os.environ.copy()
    env.update({
        "HOME": HOME,
        "PATH": f"/usr/local/bin:{HOME}/.local/bin:{os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CLAUDE_CODE_TMPDIR": f"{STATE_DIR}/tmp",
    })

    # Resume mode: if a target is persisted, resume that session in its own cwd
    # with full context. WhatsApp plugin stays loaded so WA replies still work.
    resume_target = read_resume_target()
    base_flags = [
        "--dangerously-load-development-channels",
        "plugin:whatsapp@claude-whatsapp",
        "--dangerously-skip-permissions",
    ]
    if resume_target:
        spawn_args = [claude_bin, "--resume", resume_target["session_id"]] + base_flags
        spawn_cwd = resume_target.get("cwd") or AGENT_DIR
        log(f"Resuming session {resume_target['session_id']} in {spawn_cwd}")
    else:
        spawn_args = [claude_bin] + base_flags
        spawn_cwd = AGENT_DIR

    try:
        proc = subprocess.Popen(
            spawn_args,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=env, cwd=spawn_cwd, close_fds=True,
        )
    except Exception as e:
        log(f"Failed to spawn claude: {e}")
        # Bad resume target would loop forever — clear it and fall back next start.
        if resume_target:
            try:
                os.remove(RESUME_TARGET)
            except OSError:
                pass
        sys.exit(1)

    os.close(slave_fd)
    log(f"Spawned PID {proc.pid}")

    # Start watchdog thread
    watchdog = TimeoutWatchdog(proc)
    watchdog.start()

    confirmed = False
    compact_count = 0
    rate_limit_handled = False
    usage_notified = False
    restart_pending = False
    flush_started = 0
    deferred_notice_sent = False  # log-once when a refresh is deferred for pending work
    swap_action = None       # set when a owner swap request is consumed
    preamble_sent = False    # inject WA-reply reminder once after resuming
    notify_jid = notify_chat_id()
    flush_sentinel = f"{STATE_DIR}/tmp/.flushed"
    buf = b""
    start_time = time.time()

    while proc.poll() is None:
        now = time.time()

        # --- Scheduled 30-min fresh restart: flush memory first, then restart ---
        # Defer if work is in flight — either a fresh .busy marker OR session-state
        # declares pending work — but never past SESSION_HARD_MAX so a stale/forgotten
        # signal cannot block restart forever. Owner order: don't kill unfinished work.
        age = now - start_time
        if os.path.exists(REFRESH_NOW):
            try:
                os.remove(REFRESH_NOW)
            except OSError:
                pass
            log("Manual refresh requested via .refresh")
            start_time = now - SESSION_MAX_AGE - 1
            age = now - start_time
        if age < SESSION_HARD_MAX:
            try:
                marker_fresh = (now - os.path.getmtime(BUSY_SENTINEL)) < BUSY_STALE
            except OSError:
                marker_fresh = False  # no marker
            busy = marker_fresh or pending_work()
        else:
            busy = False  # hard cap reached — force refresh regardless
        if confirmed and not restart_pending and age > SESSION_MAX_AGE and busy and not deferred_notice_sent:
            log(f"Refresh due but work in flight (age {int(age)}s) — deferring, not killing")
            deferred_notice_sent = True
            # Owner policy (A): silent refresh normally, but ping when a refresh is
            # deferred for pending work. Agent decides — skip if mid-work, so we don't
            # derail active subagents.
            dmsg = load_prompt("refresh_deferred", owner=notify_jid)
            time.sleep(0.5)
            try:
                os.write(master_fd, dmsg.encode() + b"\r")
            except OSError:
                pass
        if confirmed and not restart_pending and age > SESSION_MAX_AGE and not busy:
            restart_pending = True
            flush_started = now
            try:
                if os.path.exists(flush_sentinel):
                    os.remove(flush_sentinel)
            except OSError:
                pass
            write_session_state("scheduled_refresh")
            log("Scheduled 30min refresh — asking agent to flush memory before restart")
            instr = load_prompt("flush_before_refresh", sentinel=flush_sentinel)
            try:
                os.write(master_fd, instr.encode() + b"\r")
            except OSError:
                pass

        # --- Session swap request from WhatsApp (owner swap commands) ---
        if confirmed and not restart_pending and os.path.exists(RESUME_REQUEST):
            try:
                with open(RESUME_REQUEST) as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = None
            try:
                os.remove(RESUME_REQUEST)
            except OSError:
                pass
            if req:
                swap_action = req
                restart_pending = True
                flush_started = now
                try:
                    if os.path.exists(flush_sentinel):
                        os.remove(flush_sentinel)
                except OSError:
                    pass
                write_session_state("session_swap", f"swap: {req.get('action')}")
                log(f"Session swap requested: {req.get('action')} {req.get('project', '')}")
                instr = load_prompt("flush_before_swap", sentinel=flush_sentinel)
                try:
                    os.write(master_fd, instr.encode() + b"\r")
                except OSError:
                    pass

        if restart_pending:
            # Clean exit: agent signalled done via sentinel file (no stdout-echo races).
            # Firm bound: restart after grace even if agent still busy (avoids the
            # indefinite wait when a high-effort turn never touches the sentinel).
            done = os.path.exists(flush_sentinel)
            grace = SWAP_GRACE if swap_action else FLUSH_GRACE
            timed = (now - flush_started) > grace
            if done or timed:
                why = "flush done" if done else f"{grace}s grace elapsed"
                if not swap_action and not resume_target:
                    # /clear resets context but keeps the process — and the WhatsApp
                    # plugin connection — alive. Killing it reconnected WA 27-40x/day,
                    # suspected trigger of the 2026-09-24 device_removed logouts.
                    log(f"Scheduled refresh — {why}, sending /clear (WA stays connected)")
                    try:
                        if not done:
                            os.write(master_fd, b"\x1b")  # interrupt the still-running turn
                            time.sleep(1.5)
                        os.write(master_fd, b"/clear\r")
                        time.sleep(4.0)
                        os.write(master_fd, CLEAR_RECOVERY_PROMPT.encode() + b"\r")
                    except OSError:
                        pass
                    restart_pending = False
                    deferred_notice_sent = False
                    compact_count = 0
                    start_time = now
                    buf = b""
                    continue
                log(f"Restart ({'swap' if swap_action else 'scheduled refresh, resumed session'}) — {why}")
                kill_proc(proc)
                if swap_action:
                    # Persist/clear target, then exit 0 for an immediate LaunchD restart
                    # that re-spawns in resume (or normal) mode.
                    apply_swap(swap_action)
                    cleanup_pid_file()
                    watchdog.stop_event.set()
                    sys.exit(0)
                break

        try:
            r, _, _ = select.select([master_fd], [], [], 1.0)
        except (ValueError, OSError):
            break

        if not r:
            # Watchdog: if no confirmation after 60s, force Enter
            if not confirmed and (time.time() - start_time) > 60:
                log("Watchdog: 60s elapsed, forcing Enter")
                try:
                    os.write(master_fd, b"\r")
                except OSError:
                    pass
                confirmed = True
            continue

        try:
            chunk = os.read(master_fd, 8192)
        except OSError:
            break

        dbg(chunk)
        buf += chunk
        text = buf.decode("utf-8", errors="replace")
        watchdog.mark_activity()

        # Detect connection timeouts and errors
        if "connection closed (status 408)" in text or "connection closed (status undefined)" in text:
            watchdog.mark_connection_error()
        if "connection closed (status 405)" in text:
            watchdog.mark_connection_error()
        if "messages.db could not be opened" in text or "another instance owns" in text:
            watchdog.mark_connection_error()

        # --- Dev channel confirmation dialog (startup only) ---
        if not confirmed:
            if "Enter" in text and "confirm" in text and "Esc" in text and "cancel" in text:
                log("Confirmation bar detected — sending Enter")
                time.sleep(0.8)
                try:
                    os.write(master_fd, b"\r")
                except OSError:
                    pass
                confirmed = True
                buf = b""
                log("Enter sent.")

        if not confirmed:
            continue

        # --- Resumed session: remind WA-reply rule once (project CLAUDE.md lacks it) ---
        if resume_target and not preamble_sent:
            time.sleep(1.0)
            pre = load_prompt("resumed_session", owner=notify_jid)
            try:
                os.write(master_fd, pre.encode() + b"\r")
            except OSError:
                pass
            preamble_sent = True
            buf = b""
            continue

        # --- Context management ---
        # Claude CLI shows "NN% context used" with autocompact off, but
        # "NN% until auto-compact" (remaining, not used) with autocompact on.
        m = re.search(r'(\d+)%\s*context used', text)
        if m:
            pct = int(m.group(1))
        else:
            m = re.search(r'(\d+)%\s*until auto-compact', text)
            pct = (100 - int(m.group(1))) if m else None

        if pct is not None:
            if pct >= COMPACT_THRESHOLD and compact_count < MAX_COMPACTS:
                compact_count += 1
                log(f"Context {pct}% full — sending /compact (attempt {compact_count}/{MAX_COMPACTS})")
                time.sleep(1.5)
                try:
                    os.write(master_fd, b"/compact\r")
                except OSError:
                    pass
                buf = b""

            elif pct >= 95 and compact_count >= MAX_COMPACTS:
                # Already compacted max times and context still filling — restart fresh
                log(f"Context {pct}% after {compact_count} compacts — triggering graceful restart")
                write_session_state(f"context_{pct}pct_after_{compact_count}_compacts")
                time.sleep(1.0)
                kill_proc(proc)
                break

        # --- Session usage warning (notify owner once at threshold) ---
        if not usage_notified:
            um = re.search(r"used (\d+)% of your session limit", text)
            if um and int(um.group(1)) >= USAGE_WARN_THRESHOLD:
                upct = int(um.group(1))
                rm = re.search(r"resets ([0-9]{1,2}:[0-9]{2}\s*[ap]\.?m\.?)", text)
                reset = rm.group(1).strip() if rm else "?"
                usage_notified = True
                log(f"Session usage {upct}% — notifying owner {notify_jid} (reset {reset})")
                msg = load_prompt("usage_warning", pct=upct, reset=reset, owner=notify_jid)
                time.sleep(0.5)
                try:
                    os.write(master_fd, msg.encode() + b"\r")
                except OSError:
                    pass
                buf = b""

        # --- Rate limit dialog (only on a REAL block, not the 80-99% warning) ---
        if not rate_limit_handled:
            if ("What do you want to do" in text or "rate-limit-options" in text or
                    "Stop and wait" in text or "limit reached" in text or
                    re.search(r"used 100% of your session limit", text)):

                sleep_secs = parse_reset_seconds(text)
                reset_ts = time.strftime('%H:%M', time.localtime(time.time() + sleep_secs))
                log(f"Rate limit hit — killing Claude, sleeping {sleep_secs}s until ~{reset_ts}, then restart")
                write_session_state("rate_limit", f"reset_at: {reset_ts}\nsleep_seconds: {sleep_secs}")
                rate_limit_handled = True

                # Kill Claude — don't wait in frozen session
                kill_proc(proc)

                # Sleep until reset (outside Claude process — no RAM disk issues)
                log(f"Sleeping until {reset_ts}...")
                time.sleep(sleep_secs)
                log("Rate limit should be reset — exiting for LaunchD restart")
                # Exit 0 so LaunchD restarts immediately (no extra 10s penalty sleep)
                sys.exit(0)

        if len(buf) > 65536:
            buf = buf[-32768:]

    exit_code = proc.wait()
    log(f"Process exited with code {exit_code}")

    # Stop watchdog
    watchdog.stop_event.set()
    watchdog.join(timeout=2)

    # Cleanup PID file
    cleanup_pid_file()

    # Resume that dies almost immediately = bad session id / cwd. Clear the target
    # so the next LaunchD restart falls back to a normal Jessica session (no loop).
    if resume_target and exit_code != 0 and (time.time() - start_time) < 30:
        log("Resume failed early — clearing resume target, falling back to normal session")
        try:
            os.remove(RESUME_TARGET)
        except OSError:
            pass

    if exit_code != 0:
        log("Non-zero exit — sleeping 10s before restart")
        time.sleep(10)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

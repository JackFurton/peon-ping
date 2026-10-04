#!/usr/bin/env python3
"""
peon-ping Antigravity watcher — event emitter for antigravity-py.sh

Monitors Antigravity conversation stores for file changes and emits
CESP-compatible JSON events to stdout.

This script is a pure event emitter — it does NOT play sounds directly.
The parent shell wrapper (antigravity-py.sh) reads these JSON lines and
pipes each one into peon.sh, which handles sound playback, config,
pack rotation, volume, notifications, etc.

Output format (one JSON object per line):
  {"event": "SessionStart", "session_id": "antigravity-abc12345", "cwd": "/path"}
  {"event": "UserPromptSubmit", "session_id": "antigravity-abc12345", "cwd": "/path"}
  {"event": "Stop", "session_id": "antigravity-abc12345", "cwd": "/path"}

Two event sources, in preference order:

1. Transcript mode (~/.gemini/antigravity*/brain/<guid>/.system_generated/logs/transcript.jsonl)
   Records are parsed as they are appended, so turn boundaries are read
   from the agent's own record types rather than guessed from file mtime:

     USER_INPUT (source=USER_EXPLICIT)  → UserPromptSubmit
     PLANNER_RESPONSE with tool_calls   → (no event, turn still running)
     tool results (GENERIC, RUN_COMMAND, VIEW_FILE, ...)
                                        → (no event, turn still running)
     PLANNER_RESPONSE, no tool_calls, with prose
                                        → Stop (final answer, turn over)
     PLANNER_RESPONSE, no tool_calls, no prose
                                        → (no event, failed generation)
     ERROR_MESSAGE                      → PostToolUseFailure (first in a run)

   Idle timeouts are disabled for these conversations. A tool call that
   runs longer than any threshold can no longer be mistaken for
   completion, and the write that follows it can no longer be mistaken
   for a new user prompt.

   Only transcript.jsonl is parsed. Antigravity also writes a
   byte-identical transcript_full.jsonl; parsing both would emit every
   event twice.

2. Legacy mtime mode (~/.gemini/antigravity*/conversations/*.pb, *.db)
   Sessions with no transcript fall back to silence detection:
     New conversation file            → SessionStart
     IDLE → ACTIVE transition         → UserPromptSubmit
     ACTIVE → IDLE (45s silence)      → Stop

Tool confirmation prompts are read from the Antigravity CLI log
(~/.gemini/antigravity*/cli.log, a symlink into log/cli-<timestamp>.log that
is retargeted at every launch):
  "Surfacing tool confirmation"  → PermissionRequest (CESP input.required)
  "Responding to tool confirmation" → clears the pending prompt

The surfacing line carries no conversation ID (only the response line
does), so the prompt is attributed to the most recently active
conversation. With two Antigravity sessions running at once the
attribution can land on the wrong one; the sound still fires.

Not detectable: resource.limit, user.spam. Rate-limit ERROR_MESSAGEs are
reported as task.error because peon.sh only reaches resource.limit via
PreCompact.

Requires: Python 3.8+, watchdog

Usage (called by antigravity-py.sh, not directly):
  python3 antigravity-watcher.py [--cwd /path]
"""

import json
import os
import re
import sqlite3
import sys
import time
import signal
import logging
from pathlib import Path
from urllib.parse import unquote

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers.polling import PollingObserver
except ImportError:  # Allow state-machine tests to import without watchdog.
    Observer = None
    PollingObserver = None

    class FileSystemEventHandler:
        pass

# --- Paths ---
HOME = Path.home()
ANTIGRAVITY_DIRS = [
    Path(os.path.expanduser(os.environ.get("ANTIGRAVITY_DIR", "~/.gemini/antigravity"))),
    HOME / ".gemini/antigravity-cli",
    HOME / ".gemini/antigravity-ide",
]

CONVERSATIONS_DIR = os.path.expanduser(
    os.environ.get("ANTIGRAVITY_CONVERSATIONS_DIR", str(ANTIGRAVITY_DIRS[0] / "conversations"))
)
BRAIN_DIR = os.path.expanduser(
    os.environ.get("ANTIGRAVITY_BRAIN_DIR", str(ANTIGRAVITY_DIRS[0] / "brain"))
)

# --- Timing ---
# Only used by legacy .pb/.db conversations that have no transcript to
# parse. Agents write every 1-5s during active work, but a single long
# tool call (a test suite, a build) produces one long gap, and anything
# under that gap length reports completion in the middle of the turn.
# 45s is a compromise; transcript-backed sessions ignore this entirely.
IDLE_THRESHOLD = float(os.environ.get("ANTIGRAVITY_IDLE_SECONDS", "45"))
CHECK_INTERVAL = 1.0        # how often to poll for completions
PER_GUID_COOLDOWN = 30.0    # min seconds between Stop events per GUID
STARTUP_GRACE = float(os.environ.get("ANTIGRAVITY_STARTUP_GRACE", "30"))

# --- Logging (to stderr so stdout stays clean for JSON events) ---
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [antigravity-watcher] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("antigravity-watcher")

# --- States ---
IDLE = "idle"
ACTIVE = "active"

# Antigravity writes transcript.jsonl and a byte-identical
# transcript_full.jsonl. Parse exactly one of them.
PRIMARY_TRANSCRIPT = "transcript.jsonl"

# Record types that mean "a tool ran and returned". They are the agent
# reading its own tool output mid-turn, not the user saying anything.
TOOL_RESULT_TYPES = {
    "GENERIC",
    "RUN_COMMAND",
    "VIEW_FILE",
    "LIST_DIRECTORY",
    "GREP_SEARCH",
    "CODE_ACTION",
    "READ_URL_CONTENT",
    "INVOKE_SUBAGENT",
    "CHECKPOINT",
    "SYSTEM_MESSAGE",
    "CONVERSATION_HISTORY",
}

CONFIRM_SURFACE = "Surfacing tool confirmation"
CONFIRM_RESPOND = "Responding to tool confirmation"

# Older Antigravity builds record shell results as RUN_COMMAND with an
# exit_code field; newer ones record them as GENERIC and state the code
# in the content instead. Both are in use, so read both.
EXIT_CODE_RE = re.compile(r"The command exited with code (\d+)")

# Antigravity stores the workspace as a file:// URI in the conversation db.
FILE_URI_RE = re.compile(rb"file://(/[ -~]+?)(?=[\x00-\x1f]|$)")
COMMAND_RESULT_TYPES = ("RUN_COMMAND", "GENERIC")


def watched_roots():
    """Return existing directories that may contain Antigravity conversation state."""
    candidates = [Path(CONVERSATIONS_DIR), Path(BRAIN_DIR)]
    if not os.environ.get("ANTIGRAVITY_CONVERSATIONS_DIR") and not os.environ.get("ANTIGRAVITY_BRAIN_DIR"):
        for base in ANTIGRAVITY_DIRS:
            candidates.extend([base / "conversations", base / "brain"])

    roots = []
    seen = set()
    for root in candidates:
        expanded = Path(os.path.expanduser(str(root)))
        key = str(expanded)
        if key in seen or not expanded.is_dir():
            continue
        seen.add(key)
        roots.append(expanded)
    return roots


def log_paths():
    """
    Return existing Antigravity CLI log files, resolved through symlinks.

    cli.log in the base directory is a symlink into log/, retargeted at every
    launch (log/cli-<timestamp>.log). Watching the symlink's own directory
    never sees the writes, because they land on the target one level down, so
    both locations are globbed and every path is resolved to its real file.
    """
    paths = []
    seen = set()
    for base in ANTIGRAVITY_DIRS:
        base = Path(os.path.expanduser(str(base)))
        for pattern in ("cli*.log", "log/cli*.log"):
            for candidate in sorted(base.glob(pattern)):
                try:
                    real = candidate.resolve()
                except OSError:
                    continue
                key = str(real)
                if key in seen or not real.is_file():
                    continue
                seen.add(key)
                paths.append(real)
    return paths


def path_is_watched(path):
    """Return True for files that represent Antigravity conversation activity."""
    path = Path(path)
    if path.suffix in (".pb", ".db"):
        return True
    return path.name.startswith("transcript") and path.suffix == ".jsonl"


def is_transcript(path):
    """Return True for any transcript JSONL variant."""
    path = Path(path)
    return path.name.startswith("transcript") and path.suffix == ".jsonl"


def is_primary_transcript(path):
    """Return True only for the transcript we parse (see PRIMARY_TRANSCRIPT)."""
    return Path(path).name == PRIMARY_TRANSCRIPT


def is_log(path):
    """Return True for an Antigravity CLI log file."""
    path = Path(path)
    return path.name.startswith("cli") and path.suffix == ".log"


def read_new_lines(path, offsets, key):
    """
    Return complete lines appended to `path` since the last call, and
    advance `offsets[key]` past them.

    A trailing line with no newline is a partial write still in flight;
    it is left unconsumed so the next call sees it whole. If the file
    shrank it was rotated or truncated, so the offset resets to 0.

    The key is normalised, because the same file arrives here by two routes:
    a filesystem event carries the path as written (cli.log, a symlink),
    while polling carries it resolved. Two keys for one file means reading
    it twice and emitting everything twice.
    """
    key = os.path.realpath(key)
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    start = offsets.get(key, 0)
    if size < start:
        start = 0
    if size == start:
        return []

    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(size - start)
    except OSError:
        return []

    consumed = chunk.rfind(b"\n")
    if consumed == -1:
        # Nothing complete yet — wait for the rest of the line.
        return []

    offsets[key] = start + consumed + 1
    text = chunk[: consumed + 1].decode("utf-8", "replace")
    return [line for line in text.splitlines() if line.strip()]


def extract_guid(path):
    """Extract conversation GUID from supported Antigravity state file paths."""
    path = Path(path)
    if path.suffix in (".pb", ".db"):
        return path.stem

    if path.name.startswith("transcript") and path.suffix == ".jsonl":
        parts = path.parts
        if "brain" in parts:
            idx = parts.index("brain")
            if idx + 1 < len(parts):
                return parts[idx + 1]

    return ""


def _cwd_from_conversation_db(guid):
    """
    Read a conversation's own workspace out of its database.

    conversations/<guid>.db keeps it in trajectory_metadata_blob as a
    file:// URI. This is per conversation and does not go stale, so it
    answers for a session resumed from weeks ago, which the last-used
    cache cannot.
    """
    for base in ANTIGRAVITY_DIRS:
        db = Path(os.path.expanduser(str(base))) / "conversations" / f"{guid}.db"
        if not db.is_file():
            continue
        try:
            # Read-only: Antigravity may be writing this same file.
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute(
                    "select data from trajectory_metadata_blob limit 1"
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if not row or not row[0]:
            continue
        match = FILE_URI_RE.search(row[0])
        if match:
            return unquote(match.group(1).decode("utf-8", "replace"))
    return None


def _cwd_from_recent_cache(guid):
    """
    Fall back to the last-conversation-per-workspace cache.

    Only covers the newest conversation in each workspace, but costs one
    small JSON read and covers a conversation whose database has not been
    written yet.
    """
    for base in ANTIGRAVITY_DIRS:
        cache = Path(os.path.expanduser(str(base))) / "cache" / "last_conversations.json"
        try:
            data = json.loads(cache.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for project, cached_guid in data.items():
            if cached_guid == guid and project:
                return project
    return None


def conversation_cwd(guid, default):
    """
    Return the workspace a conversation belongs to.

    The watcher runs as a daemon, so its own working directory is "/", and
    without this every notification would be attributed to that instead of
    the project being worked in. peon.sh turns an unusable cwd into the
    label "claude", so Antigravity's events would claim to be Claude Code's.
    """
    return _cwd_from_conversation_db(guid) or _cwd_from_recent_cache(guid) or default


def command_exit_code(record):
    """
    Return the shell exit code recorded on a tool result, or None.

    peon.sh only sounds task.error for a failure that names a tool and
    carries an error string, so the code has to be recovered here rather
    than inferred downstream.
    """
    code = record.get("exit_code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    match = EXIT_CODE_RE.search(str(record.get("content") or ""))
    return int(match.group(1)) if match else None


def emit_event(event_name, guid, cwd, extra=None):
    """
    Write a single JSON event line to stdout.
    The parent shell wrapper reads this and pipes it to peon.sh.
    """
    session_id = f"antigravity-{guid[:8]}"
    payload = {
        "event": event_name,
        "session_id": session_id,
        "cwd": cwd,
    }
    if extra:
        payload.update(extra)
    # Flush immediately so the shell wrapper sees it without buffering
    print(json.dumps(payload), flush=True)


class ConversationWatcher(FileSystemEventHandler):
    """
    Tracks ALL conversations with a two-state machine each.

    Conversations that have a transcript.jsonl are driven by parsed
    records (see _handle_record) and never time out. Conversations
    without one fall back to silence detection:

    IDLE   → file modified  → emit UserPromptSubmit → ACTIVE
    ACTIVE → file modified  → update timestamp (no event)
    ACTIVE → IDLE_THRESHOLD → emit Stop             → IDLE

    New state file created  → emit SessionStart     → ACTIVE
    """

    def __init__(self, cwd):
        super().__init__()
        self.cwd = cwd
        self.start_time = time.time()
        # guid → {"state", "last_mod", "last_stop", "transcript"}
        self.conversations = {}
        # path → byte offset, for transcripts and cli.log alike
        self.offsets = {}
        # Conversation a tool confirmation prompt is attributed to
        self.last_active_guid = None
        self.pending_permission = False

        # Pre-register existing files as IDLE so we don't
        # false-trigger events on startup
        for root in watched_roots():
            for path in root.rglob("*"):
                if not path.is_file() or not path_is_watched(path):
                    continue
                guid = extract_guid(path)
                if not guid:
                    continue
                conv = self.conversations.setdefault(guid, {
                    "state": IDLE,
                    "last_mod": 0,
                    "last_stop": 0,
                    "transcript": False,
                })
                if is_primary_transcript(path):
                    # Start at EOF — the backlog is history, not events.
                    conv["transcript"] = True
                    self.offsets[os.path.realpath(path)] = self._file_size(path)

        # Same for the CLI logs: only new confirmation prompts matter.
        for path in log_paths():
            self.offsets[os.path.realpath(path)] = self._file_size(path)

        # A confirmation prompt is attributed to the last active conversation,
        # which is unknown until one produces a record. Seed it from the most
        # recently touched transcript, so a watcher started mid-session can
        # still announce a prompt instead of staying silent until the next
        # thing the user types.
        self.last_active_guid = self._most_recent_guid()

        log.info(f"Pre-registered {len(self.conversations)} existing conversations")

    def _most_recent_guid(self):
        """Return the GUID of the most recently modified transcript, if any."""
        newest, newest_guid = 0, None
        for root in watched_roots():
            for path in root.rglob(PRIMARY_TRANSCRIPT):
                guid = extract_guid(path)
                if not guid:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest:
                    newest, newest_guid = mtime, guid
        return newest_guid

    @staticmethod
    def _file_size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _in_grace_period(self):
        """Suppress events during the startup grace period."""
        return (time.time() - self.start_time) < STARTUP_GRACE

    def _emit(self, event_name, guid, extra=None):
        """Emit an event unless we are still inside the startup grace period."""
        if self._in_grace_period():
            return
        emit_event(event_name, guid, conversation_cwd(guid, self.cwd), extra)

    def _on_file_activity(self, path):
        """Handle any supported conversation file modification or creation."""
        if not path_is_watched(path):
            return

        # transcript_full.jsonl duplicates transcript.jsonl byte for byte.
        # Acting on both would double every event.
        if is_transcript(path) and not is_primary_transcript(path):
            return

        guid = extract_guid(path)
        if not guid:
            return

        now = time.time()

        if guid not in self.conversations:
            # Brand new conversation — genuine session.start
            self.conversations[guid] = {
                "state": ACTIVE,
                "last_mod": now,
                "last_stop": 0,
                "transcript": is_primary_transcript(path),
            }
            self.last_active_guid = guid
            log.info(f"New session: {guid[:8]}")
            self._emit("SessionStart", guid)
            if is_primary_transcript(path):
                # A new transcript starts at 0, but the SessionStart above
                # already covers the opening prompt.
                self.offsets.setdefault(os.path.realpath(path), 0)
                self._consume_transcript(path, guid, suppress_prompt=True)
            return

        conv = self.conversations[guid]
        self.last_active_guid = guid

        if is_primary_transcript(path):
            conv["transcript"] = True
            conv["last_mod"] = now
            self._consume_transcript(path, guid)
            return

        # Legacy .pb/.db conversation with no transcript to read: the only
        # available signal is that the file changed at all.
        if conv.get("transcript"):
            return

        if conv["state"] == IDLE:
            # IDLE → ACTIVE: user sent a new message
            conv["state"] = ACTIVE
            conv["last_mod"] = now
            log.info(f"Agent activated: {guid[:8]}")
            self._emit("UserPromptSubmit", guid)

        elif conv["state"] == ACTIVE:
            # Still working — just update the timestamp
            conv["last_mod"] = now

    def _consume_transcript(self, path, guid, suppress_prompt=False):
        """Parse newly appended transcript records and emit their events."""
        for line in read_new_lines(path, self.offsets, str(path)):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if self._handle_record(guid, record, suppress_prompt=suppress_prompt):
                # Only the opening prompt is covered by SessionStart. A
                # later prompt in the same batch is a real new turn.
                suppress_prompt = False

    def _handle_record(self, guid, record, suppress_prompt=False):
        """
        Map one transcript record to an event.

        The turn boundary is explicit here: a PLANNER_RESPONSE that
        carries tool_calls is the agent dispatching work, and one that
        carries prose instead is its final answer.

        Returns True if the record was an explicit user prompt.
        """
        conv = self.conversations.get(guid)
        if conv is None:
            return False

        rtype = record.get("type")

        if rtype == "USER_INPUT":
            if record.get("source") != "USER_EXPLICIT":
                return False
            conv["state"] = ACTIVE
            conv["last_mod"] = time.time()
            conv["last_event"] = None
            self.pending_permission = False
            if not suppress_prompt:
                log.info(f"User prompt: {guid[:8]}")
                self._emit("UserPromptSubmit", guid)
            return True

        if rtype == "PLANNER_RESPONSE":
            conv["last_mod"] = time.time()
            if record.get("tool_calls"):
                # Dispatching tools — the turn continues.
                conv["state"] = ACTIVE
                conv["last_event"] = None
                return False
            if not str(record.get("content") or "").strip():
                # A response with neither tool calls nor text is a failed
                # generation. Antigravity emits one of these beside every
                # ERROR_MESSAGE while it retries a rate limit, and they
                # arrive in bursts. Not a completed turn.
                return False
            # Tool calls exhausted and there is prose to show: turn over.
            conv["state"] = IDLE
            conv["last_stop"] = time.time()
            conv["last_event"] = "Stop"
            self.pending_permission = False
            log.info(f"Agent done: {guid[:8]}")
            self._emit("Stop", guid)
            return False

        if rtype == "ERROR_MESSAGE":
            conv["last_mod"] = time.time()
            conv["state"] = IDLE
            if conv.get("last_event") == "PostToolUseFailure":
                # A rate-limited retry loop produces one of these every few
                # seconds. Sound the first, then stay quiet until something
                # else happens.
                return False
            conv["last_event"] = "PostToolUseFailure"
            log.info(f"Agent error: {guid[:8]}")
            self._emit("PostToolUseFailure", guid, {
                "tool_name": "Bash",
                "error": str(record.get("content") or "Agent error").strip()[:200],
            })
            return False

        if rtype in TOOL_RESULT_TYPES:
            # A tool returned. The agent is reading its own output, not
            # the user starting something new — no event, unless the
            # command itself failed.
            conv["state"] = ACTIVE
            conv["last_mod"] = time.time()
            conv["last_event"] = None
            self.pending_permission = False

            if rtype not in COMMAND_RESULT_TYPES:
                return False
            code = command_exit_code(record)
            if not code:
                return False
            # peon.sh gates task.error on a named tool plus an error
            # string, and labels the shell tool "Bash" for every IDE.
            conv["last_event"] = "PostToolUseFailure"
            log.info(f"Command failed ({code}): {guid[:8]}")
            self._emit("PostToolUseFailure", guid, {
                "tool_name": "Bash",
                "error": f"Exit code {code}",
            })

        return False

    def _on_log_activity(self, path):
        """
        Turn tool confirmation prompts in the CLI log into PermissionRequest.

        Antigravity flushes this log in batches, so a single read can contain
        several complete prompt-and-answer pairs that are already history. Only
        a batch that *ends* still waiting is worth a sound, and it is worth
        exactly one: the prompt currently on screen. Replaying each Surfacing
        line in a flushed backlog is what produced a burst of notifications for
        prompts answered minutes earlier.
        """
        lines = read_new_lines(path, self.offsets, str(path))
        if not lines:
            return

        was_pending = self.pending_permission
        for line in lines:
            if CONFIRM_RESPOND in line:
                self.pending_permission = False
            elif CONFIRM_SURFACE in line:
                self.pending_permission = True

        if not self.pending_permission or was_pending:
            return
        guid = self.last_active_guid
        if not guid:
            return
        log.info(f"Awaiting confirmation: {guid[:8]}")
        self._emit("PermissionRequest", guid)

    def poll_logs(self):
        """
        Read any CLI log that has grown, including one rotated in since start.

        Filesystem events are not reliable here: Antigravity holds the log open
        and flushes on its own schedule, and prompts went unnoticed for minutes
        while the watcher waited for a notification that arrived far too late.
        Polling a handful of small files once a second is cheaper than missing
        the prompt the user is staring at.

        Polling is also the only reader, deliberately. Handing the log to the
        observer as well would put two threads on one offset with a plain
        get-then-set between them, and buys nothing once this loop covers the
        file.
        """
        for path in log_paths():
            key = os.path.realpath(path)
            if key not in self.offsets:
                # First sight: start at the end. Whatever is already in the
                # file happened before we were watching.
                self.offsets[key] = self._file_size(path)
                continue
            self._on_log_activity(path)

    def on_modified(self, event):
        if not event.is_directory:
            self._on_file_activity(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._on_file_activity(event.src_path)

    def check_completions(self):
        """Poll all ACTIVE conversations for idle timeout → completion."""
        now = time.time()

        for guid, conv in self.conversations.items():
            if conv["state"] != ACTIVE:
                continue
            if conv["last_mod"] == 0:
                continue
            if conv.get("transcript"):
                # The transcript says when the turn ends. A long tool call
                # is not silence, so never guess from elapsed time here.
                continue

            elapsed = now - conv["last_mod"]
            if elapsed < IDLE_THRESHOLD:
                continue

            # Per-GUID cooldown — don't spam for the same agent
            since_last = now - conv["last_stop"]
            if since_last < PER_GUID_COOLDOWN:
                conv["state"] = IDLE
                continue

            # ACTIVE → IDLE with Stop event
            conv["state"] = IDLE
            if not self._in_grace_period():
                log.info(f"Agent done: {guid[:8]} (silent {elapsed:.0f}s)")
                self._emit("Stop", guid)
                conv["last_stop"] = now


def main():
    if Observer is None:
        log.error("Python 'watchdog' module not found. Install it: pip3 install watchdog")
        return 1

    cwd = os.getcwd()
    if "--cwd" in sys.argv:
        idx = sys.argv.index("--cwd")
        if idx + 1 < len(sys.argv):
            cwd = sys.argv[idx + 1]
    if cwd == "/":
        # A LaunchAgent starts at "/", which peon.sh cannot turn into a project
        # name, so it falls back to labelling the event "claude". Home at least
        # names the right person's machine. Only reached for a conversation
        # with no workspace recorded, such as one started outside a project.
        cwd = str(HOME)

    roots = watched_roots()
    if not roots:
        expected = ", ".join(str(p) for base in ANTIGRAVITY_DIRS for p in (base / "conversations", base / "brain"))
        log.info(f"Waiting for Antigravity conversation state directories: {expected}")
        while not roots:
            time.sleep(2)
            roots = watched_roots()

    handler = ConversationWatcher(cwd=cwd)

    log.info("Watching: " + ", ".join(str(root) for root in roots))
    log.info(f"Idle threshold: {IDLE_THRESHOLD}s")
    log.info(f"Grace period: {STARTUP_GRACE}s")

    observer_kind = os.environ.get("ANTIGRAVITY_OBSERVER", "").strip().lower()
    if observer_kind == "polling":
        observer = PollingObserver(timeout=CHECK_INTERVAL)
        log.info("Observer: polling")
    else:
        observer = Observer()
        log.info("Observer: native")

    for root in roots:
        observer.schedule(handler, str(root), recursive=True)

    observer.start()

    def shutdown(signum, frame):
        log.info("Shutting down...")
        observer.stop()
        observer.join()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            time.sleep(CHECK_INTERVAL)
            handler.check_completions()
            handler.poll_logs()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()

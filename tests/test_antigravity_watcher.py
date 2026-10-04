#!/usr/bin/env python3
"""
Smoke tests for antigravity-watcher.py

Verifies the ConversationWatcher state machine, event emission,
cooldown enforcement, and startup grace period logic.

Run: python3 tests/test_antigravity_watcher.py
  or: python3 -m pytest tests/test_antigravity_watcher.py -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from io import StringIO

# Add the adapters directory to the path so we can import the watcher
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "adapters"))


class TestEmitEvent(unittest.TestCase):
    """Verify JSON event output format."""

    def test_emit_event_produces_valid_json(self):
        """emit_event should write a single valid JSON line to stdout."""
        # Import here so path setup above takes effect
        from importlib import import_module
        watcher_mod = import_module("antigravity-watcher")

        captured = StringIO()
        with patch("sys.stdout", captured):
            watcher_mod.emit_event("Stop", "abc12345-full-guid", "/tmp/test")

        line = captured.getvalue().strip()
        data = json.loads(line)

        self.assertEqual(data["event"], "Stop")
        # session_id uses first 8 chars of GUID
        self.assertEqual(data["session_id"], "antigravity-abc12345")
        self.assertEqual(data["cwd"], "/tmp/test")

    def test_emit_event_session_start(self):
        """SessionStart event should have correct structure."""
        from importlib import import_module
        watcher_mod = import_module("antigravity-watcher")

        captured = StringIO()
        with patch("sys.stdout", captured):
            watcher_mod.emit_event("SessionStart", "deadbeef-1234", "/home/user")

        data = json.loads(captured.getvalue().strip())
        self.assertEqual(data["event"], "SessionStart")
        self.assertEqual(data["session_id"], "antigravity-deadbeef")


class TestConversationWatcher(unittest.TestCase):
    """Verify the per-GUID state machine logic."""

    def setUp(self):
        """Create a temp conversations directory."""
        self.tmpdir = tempfile.mkdtemp()
        self.brain_dir = os.path.join(self.tmpdir, "brain")
        os.makedirs(self.brain_dir, exist_ok=True)
        self.orig_dir = os.environ.get("ANTIGRAVITY_CONVERSATIONS_DIR")

        # Patch the module-level constant
        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self._orig_conv_dir = self.watcher_mod.CONVERSATIONS_DIR
        self._orig_brain_dir = self.watcher_mod.BRAIN_DIR
        self._orig_antigravity_dirs = self.watcher_mod.ANTIGRAVITY_DIRS
        self.watcher_mod.CONVERSATIONS_DIR = self.tmpdir
        self.watcher_mod.BRAIN_DIR = self.brain_dir
        self.watcher_mod.ANTIGRAVITY_DIRS = []

    def tearDown(self):
        self.watcher_mod.CONVERSATIONS_DIR = self._orig_conv_dir
        self.watcher_mod.BRAIN_DIR = self._orig_brain_dir
        self.watcher_mod.ANTIGRAVITY_DIRS = self._orig_antigravity_dirs
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_watcher(self, grace=0):
        """Create a ConversationWatcher with configurable grace period."""
        handler = self.watcher_mod.ConversationWatcher(cwd="/tmp/test")
        # Override startup grace to zero for immediate testing
        handler.start_time = time.time() - grace - 1
        return handler

    def test_new_conversation_emits_session_start(self):
        """A new .pb file should emit SessionStart."""
        handler = self._create_watcher(grace=30)

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            # Simulate a new .pb file creation
            guid = "test-guid-001"
            pb_path = os.path.join(self.tmpdir, f"{guid}.pb")
            open(pb_path, "w").close()

            handler._on_file_activity(pb_path)

        # Should have emitted SessionStart
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "SessionStart")
        self.assertEqual(events[0][1], guid)

    def test_new_db_conversation_emits_session_start(self):
        """A new .db file should emit SessionStart for newer Antigravity layouts."""
        handler = self._create_watcher(grace=30)

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            guid = "test-guid-db-001"
            db_path = os.path.join(self.tmpdir, f"{guid}.db")
            open(db_path, "w").close()

            handler._on_file_activity(db_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "SessionStart")
        self.assertEqual(events[0][1], guid)

    def test_transcript_path_extracts_brain_guid(self):
        """Transcript JSONL files should map to the brain conversation GUID."""
        handler = self._create_watcher(grace=30)

        guid = "test-guid-transcript-001"
        transcript_dir = os.path.join(
            self.brain_dir,
            guid,
            ".system_generated",
            "logs",
        )
        os.makedirs(transcript_dir, exist_ok=True)
        transcript_path = os.path.join(transcript_dir, "transcript.jsonl")
        open(transcript_path, "w").close()

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler._on_file_activity(transcript_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "SessionStart")
        self.assertEqual(events[0][1], guid)

    def test_known_idle_guid_emits_prompt_submit(self):
        """An IDLE → ACTIVE transition should emit UserPromptSubmit."""
        handler = self._create_watcher(grace=30)

        guid = "test-guid-002"
        pb_path = os.path.join(self.tmpdir, f"{guid}.pb")
        open(pb_path, "w").close()

        # Pre-register as idle
        handler.conversations[guid] = {
            "state": "idle",
            "last_mod": 0,
            "last_stop": 0,
        }

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler._on_file_activity(pb_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "UserPromptSubmit")

    def test_active_guid_no_event(self):
        """An ACTIVE → ACTIVE modification should NOT emit any event."""
        handler = self._create_watcher(grace=30)

        guid = "test-guid-003"
        pb_path = os.path.join(self.tmpdir, f"{guid}.pb")
        open(pb_path, "w").close()

        handler.conversations[guid] = {
            "state": "active",
            "last_mod": time.time(),
            "last_stop": 0,
        }

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler._on_file_activity(pb_path)

        self.assertEqual(len(events), 0)

    def test_idle_timeout_emits_stop(self):
        """ACTIVE conversation past idle threshold should emit Stop."""
        handler = self._create_watcher(grace=30)

        guid = "test-guid-004"
        handler.conversations[guid] = {
            "state": "active",
            "last_mod": time.time() - self.watcher_mod.IDLE_THRESHOLD - 5,
            "last_stop": 0,
        }

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler.check_completions()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "Stop")
        # State should be idle now
        self.assertEqual(handler.conversations[guid]["state"], "idle")

    def test_cooldown_suppresses_duplicate_stop(self):
        """Stop should be suppressed if within per-GUID cooldown."""
        handler = self._create_watcher(grace=30)

        guid = "test-guid-005"
        handler.conversations[guid] = {
            "state": "active",
            "last_mod": time.time() - self.watcher_mod.IDLE_THRESHOLD - 5,
            "last_stop": time.time() - 5,  # 5s ago, within 30s cooldown
        }

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler.check_completions()

        # No Stop event due to cooldown
        self.assertEqual(len(events), 0)
        # But state should still transition to idle
        self.assertEqual(handler.conversations[guid]["state"], "idle")

    def test_startup_grace_suppresses_events(self):
        """Events during startup grace period should be suppressed."""
        handler = self.watcher_mod.ConversationWatcher(cwd="/tmp/test")
        # Do NOT override start_time — grace is active

        guid = "test-guid-006"
        pb_path = os.path.join(self.tmpdir, f"{guid}.pb")
        open(pb_path, "w").close()

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler._on_file_activity(pb_path)

        # No event during grace period
        self.assertEqual(len(events), 0)
        # But the conversation should still be registered
        self.assertIn(guid, handler.conversations)

    def test_pre_registers_existing_pb_files(self):
        """Existing .pb files at startup should be pre-registered as idle."""
        guid = "existing-guid-007"
        pb_path = os.path.join(self.tmpdir, f"{guid}.pb")
        open(pb_path, "w").close()

        handler = self._create_watcher(grace=30)

        self.assertIn(guid, handler.conversations)
        self.assertEqual(handler.conversations[guid]["state"], "idle")

    def test_pre_registers_existing_db_files(self):
        """Existing .db files at startup should be pre-registered as idle."""
        guid = "existing-guid-db-008"
        db_path = os.path.join(self.tmpdir, f"{guid}.db")
        open(db_path, "w").close()

        handler = self._create_watcher(grace=30)

        self.assertIn(guid, handler.conversations)
        self.assertEqual(handler.conversations[guid]["state"], "idle")

    def test_pre_registers_existing_transcript_files(self):
        """Existing transcript files should be pre-registered by brain GUID."""
        guid = "existing-guid-transcript-009"
        transcript_dir = os.path.join(
            self.brain_dir,
            guid,
            ".system_generated",
            "logs",
        )
        os.makedirs(transcript_dir, exist_ok=True)
        open(os.path.join(transcript_dir, "transcript_full.jsonl"), "w").close()

        handler = self._create_watcher(grace=30)

        self.assertIn(guid, handler.conversations)
        self.assertEqual(handler.conversations[guid]["state"], "idle")

    def test_non_pb_files_ignored(self):
        """Unsupported files should be completely ignored."""
        handler = self._create_watcher(grace=30)

        events = []
        with patch.object(self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)):
            handler._on_file_activity("/tmp/not-a-proto.txt")

        self.assertEqual(len(events), 0)


if __name__ == "__main__":
    unittest.main()


class TestReadNewLines(unittest.TestCase):
    """Verify the incremental offset reader used for transcripts and logs."""

    def setUp(self):
        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "transcript.jsonl")
        self.offsets = {}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _append(self, text):
        with open(self.path, "a") as fh:
            fh.write(text)

    def test_reads_only_new_lines(self):
        """A second call should return only what was appended since the first."""
        self._append("one\ntwo\n")
        first = self.watcher_mod.read_new_lines(self.path, self.offsets, self.path)
        self.assertEqual(first, ["one", "two"])

        self._append("three\n")
        second = self.watcher_mod.read_new_lines(self.path, self.offsets, self.path)
        self.assertEqual(second, ["three"])

    def test_partial_line_is_withheld_until_complete(self):
        """A line still being written must not be parsed half-formed."""
        self._append('{"type": "USER_IN')
        self.assertEqual(
            self.watcher_mod.read_new_lines(self.path, self.offsets, self.path), []
        )

        self._append('PUT"}\n')
        self.assertEqual(
            self.watcher_mod.read_new_lines(self.path, self.offsets, self.path),
            ['{"type": "USER_INPUT"}'],
        )

    def test_truncation_resets_offset(self):
        """A shrunken file was rotated, so reading restarts from the top."""
        self._append("old-line-one\nold-line-two\n")
        self.watcher_mod.read_new_lines(self.path, self.offsets, self.path)

        with open(self.path, "w") as fh:
            fh.write("fresh\n")

        self.assertEqual(
            self.watcher_mod.read_new_lines(self.path, self.offsets, self.path),
            ["fresh"],
        )

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            self.watcher_mod.read_new_lines(
                os.path.join(self.tmpdir, "gone.jsonl"), self.offsets, "gone"
            ),
            [],
        )


class TestTranscriptEvents(unittest.TestCase):
    """Verify transcript records drive events instead of file mtime."""

    GUID = "transcript-guid-001"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.brain_dir = os.path.join(self.tmpdir, "brain")
        self.logs_dir = os.path.join(
            self.brain_dir, self.GUID, ".system_generated", "logs"
        )
        os.makedirs(self.logs_dir, exist_ok=True)
        self.transcript = os.path.join(self.logs_dir, "transcript.jsonl")

        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self._orig = (
            self.watcher_mod.CONVERSATIONS_DIR,
            self.watcher_mod.BRAIN_DIR,
            self.watcher_mod.ANTIGRAVITY_DIRS,
        )
        self.watcher_mod.CONVERSATIONS_DIR = self.tmpdir
        self.watcher_mod.BRAIN_DIR = self.brain_dir
        self.watcher_mod.ANTIGRAVITY_DIRS = [self.watcher_mod.Path(self.tmpdir)]

    def tearDown(self):
        (
            self.watcher_mod.CONVERSATIONS_DIR,
            self.watcher_mod.BRAIN_DIR,
            self.watcher_mod.ANTIGRAVITY_DIRS,
        ) = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _watcher(self):
        """Build a watcher with the transcript already registered and grace over."""
        handler = self.watcher_mod.ConversationWatcher(cwd="/tmp/test")
        handler.start_time = time.time() - self.watcher_mod.STARTUP_GRACE - 1
        return handler

    def _append(self, *records):
        with open(self.transcript, "a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def _drain(self, handler, path=None):
        """Feed appended records to the watcher and return emitted events."""
        events = []
        with patch.object(
            self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)
        ):
            handler._on_file_activity(path or self.transcript)
        return events

    @staticmethod
    def _user(text="hi"):
        return {"type": "USER_INPUT", "source": "USER_EXPLICIT",
                "status": "DONE", "content": text}

    @staticmethod
    def _planner(tool_calls=None, content=None):
        return {"type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
                "tool_calls": tool_calls or [], "content": content}

    def test_user_input_emits_prompt_submit(self):
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(self._user())
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["UserPromptSubmit"])
        self.assertEqual(handler.conversations[self.GUID]["state"], "active")

    def test_planner_response_with_tool_calls_emits_nothing(self):
        """The agent dispatching a tool is mid-turn, not finished."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(self._planner(tool_calls=[{"name": "RunCommand"}]))
        self.assertEqual(self._drain(handler), [])
        self.assertEqual(handler.conversations[self.GUID]["state"], "active")

    def test_tool_result_does_not_emit_prompt_submit(self):
        """Tool output landing in the transcript is not a new user prompt."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(
            {"type": "GENERIC", "source": "MODEL", "status": "DONE"},
            {"type": "RUN_COMMAND", "source": "MODEL", "status": "DONE"},
            {"type": "VIEW_FILE", "source": "MODEL", "status": "DONE"},
        )
        self.assertEqual(self._drain(handler), [])

    def test_final_planner_response_emits_stop(self):
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(self._planner(content="Here is the answer."))
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["Stop"])
        self.assertEqual(handler.conversations[self.GUID]["state"], "idle")

    def test_error_message_emits_tool_failure(self):
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append({"type": "ERROR_MESSAGE", "source": "SYSTEM",
                      "status": "DONE", "content": "429 rate limit"})
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["PostToolUseFailure"])

    def test_full_turn_emits_exactly_two_events(self):
        """A prompt with three tool calls should sound twice, not eight times."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(
            self._user(),
            self._planner(tool_calls=[{"name": "RunCommand"}]),
            {"type": "GENERIC", "source": "MODEL", "status": "DONE"},
            self._planner(tool_calls=[{"name": "ViewFile"}]),
            {"type": "VIEW_FILE", "source": "MODEL", "status": "DONE"},
            self._planner(tool_calls=[{"name": "GrepSearch"}]),
            {"type": "GREP_SEARCH", "source": "MODEL", "status": "DONE"},
            self._planner(content="Done."),
        )
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["UserPromptSubmit", "Stop"])

    def test_transcript_full_is_ignored(self):
        """transcript_full.jsonl duplicates transcript.jsonl — parsing both doubles events."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        duplicate = os.path.join(self.logs_dir, "transcript_full.jsonl")
        with open(duplicate, "w") as fh:
            fh.write(json.dumps(self._user()) + "\n")

        self.assertEqual(self._drain(handler, duplicate), [])

    def test_transcript_conversation_ignores_idle_timeout(self):
        """A long tool call must never be mistaken for completion."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        handler.conversations[self.GUID] = {
            "state": "active",
            "last_mod": time.time() - self.watcher_mod.IDLE_THRESHOLD - 60,
            "last_stop": 0,
            "transcript": True,
        }

        events = []
        with patch.object(
            self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)
        ):
            handler.check_completions()

        self.assertEqual(events, [])
        self.assertEqual(handler.conversations[self.GUID]["state"], "active")

    def test_startup_seeds_last_active_from_newest_transcript(self):
        """A watcher started mid-session can still attribute a permission prompt."""
        self._append(self._user())
        handler = self._watcher()
        self.assertEqual(handler.last_active_guid, self.GUID)

    def test_startup_does_not_replay_existing_transcript(self):
        """Pre-existing records are history; they must not fire on startup."""
        self._append(self._user(), self._planner(content="old answer"))
        handler = self._watcher()

        self._append(self._user("new question"))
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["UserPromptSubmit"])


class TestToolConfirmation(unittest.TestCase):
    """Verify cli.log tool confirmation prompts become PermissionRequest."""

    GUID = "confirm-guid-001"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.brain_dir = os.path.join(self.tmpdir, "brain")
        os.makedirs(self.brain_dir, exist_ok=True)
        self.cli_log = os.path.join(self.tmpdir, "cli.log")
        open(self.cli_log, "w").close()

        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self._orig = (
            self.watcher_mod.CONVERSATIONS_DIR,
            self.watcher_mod.BRAIN_DIR,
            self.watcher_mod.ANTIGRAVITY_DIRS,
        )
        self.watcher_mod.CONVERSATIONS_DIR = self.tmpdir
        self.watcher_mod.BRAIN_DIR = self.brain_dir
        self.watcher_mod.ANTIGRAVITY_DIRS = [self.watcher_mod.Path(self.tmpdir)]

        self.handler = self.watcher_mod.ConversationWatcher(cwd="/tmp/test")
        self.handler.start_time = time.time() - self.watcher_mod.STARTUP_GRACE - 1
        self.handler.last_active_guid = self.GUID

    def tearDown(self):
        (
            self.watcher_mod.CONVERSATIONS_DIR,
            self.watcher_mod.BRAIN_DIR,
            self.watcher_mod.ANTIGRAVITY_DIRS,
        ) = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _log(self, line):
        with open(self.cli_log, "a") as fh:
            fh.write(line + "\n")

    def _drain(self):
        """Polling is the only reader of the CLI log; see poll_logs."""
        events = []
        with patch.object(
            self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)
        ):
            self.handler._on_log_activity(self.cli_log)
        return events

    def test_surfacing_emits_permission_request(self):
        self._log('tool_confirmation_manager.go:197] Surfacing tool confirmation: "RunCommand" at step 28')
        events = self._drain()

        self.assertEqual([e[0] for e in events], ["PermissionRequest"])
        self.assertEqual(events[0][1], self.GUID)
        self.assertTrue(self.handler.pending_permission)

    def test_repeated_surfacing_only_sounds_once(self):
        """Antigravity re-surfaces the same prompt while it waits for an answer."""
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        events = self._drain()

        self.assertEqual([e[0] for e in events], ["PermissionRequest"])

    def test_responding_clears_pending(self):
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        self._drain()

        self._log("input_loop.go:591] Responding to tool confirmation: "
                  "convID=abc, stepIdx=28, approved=true")
        self.assertEqual(self._drain(), [])
        self.assertFalse(self.handler.pending_permission)

        self._log('Surfacing tool confirmation: "RunCommand" at step 30')
        self.assertEqual([e[0] for e in self._drain()], ["PermissionRequest"])

    def test_no_active_conversation_emits_nothing(self):
        """With nothing to attribute the prompt to, stay silent."""
        self.handler.last_active_guid = None
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')

        self.assertEqual(self._drain(), [])

    def test_unrelated_log_lines_ignored(self):
        self._log("I0905 16:56:25.099705 313 something_else.go:12] Nothing to see")
        self.assertEqual(self._drain(), [])


class TestCommandFailures(unittest.TestCase):
    """Verify a failing shell command sounds task.error, as it does in Claude Code."""

    def setUp(self):
        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")

    def test_exit_code_field_is_read(self):
        """Older Antigravity builds put the code in an exit_code field."""
        self.assertEqual(
            self.watcher_mod.command_exit_code({"type": "RUN_COMMAND", "exit_code": 1}), 1
        )
        self.assertEqual(
            self.watcher_mod.command_exit_code({"type": "RUN_COMMAND", "exit_code": 0}), 0
        )

    def test_exit_code_is_read_from_content(self):
        """Newer builds only state the code in the record's prose."""
        record = {
            "type": "GENERIC",
            "content": "Created At: 2026-09-05T16:25:24-04:00\n"
                       "The command exited with code 128.\nOutput:\nfatal: not a git repo",
        }
        self.assertEqual(self.watcher_mod.command_exit_code(record), 128)

    def test_no_exit_code_returns_none(self):
        self.assertIsNone(
            self.watcher_mod.command_exit_code({"type": "VIEW_FILE", "content": "some file"})
        )

    def test_emit_event_carries_extra_fields(self):
        """peon.sh gates task.error on tool_name plus a non-empty error."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            self.watcher_mod.emit_event(
                "PostToolUseFailure", "abc12345-guid", "/tmp",
                {"tool_name": "Bash", "error": "Exit code 1"},
            )

        data = json.loads(captured.getvalue().strip())
        self.assertEqual(data["tool_name"], "Bash")
        self.assertEqual(data["error"], "Exit code 1")


class TestCommandFailureEvents(TestTranscriptEvents):
    """Command failures inside a turn, driven through the real state machine."""

    def test_failed_command_emits_tool_failure(self):
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append({"type": "RUN_COMMAND", "source": "MODEL", "status": "DONE",
                      "exit_code": 1, "content": "The command exited with code 1."})
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["PostToolUseFailure"])
        self.assertEqual(events[0][3], {"tool_name": "Bash", "error": "Exit code 1"})

    def test_successful_command_is_silent(self):
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append({"type": "GENERIC", "source": "MODEL", "status": "DONE",
                      "content": "The command exited with code 0.\nOutput:\nok"})
        self.assertEqual(self._drain(handler), [])

    def test_turn_with_one_failure_sounds_three_times(self):
        """Acknowledge, error, complete — the same shape Claude Code produces."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(
            self._user(),
            self._planner(tool_calls=[{"name": "RunCommand"}]),
            {"type": "GENERIC", "source": "MODEL", "status": "DONE",
             "content": "The command exited with code 1.\nOutput:\nboom"},
            self._planner(tool_calls=[{"name": "RunCommand"}]),
            {"type": "GENERIC", "source": "MODEL", "status": "DONE",
             "content": "The command exited with code 0.\nOutput:\nfixed"},
            self._planner(content="Fixed it."),
        )
        events = self._drain(handler)

        self.assertEqual(
            [e[0] for e in events],
            ["UserPromptSubmit", "PostToolUseFailure", "Stop"],
        )

    def test_empty_planner_response_is_not_completion(self):
        """A failed generation has neither tool calls nor prose. Not a turn end."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append(self._planner(content=None))
        self.assertEqual(self._drain(handler), [])

    def test_rate_limit_retry_loop_sounds_once(self):
        """Antigravity retries a 429 in a tight loop; one error sound is enough."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        for _ in range(5):
            self._append(
                {"type": "ERROR_MESSAGE", "source": "SYSTEM", "status": "DONE",
                 "content": "429 rate limit"},
                self._planner(content=None),
            )
        events = self._drain(handler)

        self.assertEqual([e[0] for e in events], ["PostToolUseFailure"])

    def test_error_sounds_again_after_progress(self):
        """A later, unrelated failure is not swallowed by the earlier one."""
        open(self.transcript, "w").close()
        handler = self._watcher()

        self._append({"type": "ERROR_MESSAGE", "source": "SYSTEM",
                      "status": "DONE", "content": "first"})
        self._drain(handler)

        self._append(
            self._user("try again"),
            {"type": "ERROR_MESSAGE", "source": "SYSTEM",
             "status": "DONE", "content": "second"},
        )
        events = self._drain(handler)

        self.assertEqual(
            [e[0] for e in events], ["UserPromptSubmit", "PostToolUseFailure"]
        )


class TestLogPaths(unittest.TestCase):
    """Antigravity's cli.log is a symlink into log/, retargeted every launch."""

    def setUp(self):
        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self.tmpdir = tempfile.mkdtemp()
        self.logdir = os.path.join(self.tmpdir, "log")
        os.makedirs(self.logdir, exist_ok=True)
        self._orig = self.watcher_mod.ANTIGRAVITY_DIRS
        self.watcher_mod.ANTIGRAVITY_DIRS = [self.watcher_mod.Path(self.tmpdir)]

    def tearDown(self):
        self.watcher_mod.ANTIGRAVITY_DIRS = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _rotate_to(self, name):
        """Create log/<name> and point cli.log at it, as Antigravity does."""
        target = os.path.join(self.logdir, name)
        open(target, "a").close()
        link = os.path.join(self.tmpdir, "cli.log")
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(os.path.join("log", name), link)
        return target

    def test_symlinked_log_resolves_to_its_target(self):
        """Watching the symlink's own directory never sees writes one level down."""
        target = self._rotate_to("cli-20260905_165414.log")
        paths = [str(p) for p in self.watcher_mod.log_paths()]

        self.assertIn(os.path.realpath(target), [os.path.realpath(p) for p in paths])
        self.assertNotIn(os.path.join(self.tmpdir, "cli.log"), paths)

    def test_symlink_and_target_are_not_double_counted(self):
        """cli.log and log/cli-*.log are the same file; reading both doubles events."""
        self._rotate_to("cli-20260905_165414.log")
        paths = self.watcher_mod.log_paths()
        self.assertEqual(len(paths), 1)

    def test_rotation_exposes_the_new_target(self):
        self._rotate_to("cli-20260905_165414.log")
        second = self._rotate_to("cli-20260905_170000.log")

        reals = [os.path.realpath(str(p)) for p in self.watcher_mod.log_paths()]
        self.assertIn(os.path.realpath(second), reals)

    def test_is_log_matches_rotated_names(self):
        self.assertTrue(self.watcher_mod.is_log("/x/log/cli-20260905_165414.log"))
        self.assertTrue(self.watcher_mod.is_log("/x/cli.log"))
        self.assertFalse(self.watcher_mod.is_log("/x/transcript.jsonl"))


class TestFlushedLogBatches(TestToolConfirmation):
    """Antigravity flushes its log in batches, often long after the prompt."""

    def test_batch_of_answered_prompts_is_silent(self):
        """Three prompts already answered are history, not something to announce."""
        for step in (28, 30, 32):
            self._log(f'Surfacing tool confirmation: "RunCommand" at step {step}')
            self._log(f"Responding to tool confirmation: convID=x, stepIdx={step}, approved=true")

        self.assertEqual(self._drain(), [])
        self.assertFalse(self.handler.pending_permission)

    def test_batch_ending_unanswered_sounds_once(self):
        """Only the prompt still on screen matters, and only once."""
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        self._log("Responding to tool confirmation: convID=x, stepIdx=28, approved=true")
        self._log('Surfacing tool confirmation: "RunCommand" at step 30')
        self._log("Responding to tool confirmation: convID=x, stepIdx=30, approved=true")
        self._log('Surfacing tool confirmation: "RunCommand" at step 32')

        events = self._drain()
        self.assertEqual([e[0] for e in events], ["PermissionRequest"])
        self.assertTrue(self.handler.pending_permission)

    def test_already_pending_batch_does_not_re_sound(self):
        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        self.assertEqual([e[0] for e in self._drain()], ["PermissionRequest"])

        self._log('Surfacing tool confirmation: "RunCommand" at step 28')
        self.assertEqual(self._drain(), [])

    def test_poll_logs_skips_backlog_on_first_sight(self):
        """A log rotated in after startup must not replay what predates us."""
        self._log('Surfacing tool confirmation: "RunCommand" at step 1')
        self.handler.offsets.pop(os.path.realpath(self.cli_log), None)

        events = []
        with patch.object(
            self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)
        ):
            self.handler.poll_logs()
        self.assertEqual(events, [])

        self._log('Surfacing tool confirmation: "RunCommand" at step 2')
        with patch.object(
            self.watcher_mod, "emit_event", side_effect=lambda *a: events.append(a)
        ):
            self.handler.poll_logs()
        self.assertEqual([e[0] for e in events], ["PermissionRequest"])


class TestConversationCwd(unittest.TestCase):
    """The daemon's own cwd is /, so events must name the agent's workspace."""

    def setUp(self):
        from importlib import import_module
        self.watcher_mod = import_module("antigravity-watcher")
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmpdir, "cache"), exist_ok=True)
        self._orig = self.watcher_mod.ANTIGRAVITY_DIRS
        self.watcher_mod.ANTIGRAVITY_DIRS = [self.watcher_mod.Path(self.tmpdir)]

    def tearDown(self):
        self.watcher_mod.ANTIGRAVITY_DIRS = self._orig
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cache(self, mapping):
        p = os.path.join(self.tmpdir, "cache", "last_conversations.json")
        with open(p, "w") as f:
            json.dump(mapping, f)

    def _conversation_db(self, guid, workspace):
        """Write a conversation db shaped like Antigravity's own."""
        import sqlite3
        convdir = os.path.join(self.tmpdir, "conversations")
        os.makedirs(convdir, exist_ok=True)
        con = sqlite3.connect(os.path.join(convdir, f"{guid}.db"))
        con.execute("create table trajectory_metadata_blob (id text, data blob)")
        con.execute(
            "insert into trajectory_metadata_blob values (?, ?)",
            ("main", b"\x0a\x2f" + f"file://{workspace}".encode() + b"\x12\x00"),
        )
        con.commit()
        con.close()

    def test_conversation_db_beats_the_recent_cache(self):
        """The db is per conversation; the cache only knows the newest one."""
        self._conversation_db("old-guid", "/Users/x/archived")
        self._cache({"/Users/x/current": "new-guid"})

        self.assertEqual(
            self.watcher_mod.conversation_cwd("old-guid", "/"), "/Users/x/archived"
        )

    def test_resumed_conversation_absent_from_cache_still_resolves(self):
        """A session resumed from weeks ago is exactly what the cache misses."""
        self._conversation_db("ancient-guid", "/Users/x/long-ago")
        self._cache({"/Users/x/current": "todays-guid"})

        self.assertEqual(
            self.watcher_mod.conversation_cwd("ancient-guid", "/"), "/Users/x/long-ago"
        )

    def test_cache_covers_a_conversation_with_no_db_yet(self):
        self._cache({"/Users/x/fresh": "brand-new-guid"})
        self.assertEqual(
            self.watcher_mod.conversation_cwd("brand-new-guid", "/"), "/Users/x/fresh"
        )

    def test_unreadable_db_falls_through(self):
        convdir = os.path.join(self.tmpdir, "conversations")
        os.makedirs(convdir, exist_ok=True)
        with open(os.path.join(convdir, "junk-guid.db"), "wb") as f:
            f.write(b"not a database")
        self._cache({"/Users/x/fallback": "junk-guid"})

        self.assertEqual(
            self.watcher_mod.conversation_cwd("junk-guid", "/"), "/Users/x/fallback"
        )

    def test_maps_conversation_to_its_workspace(self):
        self._cache({"/Users/x/config": "abc-guid", "/Users/x/other": "def-guid"})
        self.assertEqual(
            self.watcher_mod.conversation_cwd("def-guid", "/"), "/Users/x/other"
        )

    def test_unknown_conversation_falls_back(self):
        """The cache holds only the newest conversation per workspace."""
        self._cache({"/Users/x/config": "abc-guid"})
        self.assertEqual(
            self.watcher_mod.conversation_cwd("older-guid", "/fallback"), "/fallback"
        )

    def test_legacy_completion_keeps_the_conversation_workspace(self):
        """An idle completion must retain the workspace used by the prompt."""
        import io

        self._cache({"/tmp/agent-workspace": "legacy-guid"})
        convdir = os.path.join(self.tmpdir, "conversations")
        os.makedirs(convdir, exist_ok=True)
        conversation = os.path.join(convdir, "legacy-guid.pb")
        with open(conversation, "wb") as handle:
            handle.write(b"state")

        captured = io.StringIO()
        with patch.object(self.watcher_mod, "CONVERSATIONS_DIR", convdir), \
             patch.object(self.watcher_mod, "BRAIN_DIR", os.path.join(self.tmpdir, "brain")), \
             patch.object(self.watcher_mod, "STARTUP_GRACE", 0), \
             patch("sys.stdout", captured):
            watcher = self.watcher_mod.ConversationWatcher(cwd="/tmp/daemon-workspace")
            watcher._on_file_activity(conversation)
            watcher.conversations["legacy-guid"]["last_mod"] = time.time() - 100
            watcher.check_completions()

        events = [json.loads(line) for line in captured.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["UserPromptSubmit", "Stop"])
        self.assertEqual([event["cwd"] for event in events],
                         ["/tmp/agent-workspace", "/tmp/agent-workspace"])

    def test_missing_or_corrupt_cache_falls_back(self):
        self.assertEqual(self.watcher_mod.conversation_cwd("g", "/fallback"), "/fallback")
        with open(os.path.join(self.tmpdir, "cache", "last_conversations.json"), "w") as f:
            f.write("not json")
        self.assertEqual(self.watcher_mod.conversation_cwd("g", "/fallback"), "/fallback")

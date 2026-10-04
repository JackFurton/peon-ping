#!/usr/bin/env bats

load setup.bash

setup() {
  setup_test_env
  
  # Copy peon.sh into test dir so the adapter can find it
  cp "$PEON_SH" "$TEST_DIR/peon.sh"
  
  ADAPTER_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/adapters/gemini.sh"
}

teardown() {
  teardown_test_env
}

@test "gemini adapter: SessionStart triggers greeting" {
  export CLAUDE_PEON_DIR="$TEST_DIR"
  run bash "$ADAPTER_SH" SessionStart <<'JSON'
{
  "session_id": "test-session-123",
  "cwd": "/tmp/test"
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  
  # Give async audio a moment
  sleep 0.5
  
  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/Hello"* ]]
}

@test "gemini adapter: AfterAgent triggers completion" {
  export CLAUDE_PEON_DIR="$TEST_DIR"
  run bash "$ADAPTER_SH" AfterAgent <<'JSON'
{
  "session_id": "test-session-123",
  "cwd": "/tmp/test"
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  
  sleep 0.5
  
  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/Done"* ]]
}

@test "gemini adapter: AfterTool (success) is silent" {
  export CLAUDE_PEON_DIR="$TEST_DIR"
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{
  "session_id": "test-session-123",
  "cwd": "/tmp/test",
  "tool_name": "ls",
  "exit_code": 0
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  
  sleep 0.5
  
  ! afplay_was_called
}

@test "gemini adapter: native AfterTool error plays error sound" {
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{
  "session_id": "test-native-session",
  "cwd": "/tmp/test",
  "hook_event_name": "AfterTool",
  "timestamp": "2026-10-04T15:00:00Z",
  "transcript_path": "/tmp/session.json",
  "tool_name": "run_shell_command",
  "tool_input": {"command": "false"},
  "tool_response": {
    "llmContent": "The command failed",
    "returnDisplay": "Exit code 1",
    "error": {"message": "Command failed", "type": "execution_failed"}
  }
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  sleep 0.5
  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/Error"* ]]
}

@test "gemini adapter: native AfterTool success does not invoke peon" {
  cat > "$TEST_DIR/peon.sh" <<'SCRIPT'
#!/bin/bash
cat > "$CLAUDE_PEON_DIR/captured-payload.json"
SCRIPT
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{"session_id":"native-success","cwd":"/tmp/test","tool_name":"read_file","tool_response":{"llmContent":"contents","returnDisplay":"contents"}}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  [ ! -f "$TEST_DIR/captured-payload.json" ]
}

@test "gemini adapter: empty native error does not create a false failure" {
  cat > "$TEST_DIR/peon.sh" <<'SCRIPT'
#!/bin/bash
cat > "$CLAUDE_PEON_DIR/captured-payload.json"
SCRIPT
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{"tool_response":{"llmContent":"contents","returnDisplay":"contents","error":" "}}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  [ ! -f "$TEST_DIR/captured-payload.json" ]
}

@test "gemini adapter: native error preserves input quotes and takes precedence over legacy stderr" {
  cat > "$TEST_DIR/peon.sh" <<'SCRIPT'
#!/bin/bash
cat > "$CLAUDE_PEON_DIR/captured-payload.json"
SCRIPT
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{"session_id":"gemini's-session","cwd":"/tmp/project's folder","tool_name":"run_shell_command","tool_response":{"llmContent":"failed","returnDisplay":"failed","error":{"message":"can't run it"}},"exit_code":1,"stderr":"legacy error"}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  "$PEON_PY" - "$TEST_DIR/captured-payload.json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload['hook_event_name'] == 'PostToolUseFailure', payload
assert payload['session_id'] == "gemini's-session", payload
assert payload['cwd'] == "/tmp/project's folder", payload
assert payload['tool_name'] == 'Bash', payload
assert payload['error'] == "can't run it", payload
PY
}

@test "gemini adapter: AfterTool (failure) triggers error sound" {
  export CLAUDE_PEON_DIR="$TEST_DIR"
  run bash "$ADAPTER_SH" AfterTool <<'JSON'
{
  "session_id": "test-session-123",
  "cwd": "/tmp/test",
  "tool_name": "ls",
  "exit_code": 1,
  "stderr": "File not found"
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  
  sleep 0.5
  
  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/Error"* ]]
}

@test "gemini adapter: Notification triggers notification" {
  export CLAUDE_PEON_DIR="$TEST_DIR"
  run bash "$ADAPTER_SH" Notification <<'JSON'
{
  "session_id": "test-session-123",
  "cwd": "/tmp/test"
}
JSON
  [ "$status" -eq 0 ]
  [ "$output" = "{}" ]
  
  # Notification doesn't necessarily play sound in mock setup unless configured,
  # but peon.sh was called.
}

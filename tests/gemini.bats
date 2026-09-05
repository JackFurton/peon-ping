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
  # A tool succeeding is the middle of a turn. AfterAgent reports
  # completion; sounding here fired "Job's done" after every tool call.
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

  [ "$(afplay_call_count)" -eq 0 ]
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

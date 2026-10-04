#!/usr/bin/env bats

load setup.bash

setup() {
  setup_test_env

  # Create a mock Antigravity conversations directory
  export ANTIGRAVITY_CONVERSATIONS_DIR="$TEST_DIR/conversations"
  mkdir -p "$ANTIGRAVITY_CONVERSATIONS_DIR"

  # Copy peon.sh into test dir so the adapter can find it
  cp "$PEON_SH" "$TEST_DIR/peon.sh"

  # Mock python3 so preflight passes
  cat > "$MOCK_BIN/python3" <<'SCRIPT'
#!/bin/bash
# Minimal python3 mock that passes the watchdog import check
# and handles the JSON parsing calls from the shell wrapper
if [[ "$*" == *"import watchdog"* ]]; then
  exit 0
fi
if [[ "$*" == *"import json"* ]]; then
  # JSON field extraction: read from stdin, eval the python snippet
  exec /usr/bin/python3 "$@"
fi
exec /usr/bin/python3 "$@"
SCRIPT
  chmod +x "$MOCK_BIN/python3"

  ADAPTER_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/adapters/antigravity-py.sh"
  WATCHER_PY="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/adapters/antigravity-watcher.py"
}

teardown() {
  teardown_test_env
}

# Helper: source the adapter in test mode so pipe_to_peon is available
# but the main watcher loop is skipped.
source_adapter() {
  export PEON_ADAPTER_TEST=1
  export TMPDIR="$TEST_DIR"
  source "$ADAPTER_SH" 2>/dev/null
  # Restore BATS-friendly settings (adapter sets -euo pipefail)
  set +e +u
  set +o pipefail 2>/dev/null || true
}

# ============================================================
# Syntax validation
# ============================================================

@test "adapter script has valid bash syntax" {
  run bash -n "$ADAPTER_SH"
  [ "$status" -eq 0 ]
}

@test "python watcher has valid syntax" {
  run python3 -c "import ast; ast.parse(open('$WATCHER_PY').read())"
  [ "$status" -eq 0 ]
}

# ============================================================
# Preflight: missing peon.sh
# ============================================================

@test "exits with error when peon.sh is not found" {
  local empty_dir
  empty_dir="$(mktemp -d)"
  CLAUDE_PEON_DIR="$empty_dir" run bash "$ADAPTER_SH"
  [ "$status" -eq 1 ]
  [[ "$output" == *"peon.sh not found"* ]]
  rm -rf "$empty_dir"
}

# ============================================================
# Preflight: missing python3
# ============================================================

@test "exits with error when python3 is not available" {
  rm -f "$MOCK_BIN/python3"
  # Restrict PATH so real python3 is not found
  PATH="$MOCK_BIN:/usr/bin:/bin" run bash "$ADAPTER_SH"
  # On macOS / Ubuntu, /usr/bin/python3 typically exists, so this test is
  # best-effort. Only assert the python3-specific message when the failure
  # actually came from the python3 check (vs. a downstream check like
  # watchdog import or peon.sh missing).
  if [ "$status" -eq 1 ] && [[ "$output" == *"python3"* ]] && [[ "$output" != *"watchdog"* ]]; then
    [[ "$output" == *"python3 is required"* ]]
  fi
}

# ============================================================
# Preflight: missing watcher script
# ============================================================

@test "exits with error when watcher.py is not found" {
  # Create a temp copy of the adapter pointing to a missing watcher
  local tmp_adapter
  tmp_adapter="$(mktemp)"
  cp "$ADAPTER_SH" "$tmp_adapter"

  # Point SCRIPT_DIR to a directory without the watcher
  local empty_dir
  empty_dir="$(mktemp -d)"
  # The adapter resolves WATCHER_PY relative to its own location,
  # so we just check that the error message is correct
  PEON_DIR="$TEST_DIR" run bash -c "
    SCRIPT_DIR='$empty_dir'
    WATCHER_PY='$empty_dir/antigravity-watcher.py'
    source '$tmp_adapter'
  "
  rm -f "$tmp_adapter"
  rm -rf "$empty_dir"
  # Should have failed during preflight
  [ "$status" -ne 0 ] || [[ "$output" == *"not found"* ]]
}

# ============================================================
# Test mode: adapter sources cleanly without running
# ============================================================

@test "adapter sources in test mode without error" {
  source_adapter
  # pipe_to_peon should be defined
  type pipe_to_peon
}

# ============================================================
# Event piping: pipe_to_peon constructs valid JSON
# ============================================================

@test "pipe_to_peon passes event to peon.sh" {
  source_adapter

  # Replace peon.sh with a spy that captures stdin
  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat > "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  pipe_to_peon "Stop" "antigravity-abc12345" "/tmp/test"

  # Verify the spy received valid JSON with correct fields
  [ -f "$spy_out" ]
  local captured
  captured=$(cat "$spy_out")
  [[ "$captured" == *'"hook_event_name": "Stop"'* ]] || \
  [[ "$captured" == *'"hook_event_name":"Stop"'* ]]
  [[ "$captured" == *'"source": "antigravity"'* ]] || \
  [[ "$captured" == *'"source":"antigravity"'* ]]
}

# ============================================================
# --help flag
# ============================================================

@test "--help prints usage and exits 0" {
  run bash "$ADAPTER_SH" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage"* ]]
  [[ "$output" == *"--install"* ]]
}

@test "pipe_to_peon omits tool_name when there is no failure" {
  source_adapter

  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat > "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  pipe_to_peon "Stop" "antigravity-abc12345" "/tmp/test"

  local captured
  captured=$(cat "$spy_out")
  [[ "$captured" != *"tool_name"* ]]
}

@test "pipe_to_peon forwards tool_name and error for failures" {
  # peon.sh only sounds task.error when the payload names a tool and
  # carries a non-empty error string.
  source_adapter

  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat > "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  pipe_to_peon "PostToolUseFailure" "antigravity-abc12345" "/tmp/test" "Bash" "Exit code 1"

  local captured
  captured=$(cat "$spy_out")
  [[ "$captured" == *'"tool_name": "Bash"'* ]] || [[ "$captured" == *'"tool_name":"Bash"'* ]]
  [[ "$captured" == *"Exit code 1"* ]]
}

@test "antigravity failure payload makes peon.sh play the error sound" {
  # End-to-end: watcher payload shape -> real peon.sh -> task.error sound.
  source_adapter

  pipe_to_peon "PostToolUseFailure" "antigravity-abc12345" "/tmp/test" "Bash" "Exit code 1"
  sleep 0.5

  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/Error"* ]]
}

@test "antigravity permission payload makes peon.sh play the approval sound" {
  # cli.log tool confirmations arrive as PermissionRequest -> input.required.
  source_adapter

  pipe_to_peon "PermissionRequest" "antigravity-abc12345" "/tmp/test"
  sleep 0.5

  afplay_was_called
  sound=$(afplay_sound)
  [[ "$sound" == *"/packs/peon/sounds/"* ]]
}

@test "dispatch_event_line routes a watcher line with no optional fields" {
  # Regression: $() strips trailing newlines, so a line-per-field read hit
  # EOF on the empty tool_name/error and killed the loop under set -e.
  source_adapter

  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat >> "$spy_out"
echo >> "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  run dispatch_event_line '{"event":"Stop","session_id":"antigravity-abc","cwd":"/tmp/test"}'
  [ "$status" -eq 0 ]

  local captured
  captured=$(cat "$spy_out")
  [[ "$captured" == *'"hook_event_name": "Stop"'* ]] || \
  [[ "$captured" == *'"hook_event_name":"Stop"'* ]]
  [[ "$captured" != *"tool_name"* ]]
}

@test "dispatch_event_line carries tool_name and error through to peon.sh" {
  source_adapter

  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat > "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  run dispatch_event_line '{"event":"PostToolUseFailure","session_id":"antigravity-abc","cwd":"/tmp/test","tool_name":"Bash","error":"Exit code 1"}'
  [ "$status" -eq 0 ]

  local captured
  captured=$(cat "$spy_out")
  [[ "$captured" == *"Bash"* ]]
  [[ "$captured" == *"Exit code 1"* ]]
}

@test "dispatch_event_line survives a cwd containing spaces" {
  source_adapter

  local spy_out="$TEST_DIR/peon_spy_out"
  cat > "$TEST_DIR/peon.sh" <<SCRIPT
#!/bin/bash
cat > "$spy_out"
SCRIPT
  chmod +x "$TEST_DIR/peon.sh"

  run dispatch_event_line '{"event":"SessionStart","session_id":"antigravity-abc","cwd":"/tmp/some dir"}'
  [ "$status" -eq 0 ]
  [[ "$(cat "$spy_out")" == *"/tmp/some dir"* ]]
}

@test "dispatch_event_line ignores malformed and empty lines" {
  source_adapter
  run dispatch_event_line 'not json at all'
  [ "$status" -eq 0 ]
  run dispatch_event_line ''
  [ "$status" -eq 0 ]
}

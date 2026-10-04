#!/bin/bash
# peon-ping adapter for Gemini CLI
# Translates Gemini CLI hook events into peon.sh stdin JSON

set -euo pipefail

# Path to peon.sh - handles both local and global installs
PEON_DIR="${CLAUDE_PEON_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[ ! -f "$PEON_DIR/peon.sh" ] && PEON_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/peon-ping"

GEMINI_EVENT_TYPE="${1:-SessionStart}"

# Pass input values as data rather than interpolating them into Python source.
# Gemini's native AfterTool payload reports failures in tool_response.error.
# The top-level exit_code/stderr shape remains accepted for existing callers.
MAPPED_JSON="$(
  _GEMINI_EVENT_TYPE="$GEMINI_EVENT_TYPE" python3 -c '
import json
import os
import sys

try:
    data = json.load(sys.stdin)
except (ValueError, TypeError):
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)

event = os.environ.get("_GEMINI_EVENT_TYPE", "SessionStart")
mapped = {
    "SessionStart": "SessionStart",
    "AfterAgent": "Stop",
    "Notification": "Notification",
}.get(event)
error_text = ""

if event == "AfterTool":
    response = data.get("tool_response")
    native_error = response.get("error") if isinstance(response, dict) else None
    native_failure = isinstance(native_error, dict) or (
        isinstance(native_error, str) and bool(native_error.strip())
    )
    if native_failure:
        message = native_error.get("message") if isinstance(native_error, dict) else native_error
        error_text = str(message or "Tool failed")
    else:
        try:
            exit_code = int(data.get("exit_code", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            exit_code = 0
        if exit_code:
            error_text = str(data.get("stderr") or "Tool failed")
    if not error_text:
        # AfterAgent is the completion boundary; successful tools stay quiet.
        sys.exit(0)
    mapped = "PostToolUseFailure"

if not mapped:
    sys.exit(0)

payload = {
    "hook_event_name": mapped,
    "notification_type": "",
    "cwd": str(data.get("cwd") or os.environ.get("PWD") or "/"),
    "session_id": str(data.get("session_id") or "gemini-" + str(os.getpid())),
    "permission_mode": "",
    "source": "gemini",
}
if mapped == "PostToolUseFailure":
    # peon.sh routes task.error through its shell-tool failure category.
    payload["tool_name"] = "Bash"
    payload["error"] = error_text
print(json.dumps(payload))
' 2>/dev/null
)" || MAPPED_JSON=""

if [ -n "$MAPPED_JSON" ]; then
  printf '%s' "$MAPPED_JSON" | bash "$PEON_DIR/peon.sh" >/dev/null 2>&1 || true
fi

# Always return valid empty JSON to Gemini CLI.
echo "{}"

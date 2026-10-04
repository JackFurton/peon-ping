# peon-ping adapter for Gemini CLI (Windows)
# Translates Gemini CLI hook events into peon.ps1 stdin JSON
#
# Setup: Add to ~/.gemini/settings.json:
#   {
#     "hooks": {
#       "SessionStart": [{ "command": "powershell -NoProfile -File %USERPROFILE%\\.claude\\hooks\\peon-ping\\adapters\\gemini.ps1 SessionStart" }],
#       "AfterAgent":   [{ "command": "powershell -NoProfile -File %USERPROFILE%\\.claude\\hooks\\peon-ping\\adapters\\gemini.ps1 AfterAgent" }],
#       "AfterTool":    [{ "command": "powershell -NoProfile -File %USERPROFILE%\\.claude\\hooks\\peon-ping\\adapters\\gemini.ps1 AfterTool" }],
#       "Notification":  [{ "command": "powershell -NoProfile -File %USERPROFILE%\\.claude\\hooks\\peon-ping\\adapters\\gemini.ps1 Notification" }]
#     }
#   }

param(
    [string]$EventType = "SessionStart"
)

$ErrorActionPreference = "SilentlyContinue"

# Determine peon-ping install directory
$PeonDir = if ($env:CLAUDE_PEON_DIR) { $env:CLAUDE_PEON_DIR }
           else { Join-Path $env:USERPROFILE ".claude\hooks\peon-ping" }

$PeonScript = Join-Path $PeonDir "peon.ps1"
if (-not (Test-Path $PeonScript)) {
    Write-Output "{}"
    exit 0
}

# Read JSON from stdin
$inputJson = $null
try {
    if ([Console]::IsInputRedirected) {
        $stream = [Console]::OpenStandardInput()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        $raw = $reader.ReadToEnd()
        $reader.Close()
        if ($raw) { $inputJson = $raw | ConvertFrom-Json }
    }
} catch { if ($env:PEON_DEBUG -eq "1") { Write-Warning "peon-ping: [gemini] ConvertFrom-Json failed: $_" } }
if (-not $inputJson) { $inputJson = [PSCustomObject]@{} }

# Extract common fields
$sessionId = if ($inputJson.session_id) { $inputJson.session_id } else { "gemini-$PID" }
$cwd = if ($inputJson.cwd) { $inputJson.cwd } else { $PWD.Path }

# Map Gemini event to CESP event name
$mapped = $null
$errorText = ""

switch ($EventType) {
    "SessionStart" {
        $mapped = "SessionStart"
    }
    "AfterAgent" {
        $mapped = "Stop"
    }
    "Notification" {
        $mapped = "Notification"
    }
    "AfterTool" {
        # Native Gemini hooks carry ToolResult.error inside tool_response.
        # Keep the prior top-level exit_code/stderr contract as a fallback.
        $nativeError = $inputJson.tool_response.error
        $nativeFailure = $nativeError -is [System.Management.Automation.PSCustomObject] -or (
            $nativeError -is [string] -and -not [string]::IsNullOrWhiteSpace($nativeError)
        )
        if ($nativeFailure) {
            $errorText = if ($nativeError -is [string]) { $nativeError } else { $nativeError.message }
            if (-not $errorText) { $errorText = "Tool failed" }
        } else {
            $exitCode = 0
            if ($null -ne $inputJson.exit_code) {
                [int]::TryParse([string]$inputJson.exit_code, [ref]$exitCode) | Out-Null
            }
            if ($exitCode -ne 0) {
                $errorText = if ($inputJson.stderr) { $inputJson.stderr } else { "Tool failed" }
            }
        }
        if ($errorText) {
            $mapped = "PostToolUseFailure"
        } else {
            # AfterAgent owns completion; successful tool calls stay quiet.
            Write-Output "{}"
            exit 0
        }
    }
    default {
        # Unknown event — return empty JSON
        Write-Output "{}"
        exit 0
    }
}

# Build CESP JSON payload
$payload = @{
    hook_event_name   = $mapped
    notification_type = ""
    cwd               = $cwd
    session_id        = $sessionId
    permission_mode   = ""
    source            = "gemini"
}

if ($mapped -eq "PostToolUseFailure") {
    $payload["tool_name"] = "Bash"
    $payload["error"] = $errorText
}

$payloadJson = $payload | ConvertTo-Json -Compress

# Pipe to peon.ps1 (suppress its stdout)
$payloadJson | powershell -NoProfile -NonInteractive -File $PeonScript 2>$null | Out-Null

# Always return valid empty JSON to Gemini CLI
Write-Output "{}"

exit 0

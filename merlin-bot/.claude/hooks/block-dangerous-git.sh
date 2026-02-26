#!/bin/bash
COMMAND=$(jq -r '.tool_input.command')

if echo "$COMMAND" | grep -qE 'push.*--force|push.*-f[^a-z]|push.*--delete.*(master|main)|push.*:(master|main)|branch.*-D.*(master|main)|branch.*-d.*(master|main)'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "Force push and master/main branch deletion are blocked"
    }
  }'
fi

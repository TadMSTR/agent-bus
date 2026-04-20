#!/bin/bash
# cleanup.sh — prune agent-bus logs past retention window
set -euo pipefail

COMMS_DIR="${AGENT_BUS_COMMS_DIR:-$HOME/.claude/comms}"
LOGS_DIR="$COMMS_DIR/logs"
CROSS_AGENT_RETENTION="${AGENT_BUS_CROSS_AGENT_RETENTION_DAYS:-90}"
SESSION_RETENTION="${AGENT_BUS_SESSION_RETENTION_DAYS:-30}"
find "$LOGS_DIR" -name "*-cross-agent.jsonl" -mtime +"$CROSS_AGENT_RETENTION" -delete
find "$LOGS_DIR" -name "*-session.jsonl" -mtime +"$SESSION_RETENTION" -delete
echo "agent-bus-cleanup: done $(date -u +%Y-%m-%dT%H:%M:%SZ)"

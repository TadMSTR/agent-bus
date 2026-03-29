#!/bin/bash
# cleanup.sh — prune agent-bus logs past retention window
set -euo pipefail

LOGS_DIR="$HOME/.claude/comms/logs"
find "$LOGS_DIR" -name "*-cross-agent.jsonl" -mtime +90 -delete
find "$LOGS_DIR" -name "*-session.jsonl" -mtime +30 -delete
echo "agent-bus-cleanup: done $(date -u +%Y-%m-%dT%H:%M:%SZ)"

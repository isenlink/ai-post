#!/usr/bin/env bash
# ai-bridge: command-line client for the ai-post message relay.
#
# Usage:
#   ai-bridge send <target-agent> "<message>" [reply_to_id]
#   ai-bridge poll [since_id]        # fetch new inbox messages (silent if none)
#   ai-bridge poll-new               # same, but remembers the cursor (for cron / schedulers)
#   ai-bridge thread <context_id>    # view a full conversation thread
#   ai-bridge agents                 # view the agent registry
#   ai-bridge register "<desc>" [cap1,cap2] [location]
#   ai-bridge audit [--from X] [--to Y] [--hours N]
#
# Config: ai-bridge.conf next to this script:
#   POST_URL="http://127.0.0.1:9100"
#   MY_TOKEN="<your agent token>"
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/ai-bridge.conf"
[ -f "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"

cmd="${1:-}"; shift || true
case "$cmd" in
  send)
    to="$1"; msg="$2"; reply_to="${3:-}"
    body=$(python3 -c 'import json,sys; print(json.dumps({"to":sys.argv[1],"msg":sys.argv[2],**({"reply_to":int(sys.argv[3])} if sys.argv[3] else {})},ensure_ascii=False))' "$to" "$msg" "$reply_to")
    curl -sf -X POST "$POST_URL/send" -H "X-Auth-Token: "$MY_TOKEN"" -H 'Content-Type: application/json' -d "$body"
    echo
    ;;
  poll)
    since="${1:-0}"
    curl -sf "$POST_URL/poll?since=$since" -H "X-Auth-Token: "$MY_TOKEN""
    echo
    ;;

  poll-new)
    # Cursor-based poll for schedulers: read the last seen id from $CURSOR_FILE
    # (default: .ai-bridge-cursor next to this script), fetch only newer
    # messages, print them, then advance the cursor. Stays completely silent
    # when there is nothing new, so it is safe on a 1-2 minute schedule.
    cursor_file="${CURSOR_FILE:-$DIR/.ai-bridge-cursor}"
    since=0
    [ -f "$cursor_file" ] && since="$(cat "$cursor_file" 2>/dev/null || echo 0)"
    out="$(curl -sf "$POST_URL/poll?since=$since" -H "X-Auth-Token: "$MY_TOKEN"")" || exit 1
    newmax="$(printf '%s' "$out" | python3 -c 'import json,sys; m=json.load(sys.stdin).get("messages",[]); print(max(x["id"] for x in m) if m else "")')"
    [ -n "$newmax" ] || exit 0
    printf '%s' "$out" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'
    printf '%s\n' "$newmax" > "$cursor_file"
    ;;
  thread)
    curl -sf "$POST_URL/thread?context_id=$1" -H "X-Auth-Token: "$MY_TOKEN""
    echo
    ;;
  agents)
    curl -sf "$POST_URL/agents" -H "X-Auth-Token: "$MY_TOKEN""
    echo
    ;;
  register)
    desc="$1"; caps="${2:-}"; loc="${3:-}"
    body=$(python3 -c 'import json,sys; caps=sys.argv[2]; print(json.dumps({"description":sys.argv[1],"capabilities":[c for c in caps.split(",") if c],"location":sys.argv[3]},ensure_ascii=False))' "$desc" "$caps" "$loc")
    curl -sf -X POST "$POST_URL/agents/register" -H "X-Auth-Token: "$MY_TOKEN"" -H 'Content-Type: application/json' -d "$body"
    echo
    ;;
  audit)
    args=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --from) args+=("frm=$2"); shift 2;;
        --to) args+=("to=$2"); shift 2;;
        --hours) args+=("since_hours=$2"); shift 2;;
        *) shift;;
      esac
    done
    qs=$(IFS='&'; echo "${args[*]:-}")
    curl -sf "$POST_URL/audit${qs:+?$qs}" -H "X-Auth-Token: "$MY_TOKEN""
    echo
    ;;
  *)
    echo "usage: ai-bridge send <to> <msg> [reply_to] | poll [since] | poll-new | thread <ctx> | agents | register <desc> [caps] [loc] | audit [--from X] [--to Y] [--hours N]" >&2
    exit 1
    ;;
esac

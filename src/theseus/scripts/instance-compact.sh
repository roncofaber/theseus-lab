#!/usr/bin/env bash
# Compacts (or clears) a persistent worker session between tasks. An agent session cannot compact itself, so the
# coordinator types its profile's compact or clear command into the instance's tmux pane once the pane is idle.
# Usage: theseus instance compact PANE [--clear] [KEEP_TEXT]
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
[[ $# -ge 1 ]] || { echo "usage: instance-compact.sh PANE [--clear] [KEEP_TEXT]" >&2; exit 2; }
pane=$1; shift
for _ in $(seq 1 36); do pane_busy "$pane" || break; sleep 5; done
pane_busy "$pane" && { echo "$pane still busy after 3 minutes; not compacting" >&2; exit 1; }
if [[ "${1:-}" == "--clear" ]]; then
    command="${THESEUS_CLEAR_COMMAND:-}"
    [[ -n "$command" ]] || { echo "$pane: the agent profile has no clear command" >&2; exit 1; }
else
    keep=${1:-"the standing brief path, the coordinator, the handoff format and lessons learned about this repository"}
    template="${THESEUS_COMPACT_TEMPLATE:-}"
    [[ -n "$template" ]] || { echo "$pane: the agent profile has no compact command" >&2; exit 1; }
    command="${template//\{keep\}/$keep}"
fi
tmux send-keys -t "$pane" -l "$command" && sleep 0.3 && tmux send-keys -t "$pane" Enter
for _ in $(seq 1 60); do sleep "${THESEUS_COMPACT_POLL:-5}"; pane_busy "$pane" || break; done
echo "$pane: ${command%% *} done"

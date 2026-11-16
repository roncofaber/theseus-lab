#!/usr/bin/env bash
# Types one line of text into a persistent agent's tmux pane once it is at an idle prompt (for nudges and short
# follow-ups from the queue). Waits up to three minutes for the pane to become idle.
# Usage: instance-type.sh PANE TEXT
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
[[ $# -eq 2 ]] || { echo "usage: instance-type.sh PANE TEXT" >&2; exit 2; }
pane=$1 text=$2
for _ in $(seq 1 36); do pane_busy "$pane" || break; sleep "${THESEUS_TYPE_POLL:-5}"; done
pane_busy "$pane" && { echo "$pane still busy after 3 minutes; not typing" >&2; exit 1; }
tmux send-keys -t "$pane" -l "$text" && sleep 0.3 && tmux send-keys -t "$pane" Enter
echo "$pane: typed"

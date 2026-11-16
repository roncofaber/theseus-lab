#!/usr/bin/env bash
# Types a task message into a persistent worker's tmux pane: the standing brief to re-read, the worktree, the task
# record and an optional deadline. Waits up to three minutes for an idle prompt first. The CLI renders RECORD.
# Usage: theseus instance send PANE TASK_ID WORKTREE [DEADLINE_TEXT]
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
[[ $# -ge 4 ]] || { echo "usage: instance-send.sh PANE TASK_ID WORKTREE RECORD [DEADLINE_TEXT]" >&2; exit 2; }
pane=$1 task=$2 worktree=$3 record=$4 deadline=${5:-}
for _ in $(seq 1 36); do pane_busy "$pane" || break; sleep 5; done
pane_busy "$pane" && { echo "$pane still busy after 3 minutes; not sending" >&2; exit 1; }
message="Read and follow $THESEUS_BRIEF first. New task $task in worktree $worktree. $record"
[[ -n "$deadline" ]] && message+=" Deadline: $deadline; if the work cannot be finished by then, deliver a partial handoff with what works."
message+=" Run only the checks named here. Finish with the handoff and a done line"
if [[ -n "${THESEUS_COORDINATOR:-}" && "${THESEUS_MESSAGING:-1}" == 1 ]]; then
    message+=" sent to the coordinator session $THESEUS_COORDINATOR with SendMessage."
else
    message+="."
fi
tmux send-keys -t "$pane" -l "$message" && sleep 0.5 && tmux send-keys -t "$pane" Enter
echo "$pane: sent $task"

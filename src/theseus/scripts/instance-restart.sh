#!/usr/bin/env bash
# Restarts a persistent agent session in its tmux pane and resumes the same conversation, so it reloads MCP servers,
# skills and settings without losing its name or history. The agent is the pane shell's child that owns a session
# record (Claude Code keeps one per process, <sessions dir>/<pid>.json), which also gives its session ID. The relaunch is typed into the
# pane's own shell, so the same aliases and environment (for example a model gateway configuration) apply.
# Usage: theseus instance restart PANE [--dry-run]
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
[[ $# -ge 1 ]] || { echo "usage: instance-restart.sh PANE [--dry-run]" >&2; exit 2; }
pane=$1 dry=${2:-}
sessions=${THESEUS_AGENT_SESSIONS:-$HOME/.claude/sessions}
launch=${THESEUS_AGENT_LAUNCH:-claude}
resume_template=${THESEUS_RESUME_TEMPLATE:-"{launch} --resume {session}"}
resume_template=${resume_template//\{launch\}/$launch}
[[ -n "$sessions" && -d "$sessions" ]] || { echo "$pane: the agent profile records no sessions; restart is unsupported" >&2; exit 1; }
shell_pid=$(tmux display -p -t "$pane" '#{pane_pid}') || { echo "$pane: no such pane" >&2; exit 1; }
agent() { for pid in $(pgrep -P "$shell_pid"); do [[ -f "$sessions/$pid.json" ]] && { echo "$pid"; return; }; done; }
agent_pid=$(agent)
[[ -n "$agent_pid" ]] || { echo "$pane: no agent with a session record under shell $shell_pid" >&2; exit 1; }
session=$(jq -r '.sessionId // empty' "$sessions/$agent_pid.json" 2>/dev/null)
[[ -n "$session" ]] || { echo "$pane: no session record $sessions/$agent_pid.json" >&2; exit 1; }
name=$(jq -r '.name // empty' "$sessions/$agent_pid.json")
if [[ "$dry" == --dry-run ]]; then
    echo "$pane: would restart ${name:-unnamed} (pid $agent_pid, session $session) with: ${resume_template//\{session\}/$session}"
    exit 0
fi
for _ in $(seq 1 36); do pane_busy "$pane" || break; sleep 5; done
pane_busy "$pane" && { echo "$pane still busy after 3 minutes; not restarting" >&2; exit 1; }
tmux send-keys -t "$pane" -l "/exit" && sleep 0.3 && tmux send-keys -t "$pane" Enter
for _ in $(seq 1 60); do kill -0 "$agent_pid" 2>/dev/null || break; sleep 1; done
kill -0 "$agent_pid" 2>/dev/null && { echo "$pane: pid $agent_pid did not exit after /exit" >&2; exit 1; }
tmux send-keys -t "$pane" -l "${resume_template//\{session\}/$session}" && sleep 0.3 && tmux send-keys -t "$pane" Enter
new_pid=""
for _ in $(seq 1 90); do
    new_pid=$(agent)
    [[ -n "$new_pid" ]] && break
    sleep 1
done
[[ -n "$new_pid" ]] || { echo "$pane: no agent came back within 90 s" >&2; exit 1; }
echo "$pane: restarted ${name:-unnamed} as pid $new_pid, session $session"

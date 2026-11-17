#!/usr/bin/env bash
# Restarts a persistent agent session in its tmux pane and resumes the same conversation, so it reloads MCP servers,
# skills and settings without losing its name or history. The agent is the pane shell's child that owns a session
# record (Claude Code keeps one per process, <sessions dir>/<pid>.json), which also gives its session ID. The relaunch is typed into the
# pane's own shell, so the same aliases and environment (for example a model gateway configuration) apply. When the
# session no longer exists (the pane shows THESEUS_RESUME_MISSING with its ID), the agent is launched fresh instead.
# It returns once the new agent's screen has stopped changing, so whatever is typed next reaches a ready prompt.
# Usage: theseus instance restart PANE [--dry-run]
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
[[ $# -ge 1 ]] || { echo "usage: instance-restart.sh PANE [--dry-run]" >&2; exit 2; }
pane=$1 dry=${2:-}
sessions=${THESEUS_AGENT_SESSIONS:-$HOME/.claude/sessions}
launch=${THESEUS_AGENT_LAUNCH:-claude}
resume_template=${THESEUS_RESUME_TEMPLATE:-"{launch} --resume {session}"}
missing=${THESEUS_RESUME_MISSING-No conversation found with session ID}
[[ "$pane" != =*--*: ]] || { echo "$pane is a supervised agent; theseus agents restart NAME restarts it" >&2; exit 2; }
[[ -n "$sessions" && -d "$sessions" ]] || { echo "$pane: the agent profile records no sessions; restart is unsupported" >&2; exit 1; }
shell_pid=$(tmux display -p -t "$pane" '#{pane_pid}') || { echo "$pane: no such pane" >&2; exit 1; }
agent() { for pid in $(pgrep -P "$shell_pid"); do [[ -f "$sessions/$pid.json" ]] && { echo "$pid"; return; }; done; }
agent_pid=$(agent)
[[ -n "$agent_pid" ]] || { echo "$pane: no agent with a session record under shell $shell_pid" >&2; exit 1; }
session=$(jq -r '.sessionId // empty' "$sessions/$agent_pid.json" 2>/dev/null)
[[ -n "$session" ]] || { echo "$pane: no session record $sessions/$agent_pid.json" >&2; exit 1; }
name=$(jq -r '.name // empty' "$sessions/$agent_pid.json")
if [[ -n "$name" ]]; then
    launch=${launch//\{agent\}/"$(printf '%q' "$name")"}
else
    launch=${launch// --name \{agent\}/}
fi
resume=${resume_template//\{launch\}/"$launch"}
resume=${resume//\{session\}/"$session"}
if [[ "$dry" == --dry-run ]]; then
    echo "$pane: would restart ${name:-unnamed} (pid $agent_pid, session $session) with: $resume"
    exit 0
fi
for _ in $(seq 1 36); do pane_busy "$pane" || break; sleep 5; done
pane_busy "$pane" && { echo "$pane still busy after 3 minutes; not restarting" >&2; exit 1; }
type_line() { tmux send-keys -t "$pane" -l "$1" && sleep 0.3 && tmux send-keys -t "$pane" Enter; }
poll=${THESEUS_RESTART_POLL:-1}
type_line "/exit"
deadline=$((SECONDS + 60))
while kill -0 "$agent_pid" 2>/dev/null && ((SECONDS < deadline)); do sleep "$poll"; done
kill -0 "$agent_pid" 2>/dev/null && { echo "$pane: pid $agent_pid did not exit after /exit" >&2; exit 1; }
type_line "$resume"
new_pid="" fresh=""
deadline=$((SECONDS + 90))
while ((SECONDS < deadline)); do
    new_pid=$(agent)
    [[ -n "$new_pid" ]] && break
    if [[ -n "$missing" && -z "$fresh" ]] && tmux capture-pane -p -t "$pane" 2>/dev/null | grep -F -- "$missing" | grep -qF -- "$session"; then
        fresh=1
        type_line "$launch"
    fi
    sleep "$poll"
done
[[ -n "$new_pid" ]] || { echo "$pane: no agent came back within 90 s" >&2; exit 1; }
previous=""
for _ in $(seq 1 30); do
    sleep "$poll"
    screen=$(tmux capture-pane -p -t "$pane" 2>/dev/null)
    [[ -n "$screen" && "$screen" == "$previous" ]] && ! pane_busy "$pane" && break
    previous=$screen
done
if [[ -n "$fresh" ]]; then
    new_session=$(jq -r '.sessionId // empty' "$sessions/$new_pid.json" 2>/dev/null)
    echo "$pane: session $session no longer exists; launched ${name:-unnamed} fresh as pid $new_pid, session ${new_session:-unknown}"
else
    echo "$pane: restarted ${name:-unnamed} as pid $new_pid, session $session"
fi

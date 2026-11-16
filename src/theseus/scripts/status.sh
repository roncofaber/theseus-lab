#!/usr/bin/env bash
# One line per dispatched worker (pid, state, changed files, idle minutes, handoff) and per other worktree of the
# repository. States: running, exited, FAILED (exited without a handoff), LEAKING (processes left in its slice),
# subagent (a coordinator-launched worker without a PID record, whose liveness the coordinator session tracks).
set -euo pipefail
here="$(dirname -- "${BASH_SOURCE[0]}")"
# shellcheck source=lib.sh
source "$here/lib.sh"
[[ "${1:-}" == -h || "${1:-}" == --help ]] && { sed -n '2,4p' "$0"; exit 0; }

minutes_since_newest() {
    local newest
    newest="$(find "$@" -type f -not -path '*/.git/*' -not -path '*/node_modules/*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)"
    if [[ -n "$newest" ]]; then
        printf '%sm' "$(( ($(date +%s) - ${newest%.*}) / 60 ))"
    else
        printf -- '-'
    fi
}

shopt -s nullglob
declare -A launched=()
failures=()
printf '%-40s %-8s %-12s %-8s %-8s %s\n' WORKER PID STATE CHANGED IDLE HANDOFF
for launch in "$THESEUS_DATA"/workers/*/launch.json; do
    record_dir="$(dirname "$launch")"
    name="$(basename "$record_dir")"
    worktree="$(jq -r .worktree "$launch")"
    [[ -d "$worktree" ]] || continue
    launched["$worktree"]=1
    pid="$(cat "$record_dir/pid" 2>/dev/null || echo '-')"
    state=exited
    if [[ ! -f "$record_dir/pid" ]]; then
        state=subagent
    elif worker_alive "$name"; then
        state=running
    fi
    changed="$(git -C "$worktree" status --porcelain --untracked-files=all | grep -vc " $THESEUS_WORKER_LOG/" || true)"
    handoff=no
    [[ -s "$worktree/$THESEUS_WORKER_LOG/handoff.md" ]] && handoff=yes
    leaked="$(systemctl --user show -p TasksCurrent --value "$(slice_of "$name")" 2>/dev/null || true)"
    if [[ "$state" == exited && "$leaked" =~ ^[1-9][0-9]*$ ]]; then
        state=LEAKING
        failures+=("$name: $leaked processes still run in $(slice_of "$name") after the worker exited; theseus stop $name")
    fi
    if [[ "$state" == exited && "$handoff" == no ]]; then
        state=FAILED
        failures+=("$name: $( { jq -r '.result // .error // "no result"' "$record_dir/result.json" 2>/dev/null || echo "no result"; } | head -c 160)")
    fi
    activity=("$record_dir" "$worktree")
    session="$(jq -r '.session // empty' "$launch")"
    if [[ -n "$session" ]]; then
        for transcript in "$HOME"/.claude/projects/*/"$session.jsonl"; do
            activity+=("$transcript")
        done
    fi
    printf '%-40s %-8s %-12s %-8s %-8s %s\n' "$name" "$pid" "$state" "$changed" "$(minutes_since_newest "${activity[@]}")" "$handoff"
done

for failure in "${failures[@]}"; do
    printf 'FAILED %s\n' "$failure"
done

git -C "$THESEUS_ROOT" worktree list --porcelain | sed -n 's/^worktree //p' | while read -r worktree; do
    [[ "$worktree" == "$THESEUS_ROOT" || -n "${launched[$worktree]:-}" ]] && continue
    changed="$(git -C "$worktree" status --porcelain --untracked-files=all | wc -l)"
    printf '%-40s %-8s %-12s %-8s %-8s %s\n' "$(basename "$worktree")" - no-record "$changed" "$(minutes_since_newest "$worktree")" "see theseus tasks list"
done
printf '\nIDLE is minutes since the newest change in the worker records, worktree or session transcript.\n'

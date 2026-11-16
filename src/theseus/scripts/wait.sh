#!/usr/bin/env bash
# Blocks until a dispatched shell worker exits, then prints which ones exited and whether each left a handoff. Process
# exit is the completion signal: workers sometimes write an interim handoff while still running. Shell workers do not
# notify the coordinator the way subagents do; run this in the background after dispatching.
# Exit 0 when a worker exited, 1 on timeout (default 3 hours), 2 when no shell worker is running.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

limit=10800
while (($# > 0)); do
    case "$1" in
        --timeout) limit="$2"; shift 2 ;;
        -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
        *) printf 'Usage: theseus wait [--timeout SECONDS]\n' >&2; exit 2 ;;
    esac
done

declare -A watched=()
shopt -s nullglob
for launch in "$THESEUS_DATA"/workers/*/launch.json; do
    record_dir="$(dirname "$launch")"
    [[ -f "$record_dir/pid" ]] || continue
    worktree="$(jq -r .worktree "$launch")"
    name="$(basename "$record_dir")"
    if [[ ! -d "$worktree" ]] || ! worker_alive "$name"; then
        continue
    fi
    watched["$name"]="$worktree"
done
if ((${#watched[@]} == 0)); then
    printf 'wait: no running shell worker\n'
    exit 2
fi
printf 'wait: watching %s\n' "${!watched[*]}"

deadline=$((SECONDS + limit))
while ((SECONDS < deadline)); do
    changed=()
    for name in "${!watched[@]}"; do
        if ! worker_alive "$name"; then
            if [[ -s "${watched[$name]}/$THESEUS_WORKER_LOG/handoff.md" ]]; then
                changed+=("$name: exited with a handoff")
            else
                changed+=("$name: exited without a handoff")
            fi
        fi
    done
    if ((${#changed[@]} > 0)); then
        printf 'wait: %s\n' "${changed[@]}"
        exit 0
    fi
    sleep "${THESEUS_WAIT_POLL:-20}"
done
printf 'wait: timed out after %ss\n' "$limit"
exit 1

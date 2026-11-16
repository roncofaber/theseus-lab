#!/usr/bin/env bash
# Prints one line per new instance handoff (HANDOFF name) and per instance that sat idle with an open worktree and no
# handoff (IDLE-NO-HANDOFF instance). Handoffs present at start are ignored, so restarting the watcher repeats nothing.
# Usage: theseus instance watch [INSTANCE:PANE ...]   (default: [[instances.panes]] in theseus.toml)
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
root="$THESEUS_WORKTREES"
panes=${*:-${THESEUS_INSTANCES:-}}
[[ -n "$panes" ]] || { echo "no instances: configure [[instances.panes]] or pass INSTANCE:PANE" >&2; exit 2; }
poll="${THESEUS_WATCH_POLL:-30}"
limit=$(( ${THESEUS_IDLE_MINUTES:-5} * 60 / poll ))
((limit > 0)) || limit=1
declare -A told idle
log="$THESEUS_WORKER_LOG"
inplace="${THESEUS_ROOT:-}/$log"
for w in "$root"/*; do [[ -f "$w/$log/handoff.md" ]] && told[$(basename "$w")]=1; done
for d in "$inplace"/*/; do [[ -f "$d/handoff.md" ]] && told[in-place:$(basename "$d")]=1; done
while true; do
    for d in "$inplace"/*/; do
        [[ -f "$d/handoff.md" ]] || continue
        n="in-place:$(basename "$d")"
        if [[ -z "${told[$n]:-}" ]]; then echo "HANDOFF $n"; told[$n]=1; fi
    done
    for pp in $panes; do
        i=${pp%%:*}
        for w in "$root/$i"-*; do
            [[ -d "$w" ]] || continue
            n=$(basename "$w")
            if [[ -f "$w/$log/handoff.md" && -z "${told[$n]:-}" ]]; then echo "HANDOFF $n"; told[$n]=1; fi
        done
    done
    for pp in $panes; do
        i=${pp%%:*} p=${pp##*:}
        open=0
        for w in "$root/$i"-*; do [[ -d "$w" && ! -f "$w/$log/handoff.md" ]] && open=1; done
        if ! pane_busy "$p" && [[ "$open" == 1 ]]; then idle[$i]=$(( ${idle[$i]:-0} + 1 )); else idle[$i]=0; fi
        [[ "${idle[$i]}" == "$limit" ]] && echo "IDLE-NO-HANDOFF $i for ${THESEUS_IDLE_MINUTES:-5} min"
    done
    [[ -n "${THESEUS_WATCH_ONCE:-}" ]] && exit 0
    sleep "$poll"
done

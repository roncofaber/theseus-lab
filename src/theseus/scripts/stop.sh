#!/usr/bin/env bash
# Interrupts a dispatched shell worker with SIGINT, waits up to two minutes, stops every process left in its slice,
# then prints the session ID that followup resumes.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

if (($# != 1)) || [[ "$1" == -* ]]; then
    printf 'Usage: theseus stop NAME\n' >&2
    exit 2
fi
record_dir="$THESEUS_DATA/workers/$1"
[[ -f "$record_dir/pid" ]] || { printf 'No pid record in %s\n' "$record_dir" >&2; exit 1; }
pid="$(cat "$record_dir/pid")"

if worker_alive "$1"; then
    kill -INT "$pid"
    for _ in $(seq "${THESEUS_STOP_WAIT:-120}"); do
        worker_alive "$1" || break
        sleep 1
    done
    if worker_alive "$1"; then
        printf 'pid %s still running after SIGINT; inspect before escalating to SIGTERM\n' "$pid" >&2
        exit 1
    fi
fi

slice="$(slice_of "$1")"
left="$(systemctl --user show -p TasksCurrent --value "$slice" 2>/dev/null || true)"
if [[ "$(systemctl --user is-active "$slice" 2>/dev/null || true)" == active ]]; then
    systemctl --user stop "$slice"
    [[ "$left" =~ ^[1-9][0-9]*$ ]] && printf 'stopped %s leftover processes in %s\n' "$left" "$slice"
fi

session="$(jq -r '.session // empty' "$record_dir/launch.json" 2>/dev/null || true)"
printf 'stopped %s (pid %s); session=%s\n' "$1" "$pid" "${session:-<none>}"
[[ -n "$session" ]]

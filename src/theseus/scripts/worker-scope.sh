#!/usr/bin/env bash
# Runs a shell worker in its own systemd user slice, <project>-worker-<name>.slice under <project>-worker.slice.
# stop.sh stops that slice, which ends every process the worker started, including nohup'd or setsid'd ones. The parent
# slice caps what all workers together take from this machine. exec keeps the caller's PID on the worker, so the
# recorded PID stays valid.
set -uo pipefail
# shellcheck source=lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    echo "Usage: worker-scope.sh NAME -- COMMAND...   run COMMAND in the worker's slice" >&2
    echo "       worker-scope.sh --slice NAME          print the worker's slice name" >&2
    exit 2
}

if [[ "${1:-}" == --slice ]]; then
    (($# == 2)) || usage
    slice_of "$2"
    exit 0
fi
(($# >= 3)) && [[ "$2" == -- ]] || usage
slice="$(slice_of "$1")"
shift 2

if [[ -n "${THESEUS_NO_SLICE:-}" ]]; then
    exec "$@"
fi
if systemctl --user set-property --runtime "$(slice_parent)" \
    CPUQuota="${THESEUS_WORKER_CPU_QUOTA:-800%}" MemoryMax="${THESEUS_WORKER_MEMORY_MAX:-32G}" 2>/dev/null; then
    exec systemd-run --user --scope --quiet --collect --slice="$slice" -- "$@"
fi
echo "worker-scope: no systemd user manager; the worker runs without a slice" >&2
exec "$@"

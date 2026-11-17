# Shared helpers for the packaged worker scripts. Source it; the theseus CLI exports THESEUS_* before running them.
# shellcheck shell=bash

: "${THESEUS_DATA:?run through the theseus CLI, which exports THESEUS_DATA}"
: "${THESEUS_PROJECT:?run through the theseus CLI, which exports THESEUS_PROJECT}"
THESEUS_WORKER_LOG="${THESEUS_WORKER_LOG:-.theseus}"

# slice_of NAME: the systemd user slice of one worker, a child of <project>-worker.slice.
slice_parent() { printf '%s-worker.slice' "${THESEUS_PROJECT//[^A-Za-z0-9]/_}"; }
slice_of() { printf '%s-worker-%s.slice' "${THESEUS_PROJECT//[^A-Za-z0-9]/_}" "${1//[^A-Za-z0-9]/_}"; }

# worker_alive NAME: true while the pid recorded for a dispatched shell worker is still that worker. A bare kill -0 also
# accepts an unrelated process that reused the pid after a reboot, so the pid must sit in the worker slice or have been
# started within two minutes before its pid file was written (dispatch and followup write it right after launch).
worker_alive() {
    local record="$THESEUS_DATA/workers/$1" pid slice fields recorded start
    pid="$(cat "$record/pid" 2>/dev/null)" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if [[ ! -r /proc/self/stat ]]; then
        # Without procfs (macOS, BSD) only liveness can be checked, not pid reuse.
        kill -0 "$pid" 2>/dev/null
        return
    fi
    [[ -r "/proc/$pid/stat" ]] || return 1
    fields="$(sed -E 's/^[0-9]+ \(.*\) //' "/proc/$pid/stat" 2>/dev/null)" || return 1
    [[ "${fields%% *}" != Z && "${fields%% *}" != X ]] || return 1
    slice="$(slice_of "$1")"
    if grep -qF "/$slice" "/proc/$pid/cgroup" 2>/dev/null; then
        return 0
    fi
    recorded="$(stat -c %Y "$record/pid" 2>/dev/null)" || return 1
    start="$(cut -d' ' -f20 <<< "$fields")"
    start=$(( $(awk '/^btime/ {print $2}' /proc/stat) + start / $(getconf CLK_TCK) ))
    (( start <= recorded + 2 && start >= recorded - 120 ))
}

# tmux_at PANE ARGS...: tmux for the server PANE lives on. A supervised agent's address (=project--agent:) is on the
# private server named by THESEUS_TMUX_FLAG and THESEUS_TMUX_SOCKET, always passed explicitly; any other pane is on
# the user's own server, called exactly as before.
tmux_at() {
    local pane=$1
    shift
    if [[ "$pane" == =*--*: ]]; then
        env -u TMUX -u TMUX_PANE tmux "${THESEUS_TMUX_FLAG:?run through the theseus CLI}" "${THESEUS_TMUX_SOCKET:?}" "$@"
    else
        tmux "$@"
    fi
}

# pane_busy PANE: true while the agent in the tmux pane is working (its status line shows the busy pattern).
pane_busy() {
    [[ -n "${THESEUS_BUSY_PATTERN:-}" ]] || return 1
    tmux_at "$1" capture-pane -p -t "$1" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -3 | grep -qF -- "$THESEUS_BUSY_PATTERN"
}

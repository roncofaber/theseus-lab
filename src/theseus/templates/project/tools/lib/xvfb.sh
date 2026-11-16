# Private Xvfb display for headless captures and performance runs. Source it, then:
#   xvfb_start WxHxDEPTH LOG [EXTRA_XVFB_ARGS...] || exit 1   # sets xvfb_pid and xvfb_display (":N")
#   xvfb_stop                                                 # from the caller's EXIT trap
# The server picks a free display number itself (-displayfd), so parallel runs never collide with each other or the
# user's session; the caller decides whether to export DISPLAY.
# shellcheck shell=bash

xvfb_pid=""
xvfb_display=""

xvfb_start() {
    local geometry=$1 log=$2 fdfile _
    shift 2
    fdfile=$(mktemp "${TMPDIR:-/tmp}/xvfb-display.XXXXXX") || return 1
    Xvfb -displayfd 3 -screen 0 "$geometry" -nolisten tcp "$@" 3>"$fdfile" >"$log" 2>&1 &
    xvfb_pid=$!
    for _ in $(seq 1 100); do
        [[ -s "$fdfile" ]] && break
        kill -0 "$xvfb_pid" 2>/dev/null || break
        sleep 0.1
    done
    if [[ ! -s "$fdfile" ]]; then
        printf 'Xvfb did not report a display; see %s\n' "$log" >&2
        tail -5 "$log" >&2 2>/dev/null
        rm -f "$fdfile"
        xvfb_stop
        return 1
    fi
    xvfb_display=":$(tr -d '[:space:]' <"$fdfile")"
    rm -f "$fdfile"
    if [[ "$xvfb_display" == "${DISPLAY:-}" ]]; then
        printf 'private Xvfb display %s collides with DISPLAY; refusing\n' "$xvfb_display" >&2
        xvfb_stop
        return 1
    fi
}

xvfb_stop() {
    if [[ -n "$xvfb_pid" ]]; then
        kill "$xvfb_pid" 2>/dev/null || true
        wait "$xvfb_pid" 2>/dev/null || true
    fi
    xvfb_pid=""
    return 0
}

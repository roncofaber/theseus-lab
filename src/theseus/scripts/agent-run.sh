#!/usr/bin/env bash
# Runs one headless agent turn (the profile's launch command) and, when the profile allows it, resumes the session
# after a retryable gateway error (Claude JSON output with status 429, 5xx or 530), so a short provider outage does not
# end the task. The resume command comes from THESEUS_RESUME_JSON, a JSON argument list containing {message}.
# The recorded PID is this wrapper; SIGINT from stop.sh is forwarded and never retried.
set -uo pipefail

if [[ $# -lt 3 || "$1" == "-h" || "$1" == "--help" ]]; then
    echo "Usage: agent-run.sh RECORD_DIR -- LAUNCH ARGS..." >&2
    exit 2
fi
record_dir="$1"
shift
[[ "$1" == "--" ]] && shift
attempts="${THESEUS_RETRIES:-3}"
stopped=0
child=0
trap 'stopped=1; ((child)) && kill -INT "$child" 2>/dev/null' INT TERM

run() {
    env --default-signal=INT "$@" > "$record_dir/result.json" 2>> "$record_dir/stderr.log" &
    child=$!
    while ! wait "$child"; do
        kill -0 "$child" 2>/dev/null || break
    done
    child=0
}

retry_delay() {
    python3 - "$record_dir/result.json" <<'PY'
import json, re, sys
try:
    result = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(1)
status = str(result.get("api_error_status") or "")
if not result.get("is_error") or status not in {"429", "500", "502", "503", "504", "529", "530"}:
    sys.exit(1)
match = re.search(r'"retry_after"\s*:\s*(\d+)', str(result.get("result", "")))
print(min(int(match.group(1)) if match else 120, 600))
PY
}

run "$@"
[[ "${THESEUS_RETRY:-0}" == 1 && -n "${THESEUS_RESUME_JSON:-}" ]] || exit 0
message="A transient gateway error interrupted your previous turn. Continue the task from where you stopped and finish with the handoff."
for ((attempt = 1; attempt <= attempts && stopped == 0; attempt++)); do
    delay="$(retry_delay)" || break
    mv "$record_dir/result.json" "$record_dir/result-gateway-$(date -u +%Y%m%dT%H%M%SZ)-$attempt.json"
    printf '%s gateway error; resuming after %ss (attempt %s of %s)\n' "$(date -u +%FT%TZ)" "$delay" "$attempt" "$attempts" >> "$record_dir/stderr.log"
    sleep "$delay" & child=$!; wait "$child"; child=0
    ((stopped)) && break
    mapfile -d '' -t resume < <(python3 -c 'import json, sys; [print(a.replace("{message}", sys.argv[2]), end="\0") for a in json.loads(sys.argv[1])]' "$THESEUS_RESUME_JSON" "$message")
    run "${resume[@]}"
done

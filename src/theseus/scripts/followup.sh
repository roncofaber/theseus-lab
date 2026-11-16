#!/usr/bin/env bash
# Resumes a stopped headless worker's session in its worktree, in the background, with a follow-up message, using the
# resume command its agent profile recorded in launch.json. The previous result.json is rotated to
# result-<UTC time>.json and the message is logged in followups.log.
set -euo pipefail
here="$(dirname -- "${BASH_SOURCE[0]}")"
# shellcheck source=lib.sh
source "$here/lib.sh"

if (($# != 2)) || [[ "$1" == -* ]]; then
    printf 'Usage: theseus followup NAME MESSAGE\n' >&2
    exit 2
fi
name="$1"
message="$2"
record_dir="$THESEUS_DATA/workers/$name"
launch="$record_dir/launch.json"
[[ -f "$launch" ]] || { printf 'No launch record %s\n' "$launch" >&2; exit 1; }
if worker_alive "$name"; then
    printf '%s is still running as pid %s; stop it first\n' "$name" "$(cat "$record_dir/pid")" >&2
    exit 1
fi
[[ "$(jq -r '.resume // empty | length' "$launch")" =~ ^[1-9] ]] || {
    printf '%s (profile %s) has no resume command; follow up from the coordinator session instead\n' "$name" "$(jq -r '.provider // "?"' "$launch")" >&2
    exit 1
}
worktree="$(jq -r .worktree "$launch")"
mapfile -t envs < <(jq -r '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' "$launch")
mapfile -d '' -t resume < <(jq -j --arg m "$message" '.resume[] | gsub("\\{message\\}"; $m) + "\u0000"' "$launch")
resume_json="$(jq -c '.resume' "$launch")"

[[ -f "$record_dir/result.json" ]] && mv "$record_dir/result.json" "$record_dir/result-$(date -u +%Y%m%dT%H%M%SZ).json"
printf '%s %s\n' "$(date -u +%FT%TZ)" "$message" >> "$record_dir/followups.log"
(cd "$worktree" && exec env --default-signal=INT THESEUS_ROOT="$worktree" THESEUS_RESUME_JSON="$resume_json" nohup \
    bash "$here/worker-scope.sh" "$name" -- bash "$here/agent-run.sh" "$record_dir" -- env "${envs[@]}" "${resume[@]}" > /dev/null 2>&1) &
pid=$!
printf '%s\n' "$pid" > "$record_dir/pid"
printf 'Resumed %s (session %s) as pid %s\n' "$name" "$(jq -r '.session // "-"' "$launch")" "$pid"

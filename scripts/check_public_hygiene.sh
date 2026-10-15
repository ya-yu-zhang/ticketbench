#!/usr/bin/env bash
# Pre-push / CI helper for a public portfolio repo.
# - Tracked tree must not contain .env / key-like paths
# - New commits must use a GitHub noreply author email
# Historical web commits (listed below) warn only — history is not rewritten.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${ROOT}" ]]; then
  echo "error: not inside a git repository" >&2
  exit 2
fi
cd "$ROOT"

# Short SHAs intentionally left with a personal Author email (no rewrite policy).
LEGACY_AUTHOR_OK=(42c08e2 6c71a3f)

fail=0

echo "== tracked path hygiene =="
mapfile -t tracked < <(git ls-files)
sensitive_re='(^|/)\.env($|\.)|(^|/)\.env\.|id_rsa|id_ed25519|\.pem$|\.p12$|\.pfx$|(^|/)secrets?/|(^|/)credentials($|/)|(^|/)\.aws/|(^|/)token\.|\.key$'
for f in "${tracked[@]}"; do
  if [[ "$f" == ".env.example" ]]; then
    continue
  fi
  if echo "$f" | grep -Eiq "$sensitive_re"; then
    echo "FAIL tracked sensitive path: $f" >&2
    fail=1
  fi
done
if [[ "$fail" -eq 0 ]]; then
  echo "ok: no sensitive tracked paths"
fi

is_noreply() {
  local email="$1"
  [[ "$email" == *'@users.noreply.github.com' ]] || [[ "$email" == 'noreply@github.com' ]]
}

is_legacy() {
  local full="$1" short
  short="$(git rev-parse --short=7 "$full")"
  local s
  for s in "${LEGACY_AUTHOR_OK[@]}"; do
    if [[ "$short" == "$s"* ]] || [[ "$full" == "$s"* ]]; then
      return 0
    fi
  done
  return 1
}

echo "== author email (commits under review) =="
commits=()
if [[ -n "${GITHUB_SHA:-}" ]]; then
  commits+=("$GITHUB_SHA")
elif git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
  mapfile -t commits < <(git rev-list --reverse '@{upstream}..HEAD')
fi
if [[ "${#commits[@]}" -eq 0 ]]; then
  commits=("$(git rev-parse HEAD)")
fi

for c in "${commits[@]}"; do
  [[ -n "$c" ]] || continue
  ae="$(git log -1 --format='%ae' "$c")"
  subj="$(git log -1 --format='%h %s' "$c")"
  if is_noreply "$ae"; then
    echo "ok: $subj → $ae"
  elif is_legacy "$c"; then
    echo "warn: legacy author accepted on $subj → $ae"
  else
    echo "FAIL author email on $subj → $ae (want *@users.noreply.github.com)" >&2
    fail=1
  fi
done

echo "== historical author notice =="
while IFS=$'\t' read -r h ae; do
  if ! is_noreply "$ae"; then
    echo "notice: $h <$ae> (kept; Keep my email private going forward)"
  fi
done < <(git log --format='%h%x09%ae' HEAD)

if [[ "$fail" -ne 0 ]]; then
  echo "public hygiene: FAILED" >&2
  exit 1
fi
echo "public hygiene: PASSED"
exit 0

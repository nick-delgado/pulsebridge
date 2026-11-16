#!/usr/bin/env bash
# T38 demonstration, run by hand: bash infra/modules/edge-vm/tests/demo/pipefail_sigpipe.sh
# (about 3 seconds; exit 0 = demonstrated). `printf ... | grep -q` under `set -o pipefail` with a
# payload larger than the pipe buffer (64 KiB on Linux). grep -q exits at the first match;
# printf, still writing, gets SIGPIPE and exits 141, and pipefail makes 141 the pipeline's status.
#   - positive check  `if ! printf | grep -q X`: X IS present, but the check reports it missing;
#   - negative check  `if printf | grep -q X; then fail`: X IS present, but the check passes.
# It lives in tests/demo/ so that make tf-test does not run it and static check 11 (which refuses
# the old form in tests/*.sh) does not see the old forms it uses on purpose.
# The here-string form (`grep -q X <<<"$payload"`) has no second process to be killed: the status
# is grep's. Likewise `find | grep -q .` versus capturing find's output.
set -euo pipefail

# The match is the FIRST line, followed by ~1.9 MB more lines that printf is still writing.
payload="needle
$(for i in $(seq 1 100000); do printf 'filler-line-%06d\n' "$i"; done)"
printf 'payload: %d bytes\n' "${#payload}"

trials=20
old_pos_false=0 old_neg_false=0 new_pos_false=0 new_neg_false=0
for _ in $(seq 1 "$trials"); do
  # Old positive check: should find "needle".
  if ! printf '%s\n' "$payload" | grep -qxF -- needle; then old_pos_false=$((old_pos_false + 1)); fi
  # Old negative check: "needle" is present, so this check should FAIL; count when it passes.
  if printf '%s\n' "$payload" | grep -q needle; then :; else old_neg_false=$((old_neg_false + 1)); fi
  # New forms.
  if ! grep -qxF -- needle <<<"$payload"; then new_pos_false=$((new_pos_false + 1)); fi
  if grep -q needle <<<"$payload"; then :; else new_neg_false=$((new_neg_false + 1)); fi
done

# find | grep -q . : many matching files, so find is still printing when grep exits.
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
for i in $(seq 1 3000); do : >"$dir/left-behind-$i.tmp"; done
old_find_false=0 new_find_false=0
for _ in $(seq 1 "$trials"); do
  if find "$dir" -name '*.tmp' | grep -q .; then :; else old_find_false=$((old_find_false + 1)); fi
  left=$(find "$dir" \( -name '*.tmp' \) -print)
  if [ -n "$left" ]; then :; else new_find_false=$((new_find_false + 1)); fi
done

printf '\n%-58s %s\n' "form ($trials trials each; the text/files ARE present)" "wrong answers"
printf '%-58s %d\n' "old  if ! printf | grep -qxF  (positive: reports missing)" "$old_pos_false"
printf '%-58s %d\n' "old  if printf | grep -q; then fail  (negative: passes)" "$old_neg_false"
printf '%-58s %d\n' "old  if find | grep -q .; then fail  (negative: passes)" "$old_find_false"
printf '%-58s %d\n' "new  if ! grep -qxF -- X <<<\"\$payload\"" "$new_pos_false"
printf '%-58s %d\n' "new  if grep -q X <<<\"\$payload\"; then fail" "$new_neg_false"
printf '%-58s %d\n' "new  left=\$(find ...); [ -n \"\$left\" ]" "$new_find_false"

# The demonstration holds only if the old forms misbehaved at least once and the new ones never.
if [ $((old_pos_false + old_neg_false + old_find_false)) -gt 0 ] && [ $((new_pos_false + new_neg_false + new_find_false)) -eq 0 ]; then
  echo "DEMONSTRATED: the old forms gave wrong answers; the new forms never did"
else
  echo "NOT DEMONSTRATED"
  exit 1
fi

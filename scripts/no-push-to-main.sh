#!/usr/bin/env bash
# Pre-push hook. Refuses direct pushes to main/master.
#
# Why: every change in this repo goes through a feature branch + PR. The CI
# status check `All tests` is the merge gate. Bypassing that path skips review
# AND the test gate, which is how releases break.
#
# Override (don't, unless you know exactly why):
#   git push --no-verify

set -euo pipefail

while read -r local_ref local_sha remote_ref remote_sha; do
  case "$remote_ref" in
    refs/heads/main|refs/heads/master)
      echo "ERROR: direct push to ${remote_ref##*/} blocked." >&2
      echo "       Create a feature branch and open a PR:" >&2
      echo "         git checkout -b feature/<your-change>" >&2
      echo "         git push -u origin HEAD" >&2
      exit 1
      ;;
  esac
done

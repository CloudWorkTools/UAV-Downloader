#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
base=${1:?usage: check_flow_docs.sh COMPARISON_SHA}
[[ "$base" =~ ^[0-9a-f]{40}$ ]] || { echo 'Flow comparison base must be a full commit SHA' >&2; exit 1; }
[[ "$base" != "$(git rev-parse HEAD)" ]] || { echo 'Flow candidate cannot be compared with itself' >&2; exit 1; }
git merge-base --is-ancestor "$base" HEAD
"${PYTHON:-python3}" tools/flow-docs/flow_checker.py check --root . --base "$base"

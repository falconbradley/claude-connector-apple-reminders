#!/usr/bin/env bash
# test.sh — every test that runs without TCC grants; CI and the release
# workflow call this, so it is the one place a repo's test commands live.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

uv run python tests/test_e2e.py --skip-live
uv run python tests/test_launcher.py

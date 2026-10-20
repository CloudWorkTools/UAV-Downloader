#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

"${PYTHON:-python3}" -m pytest -q \
  tests/test_nas.py tests/test_daemon.py tests/test_daemon_downloads.py \
  tests/test_proxy.py tests/test_storage_failures.py tests/test_resolution_pref.py \
  tests/test_compare_upstream.py tests/test_category_catalog.py tests/test_smb_storage.py \
  tests/test_web_download_settings.py tests/test_search_intersection.py \
  tests/test_asr_api.py tests/test_vad_api.py tests/test_subtitle_capabilities.py \
  tests/test_llm_translation.py tests/test_subtitle_profiles.py \
  tests/test_translation_settings.py tests/test_firecrawl.py tests/test_deploy_remote.py \
  tests/test_subtitle_engine.py tests/test_subtitle_provenance.py \
  tests/test_activity.py tests/test_video_identity.py tests/test_history.py \
  tests/test_web_checks.py

for test in tests/*.cjs; do
  node "$test"
done

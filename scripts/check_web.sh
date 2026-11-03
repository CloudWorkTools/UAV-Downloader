#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

"${PYTHON:-python3}" -m pytest -q \
  tests/test_nas.py tests/test_daemon.py tests/test_daemon_downloads.py \
  tests/test_proxy.py tests/test_storage_failures.py tests/test_resolution_pref.py tests/test_crawler_naming.py \
  tests/test_media_errors.py tests/test_hanime1.py tests/test_pathlen.py tests/test_filename_mode.py tests/test_ui_theme.py \
  tests/test_supjav_browser_errors.py tests/test_supjav_fst.py tests/test_watcher_listing_errors.py \
  tests/test_huangguo.py tests/test_huangguo_apps.py tests/test_huangguo_integration.py \
  tests/test_missav_parsing.py tests/test_jable_canonical_urls.py tests/test_cf_override.py \
  tests/test_hls_url_resolution.py tests/test_media_request_hook.py \
  tests/test_browser_selection.py tests/test_download_workflow.py tests/test_web_queue.py tests/test_web_ui_state.py \
  tests/test_compare_upstream.py tests/test_category_catalog.py tests/test_smb_storage.py \
  tests/test_web_download_settings.py tests/test_search_intersection.py \
  tests/test_asr_api.py tests/test_vad_api.py tests/test_subtitle_capabilities.py \
  tests/test_llm_translation.py tests/test_subtitle_profiles.py \
  tests/test_translation_settings.py tests/test_firecrawl.py tests/test_deploy_remote.py tests/test_deploy_artifact.py \
  tests/test_subtitle_engine.py tests/test_subtitle_provenance.py \
  tests/test_subtitle_tasks.py tests/test_subtitle_file_selection.py \
  tests/test_subtitle_results.py tests/test_subtitle_http.py \
  tests/test_activity.py tests/test_video_identity.py tests/test_history.py \
  tests/test_web_checks.py tests/test_docker_evidence.py tests/test_web_canonical_runner.py

for test in tests/*.cjs; do
  node "$test"
done

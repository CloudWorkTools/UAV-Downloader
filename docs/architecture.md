# Architecture and source layout

UAV Downloader uses a `src` layout so runtime packages, build
files, documentation, and tests have explicit boundaries.

```text
src/uav_downloader/
  apps/          UAV Browser and UAV Watcher desktop applications
  cli/           UAV Downloader CLI and command-line argument handling
  core/          configuration, paths, migration, SSL, updater, crash logging
  entrypoints/   Windows and installed application entrypoints
  i18n/          application strings and site labels
  legacy/        supported fallback UI kept outside the primary applications
  sites/         shared crawler contract and per-site implementations
  subtitles/     recognition, translation, LLM, settings, and provenance
  ui/            shared theme and settings components
packaging/windows/
  PyInstaller specifications and Windows version resources
scripts/
  deterministic build and repository-maintenance automation
tools/
  manual development and screenshot utilities
tests/
  unit, integration, regression, packaging, and migration contracts
docs/
  user and contributor documentation plus visual assets
```

## Public entrypoints

- `uav-browser` → `uav_downloader.entrypoints.browse:main`
- `uav-watcher` → `uav_downloader.entrypoints.watch:main`
- `uav-downloader-cli` → `uav_downloader.cli.headless:main`

Both Windows entrypoints install shared runtime guards before importing GUI or
subtitle dependencies. This keeps SSL certificate setup, crash reporting, and
the frozen translation diagnostic consistent.

## Site boundary

`uav_downloader.sites` owns the current registry and exposes canonical
`validate_url` and `create_site` functions. Misspelled v2 aliases remain only
as compatibility shims. Each supported site implementation shares the crawler
contract, quality preference, proxy settings, naming rules, and download
worker limits.

## Persistence boundary

New user data belongs under `UAV Downloader`; legacy locations are
read only as migration sources. Migration is copy-only, skips links, never
deletes old data, and never overwrites a file already present at the new
location.

## Release boundary

Windows release bytes are produced by pinned CI and then verified as the same
bytes through artifact download, release upload, checksums, and attestations.
Canonical v3 assets and legacy aliases are both generated from that one build.

## Web 與 NAS 擴充邊界

Web 服務位於 `uav-web/`，透過 JSON API 呼叫 Python daemon。Rust 不接觸 NAS 密碼的永久儲存，也不直接呼叫爬蟲。

`daemon/nas.py` 提供設定讀寫、連線測試及影片傳送。HTTP handler 呼叫設定介面；任務管理器在加入任務時保存目的地快照，在影片與字幕完成後呼叫傳送介面。NAS 模組不回呼任務管理器，傳送取消透過事件傳入。

`uav-web/frontend/video-selection.js` 只管理批次選取與送出，由頁面傳入下載方法與完成回呼。

`uav-web/frontend/nas-settings.js` 只管理 NAS 表單，以 `load`、`payload` 與 `testConnection` 接入設定頁面。一般下載設定及影片瀏覽仍由現有頁面管理，新增 NAS 欄位不會修改網站爬蟲。

擴充測試分別位於 `tests/test_nas.py`、`tests/test_daemon.py` 與 `tests/web_ui.cjs`。容器啟動檢查由 `scripts/check_web_container.sh` 執行，上游同步的判斷流程見[上游更新維護](upstream-sync.md)。

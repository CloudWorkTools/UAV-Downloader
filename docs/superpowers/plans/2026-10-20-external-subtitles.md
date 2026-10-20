# External Subtitle API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement the task graph. Each task is linked to its GitHub issue and needs its own review and verification before merge to the integration branch.

**Goal:** Let users complete four independently configurable external subtitle stages from the Web, reuse versioned results, redo selected stages, and safely publish SRT files.

**Architecture:** Keep provider and credential handling in subtitle settings/adapters, stage results and subtitle queue in dedicated modules, and connect them through the existing daemon. The Web and Rust proxy expose validated operations; the browser remains the primary acceptance surface and drives the actual Web/daemon plus isolated HTTP model fixtures.

**Tech Stack:** Python 3.12, existing `requests` and file-backed persistence, Rust Web proxy, existing HTML/JavaScript, Node.js and Chrome headless, Docker production image.

**Spec:** [GitHub issue #10](https://github.com/CloudWorkTools/UAV-Downloader/issues/10); decomposition snapshot: `docs/superpowers/specs/2026-10-20-external-subtitles-work-breakdown.md`.

## Global Constraints

- Four stages are VAD, ASR, original-text correction, and subtitle translation; correction and translation can be disabled.
- LocalAI is the default service type; Ollama and LM Studio support is evaluated by stage capability, not the label “OpenAI compatible.”
- Every stage supports an external API path; do not download or load local model weights for this path.
- ASR must return segment start, end, and text in seconds; do not infer or guess time units.
- Requests that require reasoning disabled must be blocked when that capability is unknown; hiding reasoning output does not count as disabling it.
- A stage redo runs only the selected stage and selected translation language; downstream results become stale without automatic execution.
- Keep latest and previous successful result per stage/language; failed or cancelled work does not replace a successful result.
- A submitted/running task uses its immutable, non-secret settings snapshot. Secrets stay separately protected and tied to their service URL.
- Allow validated HTTP(S) model base URLs, including private-network endpoints. Reuse the existing ASR URL validation and credential binding behavior; disable redirects on credential-bearing requests and do not route model requests through the download proxy.
- Preserve legacy settings, task records, downloaded media, and human-edited or unknown-origin subtitle files.
- Keep file-backed persistence and existing storage abstractions; do not add a database or general plugin framework.
- Web acceptance must use Chrome against the real Web/daemon flow and an isolated HTTP model fixture; a frontend-only stub is insufficient.
- Record offline, production-image, and production-environment verification separately. Missing model host/version/sample data does not block local implementation, but real model quality remains unverified until measured.
- If CI changes, follow `CONTRIBUTING.md` runner and fork rules and retain purpose-specific production-image and deployment evidence.

## Review Focus

- Model endpoints on private networks, credential URL changes, redirects, and proxy variables: W1/W2 API fixture checks and W7 browser flow prove only configured HTTP(S) requests receive credentials.
- Malformed VAD/ASR/text responses, including invalid timestamps or segment IDs: W2 rejects the response; W7 confirms no invalid result is published.
- Stale/missing upstream results and same-settings force redo: W3 contract checks plus W7 browser flow confirm the chosen dependency and selected-stage request.
- Cancellation, restart, late response, and duplicate submission: W4 daemon checks plus W7 browser flow preserve media/results and resume incomplete work only.
- Empty speech, manual SRT edits, collision, and storage outage: W5 storage checks plus W7 browser flow confirm empty output and file protection.

---

## File Structure and Shared Contracts

Only add these product modules unless an existing boundary proves inadequate:

- Create `src/uav_downloader/subtitles/profiles.py`: profile validation, migration, secret references, and immutable non-secret snapshots.
- Create `src/uav_downloader/subtitles/vad.py`: external VAD adapter and interval validation.
- Create `src/uav_downloader/subtitles/results.py`: versioned stage results, dependency freshness, retention, and atomic persistence.
- Create `src/uav_downloader/daemon/subtitle_tasks.py`: subtitle-only queue, snapshots, cancellation, resume, and same-video lock.
- Modify `src/uav_downloader/subtitles/asr.py`, `settings.py`, and `engine.py` to reuse existing service and subtitle primitives.
- Modify `src/uav_downloader/daemon/task_manager.py` and `server.py` only to submit work at existing completion points and validate/forward subtitle operations.
- Modify `uav-web/src/routes.rs` only for the small set of new routes that must cross the proxy.
- Modify `uav-web/frontend/index.html`; add `subtitle-settings.js` and `subtitle-tasks.js` for subtitle-specific browser state and operations.
- Add `tests/subtitle_workflow.cjs`, which starts/uses the real Web and daemon plus a local isolated HTTP model fixture; extend the existing Python API, persistence, storage and cancellation suites for narrow cases that cannot be asserted reliably through the browser.
- Add new Python tests to `scripts/check_web.sh`. Existing `.cjs` tests are discovered automatically.

Shared Python contracts, owned by W1–W3 and consumed by W4–W7:

```python
Stage = Literal["vad", "asr", "correction", "translation"]
ProfileKind = Literal["vad", "asr", "text"]

@dataclass(frozen=True)
class StageProfile:
    profile_id: str
    display_name: str  # user-visible name for a named profile
    kind: ProfileKind  # one text profile may serve correction and translation
    provider: str
    capabilities: tuple[Stage, ...]
    base_url: str
    model: str
    parameters_json: str  # canonical serialized scalars, immutable
    credential_ref: str | None

@dataclass(frozen=True)
class ProfileSnapshot:
    profile_json: str  # canonical deep copy; no secret value
    credential_ref: str | None
    snapshot_id: str

@dataclass(frozen=True)
class SpeechInterval:
    start: float
    end: float

@dataclass(frozen=True)
class SubtitleSegment:
    segment_id: str
    start: float
    end: float
    text: str

@dataclass(frozen=True)
class StageResult:
    result_id: str
    video_id: str
    stage: Stage
    language: str | None
    input_result_ids: tuple[str, ...]
    profile_snapshot: ProfileSnapshot
    intervals: tuple[SpeechInterval, ...] | None  # set only for VAD
    segments: tuple[SubtitleSegment, ...] | None  # set only for ASR/correction/translation
```

`profiles.load_profiles() -> list[StageProfile]`; `profiles.save_profile(profile, api_key=None) -> StageProfile`; `profiles.snapshot(profile_id, stage) -> ProfileSnapshot`; validate that the selected stage appears in `capabilities`. Secret values are resolved only inside the adapter immediately before a request. Canonical JSON is copied on save and snapshot creation; no nested mutable mapping is shared with a queued task.

`vad.detect_speech(audio_path, profile_snapshot, cancel_check) -> list[SpeechInterval]`; `asr.transcribe_stage(audio_path, intervals, profile_snapshot, language, cancel_check) -> list[SubtitleSegment]`; preserve the existing public `asr.transcribe(settings, wav_path, output_base, log_path, cancel_check=None, progress_callback=None)` signature for desktop/Web callers. `engine.transform_segments(operation, segments, profile_snapshot, target_language=None, allow_reasoning=False, cancel_check=None) -> list[SubtitleSegment]`. Correction and translation preserve input IDs and boundaries.

`results.commit_success(result) -> StageResult` atomically creates a successful version; `results.latest(video_id, stage, language=None) -> StageResult | None`; `results.mark_dependents_stale(result_id) -> list[str]`. The task layer must not make a running result selectable as successful.

`subtitle_tasks.submit(video_id, selected_stages, profile_snapshots, target_languages) -> TaskSnapshot`; `subtitle_tasks.redo(video_id, stage, profile_snapshot, language=None, input_result_id=None) -> TaskSnapshot`; `subtitle_tasks.cancel(task_id) -> None`; `subtitle_tasks.resume(task_id) -> TaskSnapshot`. Existing daemon task APIs remain unchanged for download callers.

HTTP endpoints are rooted at `/api/subtitles`: `GET/PUT /profiles`, `POST /profiles/test`, `GET /videos`, `GET/POST /tasks`, `GET /tasks/{task_id}`, and `POST /tasks/{task_id}/action`. Task action JSON uses `action` = `cancel | resume | reorder | apply_new_settings | redo_stage | retry_publish`, with `position`, `profile_id`, `stage`, `language`, `input_result_id`, and `publish_status` only when required by that action. `GET /videos/{video_id}/results` lists per-stage/language latest/previous/stale metadata; `GET /videos/{video_id}/results/{result_id}` returns segments and raw/corrected comparison metadata; `GET /videos/{video_id}/results/{result_id}/content` returns downloadable SRT. `POST .../{result_id}/action` uses `action` = `restore | publish` and `replace_existing: true` only for the explicit replacement choice. All JSON consistently uses `profile_id`, `stage`, `language`, `task_id`, `result_id`, `stale`, and `publish_status`. The daemon validates again after Rust.

## Task 1: W1 — Profiles, Credentials, and Migration (#11)

**Files:** Create `subtitles/profiles.py`, `tests/test_subtitle_profiles.py`; modify `subtitles/asr.py`, `subtitles/settings.py`; extend `tests/test_asr_api.py` and `tests/test_translation_settings.py`.

- [ ] Add failing tests at the existing settings/API seam for profile validation, same-address secret retention, changed-address clearing, migration, and migration failure; do not claim browser-flow coverage at this layer.
- [ ] Implement `StageProfile`, `ProfileSnapshot`, `load_profiles`, `save_profile`, and `snapshot`; preserve source files on migration failure.
- [ ] Deep-copy/canonicalize profile parameters when saving and snapshotting; prove later mutation of input or loaded data cannot change a queued snapshot.
- [ ] Keep secrets out of snapshots/public JSON; accept validated HTTP(S) URLs under the ASR rule and bind each secret to its URL.
- [ ] Verify settings/API and migration tests; commit W1 and close #11 through its PR.

## Task 2: W2 — Service Adapters and Capability Validation (#12)

**Files:** Create `subtitles/vad.py`, `tests/test_vad_api.py`, `tests/test_subtitle_capabilities.py`; modify `subtitles/asr.py`, `subtitles/llm.py`, `subtitles/settings.py`; extend `tests/test_asr_api.py` and `tests/test_llm_translation.py`.

- [ ] Add failing adapter/API tests using an isolated HTTP fixture and assert the actual request and capability result.
- [ ] Implement VAD intervals, ASR segments, and text operations using the shared contracts; validate URL composition, finite seconds, segment IDs, provider reasoning controls, cancellation, and bounded responses.
- [ ] Disable redirects and environment proxies for credential-bearing requests; allow configured private-network endpoints.
- [ ] Verify adapter/API and malformed-response cases; browser coverage is completed in W7/W8. Commit W2 and close #12 through its PR.

## Task 3: W3 — Stage Results and Selected-Stage Engine (#13)

**Files:** Create `subtitles/results.py`, `tests/test_subtitle_results.py`; modify `subtitles/engine.py`; extend `tests/test_subtitle_engine.py` and `tests/test_subtitle_provenance.py`.

- [ ] Add failing engine/result tests at the available API seam for all enabled stages, same-settings force redo, explicit old-upstream selection, and restore.
- [ ] Implement atomic result versions, latest/previous retention, input links, selected-output publishing, and downstream stale marking through the shared contract.
- [ ] Preserve source timestamps across chunks; text stages retain IDs/boundaries; disabled stages make no external calls.
- [ ] Assert only the selected redo stage/language is requested and failure/cancellation leaves prior versions selectable. Browser end-to-end coverage is added in W7/W8. Commit W3 and close #13 through its PR.

## Task 4: W4 — Subtitle Queue, Snapshots, and Resume (#14)

**Files:** Create `daemon/subtitle_tasks.py`, `tests/test_subtitle_tasks.py`; modify `daemon/task_manager.py`; extend `tests/test_daemon_downloads.py`.

- [ ] Add failing daemon/API tests for queue ordering, duplicate submission, cancellation, restart, and explicit resume.
- [ ] Implement `submit`, `redo`, `cancel`, and `resume` using immutable snapshots; serialize per-video writes, persist incomplete chunks, and ignore late replies after cancellation.
- [ ] Submit subtitle work at download completion without changing the download lifecycle or retrying video download after subtitle failure.
- [ ] Verify only unfinished chunks resume and failed persistence never returns success; browser end-to-end coverage is added in W7/W8. Commit W4 and close #14 through its PR.

## Task 5: W5 — Video Selection and Safe SRT/NAS Publishing (#15)

**Files:** Modify `daemon/subtitle_tasks.py`, `daemon/task_manager.py`, `subtitles/engine.py`; only if required, extend `core/storage.py` or `daemon/nas.py`; add `tests/test_subtitle_file_selection.py`; extend `tests/test_smb_storage.py`, `tests/test_subtitle_provenance.py`, and `tests/test_web_download_settings.py`.

- [ ] Add failing task/storage/API tests for selecting an old download and allowed-root video, publishing SRT, and retrying failed publication.
- [ ] Validate roots, symlinks, missing files, and permissions; selection creates subtitle work only.
- [ ] Publish selected-language SRT atomically through existing storage; preserve modified/unknown files and save separately unless replacement is explicit.
- [ ] Verify isolated local and available mounted/NAS/SMB paths and prove storage retry adds no model request; browser end-to-end coverage is added in W7/W8. Commit W5 and close #15 through its PR.

## Task 6: W6 — Subtitle HTTP API and Rust Proxy (#16)

**Files:** Modify `daemon/server.py`, `uav-web/src/routes.rs`; add `tests/test_subtitle_http.py`; extend `tests/test_web_download_settings.py` and `tests/test_daemon.py`.

- [ ] Add failing HTTP tests for profile, video, task, redo, restore, and publication-retry operations through the current route seam.
- [ ] Implement the `/api/subtitles` routes below; validate stage, language, IDs, root-contained video identity, explicit replacement, and per-item batch outcomes in the daemon.
- [ ] Add only required Rust forwarding; daemon remains authoritative and responses/errors omit secrets.
- [ ] Verify HTTP status/body cases; run `cargo test --manifest-path uav-web/Cargo.toml` when Rust changes. Complete browser end-to-end coverage in W7/W8. Commit W6 and close #16 through its PR.

## Task 7: W7 — Browser Settings, Tasks, and Results (#17)

**Files:** Modify `uav-web/frontend/index.html`; create `subtitle-settings.js`, `subtitle-tasks.js`, `tests/subtitle_workflow.cjs`; extend `tests/web_settings.cjs` and `tests/task_menu.cjs`.

- [ ] Implement the real Chrome workflow over a live Web/daemon process and isolated HTTP fixture: configure → select video → submit → inspect progress → redo selected stage → cancel/resume → inspect/publish result.
- [ ] Render profile capability/reasoning state, stage/language selection, queue actions, stale dependencies, raw/corrected results, previous versions, and separate model/publish outcomes.
- [ ] Support inspect/download, restore, explicit replacement, queue reorder, apply-new-settings, and retry-publication intents from the fixed routes below.
- [ ] Verify narrow layout, keyboard, empty lists, background progress, duplicate submission, and failure/retry in Chrome; commit W7 and close #17 through its PR.

## Task 8: W8 — Integration, Quality, Documentation, and Deployment Evidence (#18)

**Files:** Modify `scripts/check_web.sh`, `README.md`, `docs/README.md`, `docs/web-ui.md`; add de-identified screenshots under `docs/assets/`; extend only the purpose-specific production-image verification entry point required by the feature.

- [ ] Ensure the browser workflow test and necessary Python contract tests run through `scripts/check_web.sh` and in the production web-test image.
- [ ] Run offline regression and a purpose-specific production-image integration flow covering stage redo, restart/resume, cancellation, safe SRT publication, and storage retry.
- [ ] Record real LocalAI VAD/ASR and LocalAI/Ollama/LM Studio text compatibility only for available, identified service/model versions; otherwise list it as unverified. Model quality requires fixed representative audio and reference text and cannot be claimed from fixture results.
- [ ] Update user documentation and capture screenshots from the running implementation with private media, addresses, paths, and credentials hidden.
- [ ] Run deployment validation only via `python3 scripts/deploy_remote.py` and record the separate production environment result or explicitly mark it incomplete; do not substitute offline or container tests.
- [ ] Run full review, resolve all findings, and close #18 through the final PR; leave the integration PR ready for review without merging it to the default branch.

## Self-Review

- **Spec coverage:** Settings, migration, secrets, four adapters, reasoning capabilities, stage versions, staleness, explicit redo/restore, queue/cancellation/restart, video selection, safe SRT/NAS publication, HTTP/Rust, browser UI, documentation, production-image and production verification are assigned across W1–W8.
- **Step scan:** W1–W6 start at the highest available settings, adapter, engine, daemon, storage, or HTTP seam. W7/W8 own the first complete real-browser flow and final browser verification.
- **Type consistency:** The `Stage`, `StageProfile`, `ProfileSnapshot`, `SpeechInterval`, `SubtitleSegment`, `StageResult`, adapter, result-store, task, and HTTP names are shared unchanged by every task.
- **Review focus:** Each of the five uncovered input classes above names the browser or fixture assertion and its owning task.
- **Proportion:** The plan fixes file ownership and cross-task interfaces without reproducing #10's product requirements or dictating method bodies.

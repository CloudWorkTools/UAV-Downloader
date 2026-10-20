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

- Model endpoints on private networks, credential URL changes, redirects, and proxy environment variables: browser-driven profile save/test proves authorized HTTP(S) requests reach only the configured fixture and never expose credentials in redirects or logs. Owned by W1/W2.
- Malformed or partial VAD/ASR/text responses, especially non-finite or out-of-range timestamps and missing/duplicate/extra segment IDs: full browser workflow reports a safe stage failure and publishes no invalid result. Owned by W2.
- Stale or missing upstream result, explicit old-version choice, and force redo with unchanged settings: browser workflow shows the dependency state and fixture request log proves only the selected stage ran. Owned by W3/W6/W7.
- Cancellation, restart, late response, and duplicate submission: browser workflow preserves the video and prior result, then resumes only incomplete work. Owned by W4/W7.
- Empty speech, manual SRT edits, collision, and storage outage: browser workflow shows an empty successful result where appropriate and preserves existing user files during failed or non-explicit replacement. Owned by W5/W7.

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

@dataclass(frozen=True)
class StageProfile:
    profile_id: str
    stage: Stage
    provider: str
    base_url: str
    model: str
    parameters: Mapping[str, str | int | float | bool]
    credential_ref: str | None

@dataclass(frozen=True)
class ProfileSnapshot:
    profile: StageProfile  # no secret value
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
    segments: tuple[SubtitleSegment, ...]  # VAD persists intervals in its stage payload
```

`profiles.load_profiles() -> list[StageProfile]`; `profiles.save_profile(profile, api_key=None) -> StageProfile`; `profiles.snapshot(profile_id) -> ProfileSnapshot`. Secret values are resolved only inside the adapter immediately before a request.

`vad.detect_speech(audio_path, profile_snapshot, cancel_check) -> list[SpeechInterval]`; `asr.transcribe(audio_path, intervals, profile_snapshot, language, cancel_check) -> list[SubtitleSegment]`; `engine.transform_segments(operation, segments, profile_snapshot, target_language=None, allow_reasoning=False, cancel_check=None) -> list[SubtitleSegment]`. Correction and translation must preserve the input IDs and boundaries.

`results.commit_success(result) -> StageResult` atomically creates a successful version; `results.latest(video_id, stage, language=None) -> StageResult | None`; `results.mark_dependents_stale(result_id) -> list[str]`. The task layer must not make a running result selectable as successful.

`subtitle_tasks.submit(video_id, selected_stages, profile_snapshots, target_languages) -> TaskSnapshot`; `subtitle_tasks.redo(video_id, stage, profile_snapshot, language=None, input_result_id=None) -> TaskSnapshot`; `subtitle_tasks.cancel(task_id) -> None`; `subtitle_tasks.resume(task_id) -> TaskSnapshot`. Existing daemon task APIs remain unchanged for download callers.

HTTP endpoints are rooted at `/api/subtitles`: `GET/PUT /profiles`, `POST /profiles/test`, `GET /videos`, `GET/POST /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `POST /tasks/{id}/resume`, `POST /tasks/{id}/redo`, `GET /videos/{id}/results`, and `POST /videos/{id}/results/{result_id}/restore`. Request/response JSON uses `profile_id`, `stage`, `language`, `input_result_id`, `task_id`, `result_id`, `stale`, and `publish_status` consistently. The daemon validates values again even when Rust has validated the same request.

## Task 1: W1 — Profiles, Credentials, and Migration (#11)

**Files:** Create `subtitles/profiles.py`, `tests/test_subtitle_profiles.py`; modify `subtitles/asr.py`, `subtitles/settings.py`; extend `tests/test_asr_api.py` and `tests/test_translation_settings.py`.

- [ ] Write browser workflow for saving and selecting profiles, including same-address secret retention and changed-address secret clearing; run against the current app to confirm the missing behavior.
- [ ] Implement `StageProfile`, `ProfileSnapshot`, `load_profiles`, `save_profile`, and `snapshot` with migration that leaves original files intact on failure.
- [ ] Keep secrets out of snapshots and public JSON; validate HTTP(S) base URLs with the ASR rule and bind each credential to its URL.
- [ ] Verify the browser flow plus migration/secret tests; commit W1 and close #11 through its PR.

## Task 2: W2 — Service Adapters and Capability Validation (#12)

**Files:** Create `subtitles/vad.py`, `tests/test_vad_api.py`, `tests/test_subtitle_capabilities.py`; modify `subtitles/asr.py`, `subtitles/llm.py`, `subtitles/settings.py`; extend `tests/test_asr_api.py` and `tests/test_llm_translation.py`.

- [ ] Extend the browser workflow to call profile compatibility testing against the isolated HTTP fixture and assert the actual request and displayed capability result.
- [ ] Implement VAD interval and ASR segment adapters and text operations using the shared contracts above; validate base URL composition, finite seconds, segment IDs, provider-specific reasoning controls, cancellation, and bounded responses.
- [ ] Make credential-bearing requests with redirects disabled and environment proxy use disabled; allow configured private-network endpoints.
- [ ] Verify browser flow and only the narrow malformed-response cases not stable at the browser boundary; commit W2 and close #12 through its PR.

## Task 3: W3 — Stage Results and Selected-Stage Engine (#13)

**Files:** Create `subtitles/results.py`, `tests/test_subtitle_results.py`; modify `subtitles/engine.py`; extend `tests/test_subtitle_engine.py` and `tests/test_subtitle_provenance.py`.

- [ ] Extend the browser workflow to run all enabled stages, force-redo one stage with unchanged settings, choose an old upstream result, and restore the previous result.
- [ ] Implement atomic successful result versions, latest/previous retention, selected output publishing, input-result links, and downstream stale marking through the shared result contract.
- [ ] Preserve source timestamps across VAD/ASR chunks; correction and translation retain stable IDs and boundaries; disabled stages produce no external calls.
- [ ] Assert the fixture request log includes only the selected redo stage/language and that failed/cancelled work leaves prior successful versions selectable; commit W3 and close #13 through its PR.

## Task 4: W4 — Subtitle Queue, Snapshots, and Resume (#14)

**Files:** Create `daemon/subtitle_tasks.py`, `tests/test_subtitle_tasks.py`; modify `daemon/task_manager.py`; extend `tests/test_daemon_downloads.py`.

- [ ] Extend the browser workflow for queue ordering, duplicate submission, cancellation, restart, and explicit resume.
- [ ] Implement `submit`, `redo`, `cancel`, and `resume` using immutable snapshots; serialize writes per video, persist incomplete chunk progress, and prevent late replies after cancellation from committing results.
- [ ] Submit subtitle work at download completion without changing the download task lifecycle or retrying the video after subtitle failure.
- [ ] Verify resumed runs request only unfinished chunks and failed persistence never returns success; commit W4 and close #14 through its PR.

## Task 5: W5 — Video Selection and Safe SRT/NAS Publishing (#15)

**Files:** Modify `daemon/subtitle_tasks.py`, `daemon/task_manager.py`, `subtitles/engine.py`; only if required, extend `core/storage.py` or `daemon/nas.py`; add `tests/test_subtitle_file_selection.py`; extend `tests/test_smb_storage.py`, `tests/test_subtitle_provenance.py`, and `tests/test_web_download_settings.py`.

- [ ] Extend the browser workflow to select an old download and an allowed-root video, then publish a subtitle and retry a failed publication.
- [ ] Validate paths against allowed roots, including symlinks, missing files, and permissions; selection must create subtitle work only.
- [ ] Publish selected-language SRT atomically through existing storage abstractions; preserve modified/unknown files and resolve collision by saving separately unless explicit replacement was chosen.
- [ ] Verify isolated local, mounted/NAS/SMB paths where available and prove storage retry adds no model request; commit W5 and close #15 through its PR.

## Task 6: W6 — Subtitle HTTP API and Rust Proxy (#16)

**Files:** Modify `daemon/server.py`, `uav-web/src/routes.rs`; add `tests/test_subtitle_http.py`; extend `tests/test_web_download_settings.py` and `tests/test_daemon.py`.

- [ ] Extend the browser workflow to exercise profile, video, task, redo, restore, and storage-retry endpoints end to end.
- [ ] Implement only the `/api/subtitles` routes above; validate stage, language, version IDs, root-contained video identity, replacement intent, and batch item results at the daemon boundary.
- [ ] Add Rust forwarding only where required by the existing route architecture; keep the daemon authoritative and omit secret values from responses and errors.
- [ ] Verify the browser flow and invalid-input status/body cases; run `cargo test --manifest-path uav-web/Cargo.toml` when Rust changes; commit W6 and close #16 through its PR.

## Task 7: W7 — Browser Settings, Tasks, and Results (#17)

**Files:** Modify `uav-web/frontend/index.html`; create `subtitle-settings.js`, `subtitle-tasks.js`, `tests/subtitle_workflow.cjs`; extend `tests/web_settings.cjs` and `tests/task_menu.cjs`.

- [ ] Implement the real Chrome workflow over a live Web/daemon process and isolated HTTP model fixture: configure → select video → submit → inspect progress → redo selected stage → cancel/resume → inspect/publish result.
- [ ] Render profile capabilities and unknown reasoning status, stage/language selection, queue actions, stale dependencies, previous versions, and the separate model-result/publish states.
- [ ] Preserve focus and user edits during background updates; prevent duplicate submission and make failures retryable without discarding entered settings.
- [ ] Verify narrow layout, keyboard operation, empty lists, background progress, duplicate submission, and failure/retry in Chrome; commit W7 and close #17 through its PR.

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
- **Step scan:** Each task begins with the browser behavior that demonstrates its gap, then fixes the owning boundary and verifies through the browser plus only the narrow contract tests needed for unstable edge cases.
- **Type consistency:** The `Stage`, `StageProfile`, `ProfileSnapshot`, `SpeechInterval`, `SubtitleSegment`, `StageResult`, adapter, result-store, task, and HTTP names are shared unchanged by every task.
- **Review focus:** Each of the five uncovered input classes above names the browser or fixture assertion and its owning task.
- **Proportion:** The plan fixes file ownership and cross-task interfaces without reproducing #10's product requirements or dictating method bodies.

import json
from dataclasses import asdict

import pytest

from uav_downloader.subtitles import asr, profiles, settings


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_profiles_path", lambda: tmp_path / "subtitle_profiles.json")
    monkeypatch.setattr(asr.config, "_ui_prefs_path", lambda: str(tmp_path / "prefs.json"))
    monkeypatch.setattr(settings, "_settings_path", lambda: str(tmp_path / "translation_api.json"))
    monkeypatch.delenv("UAV_ASR_API_KEY", raising=False)
    for name in ("UAV_TRANSLATION_API_KEY", "ALOS_TRANSLATION_API_KEY", "JABLE_TRANSLATION_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def profile(**changes):
    values = dict(profile_id="local-asr", display_name="Local ASR", kind="asr", provider="localai",
                  capabilities=("asr",), base_url="http://192.168.1.8:8000/v1",
                  model="whisper", parameters_json='{"temperature":0}')
    values.update(changes)
    return profiles.StageProfile(**values)


def test_save_profile_validates_private_http_url_and_keeps_secret_url_bound(isolated_profiles):
    saved = profiles.save_profile(profile(), api_key="token-one")
    assert saved.provider == "localai"
    assert saved.credential_ref
    assert profiles.save_profile(profile(), api_key=None).credential_ref == saved.credential_ref
    changed = profiles.save_profile(profile(base_url="http://192.168.1.9:8000/v1"))
    assert changed.credential_ref is None
    assert profiles.resolve_credential(saved.credential_ref, saved.base_url) == "token-one"
    assert profiles.resolve_credential(changed.credential_ref, changed.base_url) == ""
    stored = (isolated_profiles / "subtitle_profiles.json").read_text()
    assert "token-one" not in stored
    assert "api_key" not in json.dumps(asdict(changed))


def test_replacing_key_for_same_endpoint_refills_pinned_snapshot_but_endpoint_change_isolated(isolated_profiles):
    original = profiles.save_profile(profile(), api_key="old-secret")
    pinned = profiles.snapshot(original.profile_id, "asr")
    refreshed = profiles.save_profile(profile(), api_key="new-secret")
    assert refreshed.credential_ref == original.credential_ref == pinned.credential_ref
    assert profiles.resolve_credential(pinned.credential_ref, "http://192.168.1.8:8000/v1") == "new-secret"

    changed = profiles.save_profile(
        profile(base_url="http://192.168.1.9:8000/v1"), api_key="other-secret")
    assert changed.credential_ref != pinned.credential_ref
    assert profiles.resolve_credential(changed.credential_ref, changed.base_url) == "other-secret"
    assert profiles.resolve_credential(pinned.credential_ref, "http://192.168.1.8:8000/v1") == "new-secret"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@host/v1", "http://host/v1?key=x", "https://host/v1#fragment", "http://host:99999/v1", "http://host/v1\r\nX: y"])
def test_invalid_profile_url_is_rejected_without_replacing_profiles(isolated_profiles, url):
    profiles.save_profile(profile())
    before = (isolated_profiles / "subtitle_profiles.json").read_bytes()
    with pytest.raises(ValueError):
        profiles.save_profile(profile(base_url=url))
    assert (isolated_profiles / "subtitle_profiles.json").read_bytes() == before


def test_parameters_are_canonical_and_snapshot_isolated_from_input_and_loaded_profile(isolated_profiles):
    params = {"limits": {"tokens": 10}, "temperature": 0.2}
    original = profile(parameters_json=json.dumps(params))
    saved = profiles.save_profile(original)
    task = profiles.snapshot(saved.profile_id, "asr")
    params["limits"]["tokens"] = 99
    loaded = profiles.load_profiles()[0]
    changed = json.loads(loaded.parameters_json)
    changed["limits"]["tokens"] = 200
    assert json.loads(saved.parameters_json) == {"limits": {"tokens": 10}, "temperature": 0.2}
    assert json.loads(task.profile_json)["parameters"]["limits"]["tokens"] == 10


def test_snapshot_rejects_unsupported_stage_and_never_contains_secret(isolated_profiles):
    saved = profiles.save_profile(profile(), api_key="secret-value")
    with pytest.raises(ValueError):
        profiles.snapshot(saved.profile_id, "translation")
    snapshot = profiles.snapshot(saved.profile_id, "asr")
    assert "secret-value" not in snapshot.profile_json
    assert snapshot.credential_ref == saved.credential_ref


def test_profiles_are_named_independent_choices(isolated_profiles):
    first = profiles.save_profile(profile())
    second = profiles.save_profile(profile(
        profile_id="alternate-asr", display_name="Alternate Whisper", model="other"))
    assert {item.display_name for item in profiles.load_profiles()} >= {
        "Local ASR", "Alternate Whisper"}
    assert first.profile_id != second.profile_id


def test_migrates_legacy_asr_and_translation_profiles_without_deleting_source(isolated_profiles, monkeypatch):
    asr.save_settings({"base_url": "http://10.0.0.5:8000/v1", "model": "old-asr", "api_key": "asr-secret"})
    (isolated_profiles / "translation_api.json").write_text(json.dumps({
        "schema": 1, "selected_provider": settings.OPENAI_COMPATIBLE,
        "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "old-text",
            "api_key_protected": "dpapi:legacy"}}}), encoding="utf-8")
    # Model a platform that can decrypt the legacy translation secret.
    monkeypatch.setattr(settings, "_unprotect_secret", lambda _value: "text-secret")
    asr_before = (isolated_profiles / "asr.json").read_bytes()
    translation_before = (isolated_profiles / "translation_api.json").read_bytes()

    loaded = profiles.load_profiles()

    assert {(item.kind, item.model) for item in loaded} >= {("asr", "old-asr"), ("text", "old-text")}
    assert next(item for item in loaded if item.kind == "asr").provider == "openai-compatible"
    assert all(item.provider for item in loaded)
    assert (isolated_profiles / "asr.json").read_bytes() == asr_before
    assert (isolated_profiles / "translation_api.json").read_bytes() == translation_before
    stored = (isolated_profiles / "subtitle_profiles.json").read_text()
    assert "asr-secret" not in stored and "text-secret" not in stored


def test_migration_write_failure_preserves_legacy_sources(isolated_profiles, monkeypatch):
    asr.save_settings({"base_url": "https://asr.example/v1", "model": "legacy", "api_key": "legacy-secret"})
    source = (isolated_profiles / "asr.json").read_bytes()
    monkeypatch.setattr(profiles, "_atomic_write", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        profiles.load_profiles()
    assert (isolated_profiles / "asr.json").read_bytes() == source
    assert not (isolated_profiles / "subtitle_profiles.json").exists()


def test_migration_keeps_asr_environment_key_bound_to_legacy_url(isolated_profiles, monkeypatch):
    asr.save_settings({"base_url": "http://10.0.0.8:8000/v1", "model": "whisper"})
    monkeypatch.setenv("UAV_ASR_API_KEY", "asr-env-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "asr")
    assert migrated.base_url == "http://10.0.0.8:8000/v1"
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "asr-env-secret"
    snapshot = profiles.snapshot(migrated.profile_id, "asr")
    assert "asr-env-secret" not in snapshot.profile_json


def test_migration_keeps_asr_environment_key_with_legacy_default(isolated_profiles, monkeypatch):
    monkeypatch.setenv("UAV_ASR_API_KEY", "asr-env-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "asr")
    assert migrated.base_url == asr.DEFAULT_URL
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "asr-env-secret"
    assert not (isolated_profiles / "asr.json").exists()


def test_migration_keeps_translation_environment_key_without_copying_value(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model"}}}), encoding="utf-8")
    monkeypatch.setenv("UAV_TRANSLATION_API_KEY", "text-env-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "text")
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "text-env-secret"
    assert "text-env-secret" not in (isolated_profiles / "subtitle_profiles.json").read_text()


def test_translation_environment_key_keeps_priority_and_protected_fallback(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model",
            "api_key_protected": "dpapi:legacy"}}}), encoding="utf-8")
    monkeypatch.setattr(settings, "_unprotect_secret", lambda _value: "protected-secret")
    monkeypatch.setenv("UAV_TRANSLATION_API_KEY", "environment-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "text")
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "environment-secret"
    monkeypatch.delenv("UAV_TRANSLATION_API_KEY")
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "protected-secret"


@pytest.mark.parametrize("name", ["max_output_tokens", "max_completion_tokens"])
def test_parameters_allow_generation_token_limits(isolated_profiles, name):
    saved = profiles.save_profile(profile(parameters_json=json.dumps({name: 1200})))
    assert json.loads(saved.parameters_json) == {name: 1200}


def test_environment_references_fail_clearly_when_environment_key_disappears(
        isolated_profiles, monkeypatch):
    asr.save_settings({"base_url": "https://asr.example/v1", "model": "whisper"})
    monkeypatch.setenv("UAV_ASR_API_KEY", "asr-environment-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "asr")
    monkeypatch.delenv("UAV_ASR_API_KEY")
    with pytest.raises(ValueError, match="API 金鑰"):
        profiles.resolve_credential(migrated.credential_ref, migrated.base_url)


def test_translation_environment_only_reference_fails_when_key_disappears(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model"}}}), encoding="utf-8")
    monkeypatch.setenv("UAV_TRANSLATION_API_KEY", "text-environment-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "text")
    monkeypatch.delenv("UAV_TRANSLATION_API_KEY")
    with pytest.raises(ValueError, match="API 金鑰"):
        profiles.resolve_credential(migrated.credential_ref, migrated.base_url)


def test_invalid_translation_environment_key_does_not_migrate_as_no_key(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model"}}}), encoding="utf-8")
    monkeypatch.setenv("UAV_TRANSLATION_API_KEY", "invalid\nkey")
    with pytest.raises(ValueError, match="API 金鑰"):
        profiles.load_profiles()
    assert source.exists()
    assert not (isolated_profiles / "subtitle_profiles.json").exists()


def test_environment_key_migrates_when_protected_fallback_is_unavailable(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model",
            "api_key_protected": "dpapi:legacy"}}}), encoding="utf-8")
    original = source.read_bytes()
    monkeypatch.setattr(settings, "_unprotect_secret", lambda _value: "")
    monkeypatch.setenv("UAV_TRANSLATION_API_KEY", "environment-secret")
    migrated = next(item for item in profiles.load_profiles() if item.kind == "text")
    assert profiles.resolve_credential(migrated.credential_ref, migrated.base_url) == "environment-secret"
    monkeypatch.delenv("UAV_TRANSLATION_API_KEY")
    with pytest.raises(ValueError, match="API 金鑰"):
        profiles.resolve_credential(migrated.credential_ref, migrated.base_url)
    assert source.read_bytes() == original


def test_corrupt_legacy_translation_json_fails_without_creating_profiles(isolated_profiles):
    source = isolated_profiles / "translation_api.json"
    source.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError, match="翻譯設定"):
        profiles.load_profiles()
    assert source.read_text(encoding="utf-8") == "{ broken"
    assert not (isolated_profiles / "subtitle_profiles.json").exists()


def test_unreadable_legacy_translation_secret_fails_without_losing_reference(
        isolated_profiles, monkeypatch):
    source = isolated_profiles / "translation_api.json"
    source.write_text(json.dumps({
        "schema": 1, "profiles": {settings.OPENAI_COMPATIBLE: {
            "base_url": "https://text.example/v1", "model": "text-model",
            "api_key_protected": "dpapi:unavailable"}}}), encoding="utf-8")
    monkeypatch.setattr(settings, "_unprotect_secret", lambda _value: "")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="API 金鑰"):
        profiles.load_profiles()
    assert source.read_bytes() == before
    assert not (isolated_profiles / "subtitle_profiles.json").exists()


@pytest.mark.parametrize("secret_name", [
    "api_key", "authorization", "password", "secret", "access_token",
    "auth_token", "refresh_token",
])
def test_parameters_cannot_expose_credentials_in_snapshots(isolated_profiles, secret_name):
    params = json.dumps({"nested": {secret_name: "must-not-leak"}})
    with pytest.raises(ValueError, match="敏感"):
        profiles.save_profile(profile(parameters_json=params))
    assert not (isolated_profiles / "subtitle_profiles.json").exists()

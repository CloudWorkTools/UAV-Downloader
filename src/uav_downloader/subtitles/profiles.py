"""Validated subtitle model profiles and immutable task snapshots."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import Literal

from uav_downloader.subtitles import asr, settings

Stage = Literal["vad", "asr", "correction", "translation"]
ProfileKind = Literal["vad", "asr", "text"]
STAGES = frozenset(("vad", "asr", "correction", "translation"))
KINDS = frozenset(("vad", "asr", "text"))
DEFAULT_PROVIDER = "localai"
_SCHEMA = 1
_LOCK = threading.RLock()


@dataclass(frozen=True)
class StageProfile:
    profile_id: str
    display_name: str
    kind: ProfileKind
    provider: str
    capabilities: tuple[Stage, ...]
    base_url: str
    model: str
    parameters_json: str
    credential_ref: str | None = None


@dataclass(frozen=True)
class ProfileSnapshot:
    profile_json: str
    credential_ref: str | None
    snapshot_id: str
    reasoning_verification_json: str | None = None


def _profiles_path() -> Path:
    return asr._folder() / "subtitle_profiles.json"


def _reasoning_path() -> Path:
    return _profiles_path().with_name("subtitle_profile_reasoning.json")


def _reasoning_key_path() -> Path:
    return _profiles_path().with_name("subtitle_profile_reasoning.key")


def _reasoning_key() -> bytes:
    path = _reasoning_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = os.open(path, flags)
        else:
            try:
                os.fchmod(descriptor, 0o600)
                key = os.urandom(32)
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return key
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("字幕驗證金鑰權限無效")
        key = os.read(descriptor, 33)
        if len(key) != 32:
            raise ValueError("字幕驗證金鑰格式無效")
        return key
    finally:
        os.close(descriptor)


def _read_reasoning_raw() -> dict:
    descriptor = None
    try:
        descriptor = os.open(_reasoning_path(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 1024 * 1024:
            raise ValueError("字幕推理驗證記錄權限無效")
        chunks = []
        remaining = 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = json.loads(b"".join(chunks).decode("utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "verifications": {}}
    except ValueError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("字幕推理驗證記錄無法讀取") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (not isinstance(raw, dict) or raw.get("schema") != 1 or
            not isinstance(raw.get("verifications"), dict)):
        raise ValueError("字幕推理驗證記錄格式錯誤")
    return raw


def _write_reasoning_raw(raw: dict) -> None:
    path = _reasoning_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".subtitle-reasoning-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(raw, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _profile_parts(profile):
    if isinstance(profile, StageProfile):
        value = asdict(profile)
        parameters = json.loads(value.pop("parameters_json"))
    elif isinstance(profile, dict):
        value = dict(profile)
        parameters = value.get("parameters", {})
    else:
        raise ValueError("字幕模型設定檔格式錯誤")
    return value, parameters


def _verification_binding(profile, stage: Stage, credential_ref=None) -> str:
    value, parameters = _profile_parts(profile)
    if stage not in ("correction", "translation"):
        raise ValueError("字幕推理驗證階段無效")
    if value.get("provider") != "localai" or stage not in value.get("capabilities", ()):
        raise ValueError("字幕模型設定檔不支援推理驗證")
    reference = value.get("credential_ref") if credential_ref is None else credential_ref
    base_url = value.get("base_url", "")
    try:
        key = resolve_credential(reference, base_url)
    except ValueError:
        raise ValueError("字幕模型服務憑證無法確認") from None
    secret = _reasoning_key()
    credential_fingerprint = hmac.new(
        secret, b"uav-subtitle-credential-v1\0" + key.encode("utf-8"), hashlib.sha256).hexdigest()
    identity = {
        "profile_id": value.get("profile_id"), "stage": stage,
        "provider": value.get("provider"), "base_url": base_url.rstrip("/"),
        "model": value.get("model"), "parameters": parameters,
        "credential_fingerprint": credential_fingerprint,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hmac.new(secret, b"uav-subtitle-reasoning-binding-v1\0" + canonical,
                    hashlib.sha256).hexdigest()


def _verification_key(profile_id: str, stage: Stage) -> str:
    return f"{profile_id}:{stage}"


def _signed_verification(data: dict) -> dict:
    value = dict(data)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    value["signature"] = hmac.new(
        _reasoning_key(), b"uav-subtitle-reasoning-evidence-v1\0" + canonical,
        hashlib.sha256).hexdigest()
    return value


def _valid_verification(record: object) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("signature"), str):
        return False
    data = {key: value for key, value in record.items() if key != "signature"}
    try:
        expected = _signed_verification(data)["signature"]
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(expected, record["signature"])


def _current_profile(profile_id: str) -> StageProfile:
    profile = next((item for item in load_profiles() if item.profile_id == profile_id), None)
    if profile is None:
        raise ValueError("找不到字幕模型設定檔")
    return profile


def _new_verification(profile: StageProfile, stage: Stage, binding: str, status: str,
                      scope: str, reason: str | None, attempt_id: str | None = None) -> dict:
    if status not in {"pending", "supported", "unsupported", "unknown"}:
        raise ValueError("字幕推理驗證狀態無效")
    if scope != "localai false/true contrast":
        raise ValueError("字幕推理驗證範圍無效")
    if reason not in (None, "reasoning contrast passed", "reasoning signal missing or indistinguishable",
                      "probe failed", "profile changed during probe"):
        reason = "probe failed" if reason else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    data = {"profile_id": profile.profile_id, "stage": stage, "status": status,
            "binding": binding, "verified_at": now if status != "pending" else None,
            "scope": scope, "reason": reason, "attempt_id": attempt_id}
    return _signed_verification(data)


def begin_reasoning_verification(profile_id: str, stage: Stage) -> dict:
    """Invalidate old evidence and atomically start a fresh daemon-side probe."""
    with _LOCK:
        profile = _current_profile(profile_id)
        binding = _verification_binding(profile, stage)
        attempt_id = os.urandom(16).hex()
        raw = _read_reasoning_raw()
        raw["verifications"][_verification_key(profile_id, stage)] = _new_verification(
            profile, stage, binding, "pending", "localai false/true contrast", None,
            attempt_id)
        _write_reasoning_raw(raw)
        return {"binding": binding, "attempt_id": attempt_id}


def record_reasoning_verification(profile_id: str, stage: Stage, attempt: dict, *,
                                  status: str, reason: str | None = None) -> bool:
    """Store a test result only if the profile and effective credential still match."""
    with _LOCK:
        try:
            profile = _current_profile(profile_id)
            current = _verification_binding(profile, stage)
        except ValueError:
            return False
        if not isinstance(attempt, dict):
            return False
        raw = _read_reasoning_raw()
        key = _verification_key(profile_id, stage)
        pending = raw["verifications"].get(key)
        binding = attempt.get("binding")
        attempt_id = attempt.get("attempt_id")
        if (not _valid_verification(pending) or pending.get("status") != "pending" or
                pending.get("attempt_id") != attempt_id or not isinstance(binding, str) or
                pending.get("binding") != binding or
                not hmac.compare_digest(current, binding)):
            return False
        raw["verifications"][key] = _new_verification(
            profile, stage, binding, status, "localai false/true contrast", reason)
        _write_reasoning_raw(raw)
        return True


def reasoning_verification(profile: StageProfile, stage: Stage) -> dict:
    """Return a redacted status for the current profile and effective credential."""
    result = {"status": "unknown", "verified_at": None,
              "scope": "localai false/true contrast", "stale_reason": "尚未驗證"}
    try:
        raw = _read_reasoning_raw()
        record = raw["verifications"].get(_verification_key(profile.profile_id, stage))
        if record is None:
            return result
        binding = _verification_binding(profile, stage)
    except (OSError, ValueError):
        result["stale_reason"] = "設定或憑證無法確認，請重新驗證"
        return result
    if not _valid_verification(record):
        result["stale_reason"] = "驗證記錄無效，請重新驗證"
        return result
    result["verified_at"] = record.get("verified_at")
    result["scope"] = record.get("scope")
    if record.get("binding") != binding:
        result["stale_reason"] = "設定或憑證已變更，請重新驗證"
        return result
    if record.get("status") == "supported":
        result.update(status="supported", stale_reason=None)
    elif record.get("status") == "unsupported":
        result.update(status="unsupported", stale_reason="此設定未通過停用思考驗證")
    elif record.get("status") == "pending":
        result["stale_reason"] = "驗證進行中"
    else:
        result["stale_reason"] = "驗證未完成，請重新驗證"
    return result


def snapshot_reasoning_verified(snapshot: ProfileSnapshot, stage: Stage) -> bool:
    """Authenticate snapshot evidence and bind it to its captured config and live credential."""
    if not isinstance(snapshot, ProfileSnapshot) or not snapshot.reasoning_verification_json:
        return False
    try:
        record = json.loads(snapshot.reasoning_verification_json)
        profile = json.loads(snapshot.profile_json)
        if (not isinstance(record, dict) or not isinstance(profile, dict) or
                record.get("status") != "supported" or record.get("stage") != stage or
                record.get("profile_id") != profile.get("profile_id") or
                not _valid_verification(record)):
            return False
        binding = _verification_binding(profile, stage, snapshot.credential_ref)
        return hmac.compare_digest(binding, str(record.get("binding", "")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _canonical_parameters(value: str) -> str:
    try:
        parameters = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("模型參數必須是 JSON 物件") from None
    if not isinstance(parameters, dict):
        raise ValueError("模型參數必須是 JSON 物件")
    sensitive_names = ("authorization", "password", "secret", "credential", "apikey")
    token_secret_names = {
        "token", "accesstoken", "authtoken", "refreshtoken", "idtoken",
        "bearertoken", "apitoken", "clienttoken", "tokenvalue", "tokensecret",
    }

    def check_keys(item):
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = re.sub(r"[-_]", "", key.lower()) if isinstance(key, str) else ""
                if any(name in normalized for name in sensitive_names) or normalized in token_secret_names:
                    raise ValueError("模型參數不得包含敏感憑證欄位")
                check_keys(nested)
        elif isinstance(item, list):
            for nested in item:
                check_keys(nested)

    check_keys(parameters)
    try:
        return json.dumps(parameters, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("模型參數必須只包含 JSON 值") from None


def _validate_profile(profile: StageProfile) -> StageProfile:
    if not isinstance(profile, StageProfile):
        raise ValueError("模型設定檔格式錯誤")
    if (not isinstance(profile.profile_id, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile.profile_id)):
        raise ValueError("模型設定檔 ID 格式錯誤")
    if (not isinstance(profile.display_name, str) or not profile.display_name.strip() or
            len(profile.display_name) > 120 or re.search(r"[\x00-\x1f\x7f]", profile.display_name)):
        raise ValueError("模型設定檔名稱格式錯誤")
    if profile.kind not in KINDS:
        raise ValueError("模型設定檔類型無效")
    if not isinstance(profile.provider, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", profile.provider):
        raise ValueError("模型服務名稱格式錯誤")
    capabilities = tuple(profile.capabilities)
    allowed = {"vad": {"vad"}, "asr": {"asr"}, "text": {"correction", "translation"}}[profile.kind]
    if (not capabilities or len(set(capabilities)) != len(capabilities) or
            not set(capabilities) <= allowed):
        raise ValueError("模型設定檔階段能力無效")
    if (not isinstance(profile.model, str) or not profile.model.strip() or
            len(profile.model) > 160 or re.search(r"[\x00-\x20\x7f]", profile.model)):
        raise ValueError("模型名稱格式錯誤")
    if not isinstance(profile.base_url, str):
        raise ValueError("模型服務網址格式錯誤")
    # Empty URLs are allowed for an unconfigured LocalAI profile. Never invent a host.
    if profile.base_url:
        validated = asr._validate({"base_url": profile.base_url, "model": profile.model})
        base_url = validated["base_url"]
    else:
        base_url = ""
    canonical_parameters = _canonical_parameters(profile.parameters_json)
    return StageProfile(
        profile_id=profile.profile_id, display_name=profile.display_name.strip(),
        kind=profile.kind, provider=profile.provider,
        capabilities=capabilities, base_url=base_url, model=profile.model.strip(),
        parameters_json=canonical_parameters,
        credential_ref=profile.credential_ref,
    )


def validate_stage_parameters(kind, provider, parameters):
    """Apply the same pure parameter contract used by the selected adapter."""
    if kind == "asr":
        if provider not in ("localai", "openai-compatible"):
            raise ValueError("所選模型服務不支援時間戳語音辨識")
        return asr.validate_asr_parameters(parameters)
    if kind == "vad":
        if provider != "localai":
            raise ValueError("所選模型服務不支援獨立語音活動偵測")
        from uav_downloader.subtitles.vad import validate_vad_parameters
        return validate_vad_parameters(parameters)
    if kind == "text":
        from uav_downloader.subtitles.llm import _validate_text_parameters
        try:
            return _validate_text_parameters(provider, parameters)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
    raise ValueError("模型設定檔類型無效")


def _read_raw() -> dict:
    try:
        raw = json.loads(_profiles_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": _SCHEMA, "profiles": [], "credentials": {}}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("字幕模型設定檔無法讀取") from None
    if (not isinstance(raw, dict) or raw.get("schema") != _SCHEMA or
            not isinstance(raw.get("profiles"), list) or
            not isinstance(raw.get("credentials"), dict)):
        raise ValueError("字幕模型設定檔格式錯誤")
    return raw


def _atomic_write(raw: dict) -> None:
    path = _profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".subtitle-profiles-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(raw, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _translation_environment_is_configured(provider: str) -> bool:
    names = ["UAV_TRANSLATION_API_KEY", "ALOS_TRANSLATION_API_KEY", "JABLE_TRANSLATION_API_KEY"]
    names.extend({
        settings.OPENAI: ("OPENAI_API_KEY",),
        settings.ANTHROPIC: ("ANTHROPIC_API_KEY",),
        settings.GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }.get(provider, ()))
    return any(os.environ.get(name) for name in names)


def _from_record(record: object) -> StageProfile:
    if not isinstance(record, dict):
        raise ValueError("字幕模型設定檔格式錯誤")
    return _validate_profile(StageProfile(
        profile_id=record.get("profile_id"), display_name=record.get("display_name"), kind=record.get("kind"),
        provider=record.get("provider"), capabilities=tuple(record.get("capabilities", ())),
        base_url=record.get("base_url"), model=record.get("model"),
        parameters_json=record.get("parameters_json", "{}"),
        credential_ref=record.get("credential_ref"),
    ))


def _migrate_legacy() -> None:
    if _profiles_path().exists():
        return
    legacy_profiles: list[StageProfile] = []
    credentials: list[tuple[int, dict]] = []
    asr_path = asr._folder() / "asr.json"
    if asr_path.exists() or os.environ.get("UAV_ASR_API_KEY"):
        old = asr.load_settings(secret=True)
        asr_secret_saved = bool(asr._read().get("api_key_encrypted"))
        record = _validate_profile(StageProfile(
            profile_id="legacy-asr", display_name="Legacy ASR", kind="asr", provider="openai-compatible",
            capabilities=("asr",), base_url=old["base_url"], model=old["model"],
            parameters_json="{}"))
        legacy_profiles.append(record)
        if old.get("api_key") and asr_secret_saved:
            credentials.append((0, {"base_url": old["base_url"],
                                    "encrypted": asr._encrypt_secret(old["api_key"])}))
        elif os.environ.get("UAV_ASR_API_KEY"):
            credentials.append((0, {"base_url": old["base_url"],
                                    "environment": "UAV_ASR_API_KEY"}))
    translation_path = Path(settings._settings_path())
    if translation_path.exists():
        try:
            legacy = json.loads(translation_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("舊翻譯設定無法讀取，原設定已保留") from None
        if (not isinstance(legacy, dict) or
                legacy.get("schema", settings._SCHEMA) != settings._SCHEMA or
                not isinstance(legacy.get("profiles", {}), dict)):
            raise ValueError("舊翻譯設定格式錯誤，原設定已保留")
        provider_profiles = legacy.get("profiles", {})
        for provider in settings.PROVIDERS:
            if provider == settings.LOCAL or not isinstance(provider_profiles.get(provider), dict):
                continue
            old_profile = provider_profiles[provider]
            protected = old_profile.get("api_key_protected")
            environment_secret = settings._environment_key(provider)
            if "api_key_protected" in old_profile and protected:
                if not isinstance(protected, str) or not protected.startswith("dpapi:"):
                    if not environment_secret:
                        raise ValueError("舊翻譯 API 金鑰格式無效，請重新輸入金鑰")
                    secret = ""
                else:
                    secret = settings._unprotect_secret(protected)
                    if not secret and not environment_secret:
                        raise ValueError("舊翻譯 API 金鑰無法解密，請重新輸入金鑰")
            else:
                secret = ""
            if (_translation_environment_is_configured(provider) and
                    not environment_secret and not secret):
                raise ValueError("環境翻譯 API 金鑰格式無效，請重新設定金鑰")
            translated = settings.get_translation_settings(provider)
            record = _validate_profile(StageProfile(
                profile_id="legacy-text-" + provider.replace("-", "_"), display_name="Legacy " + provider,
                kind="text",
                provider=provider, capabilities=("correction", "translation"),
                base_url=translated.base_url, model=translated.model,
                parameters_json=json.dumps({"quality": translated.quality})))
            legacy_profiles.append(record)
            if secret or environment_secret:
                credential = {"base_url": translated.base_url}
                if environment_secret:
                    credential["environment_provider"] = provider
                if secret:
                    credential["encrypted"] = asr._encrypt_secret(secret)
                credentials.append((len(legacy_profiles) - 1, credential))
    raw = {"schema": _SCHEMA, "profiles": [], "credentials": {}}
    for index, profile in enumerate(legacy_profiles):
        if any(credential_index == index for credential_index, _ in credentials):
            _, credential = next(item for item in credentials if item[0] == index)
            reference = os.urandom(16).hex()
            profile = StageProfile(**{**asdict(profile), "credential_ref": reference})
            raw["credentials"][reference] = credential
        raw["profiles"].append(asdict(profile))
    if legacy_profiles:
        _atomic_write(raw)


def load_profiles() -> list[StageProfile]:
    with _LOCK:
        _migrate_legacy()
        raw = _read_raw()
        return [_from_record(record) for record in raw["profiles"]]


def save_profile(profile: StageProfile, api_key: str | None = None) -> StageProfile:
    profile = _validate_profile(profile)
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 4096 or
                                re.search(r"[\x00-\x20\x7f]", api_key)):
        raise ValueError("API 金鑰格式錯誤")
    with _LOCK:
        _migrate_legacy()
        raw = _read_raw()
        prior = next((item for item in raw["profiles"]
                      if isinstance(item, dict) and item.get("profile_id") == profile.profile_id), None)
        old_ref = prior.get("credential_ref") if isinstance(prior, dict) else None
        secret = api_key or ""
        if not secret and prior and prior.get("base_url") == profile.base_url and old_ref:
            entry = raw["credentials"].get(old_ref)
            if isinstance(entry, dict) and entry.get("base_url") == profile.base_url:
                profile = StageProfile(**{**asdict(profile), "credential_ref": old_ref})
        elif secret:
            reference = old_ref if prior and prior.get("base_url") == profile.base_url and old_ref else os.urandom(16).hex()
            profile = StageProfile(**{**asdict(profile), "credential_ref": reference})
            raw["credentials"][reference] = {
                "base_url": profile.base_url, "encrypted": asr._encrypt_secret(secret)}
        else:
            profile = StageProfile(**{**asdict(profile), "credential_ref": None})
        raw["profiles"] = [item for item in raw["profiles"]
                           if not isinstance(item, dict) or item.get("profile_id") != profile.profile_id]
        raw["profiles"].append(asdict(profile))
        _atomic_write(raw)
        return profile


def snapshot(profile_id: str, stage: Stage) -> ProfileSnapshot:
    profile = next((item for item in load_profiles() if item.profile_id == profile_id), None)
    if profile is None:
        raise ValueError("找不到字幕模型設定檔")
    if stage not in profile.capabilities:
        raise ValueError("模型設定檔不支援指定階段")
    public = asdict(profile)
    parameters = json.loads(public.pop("parameters_json"))
    public["parameters"] = parameters
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence = None
    if stage in ("correction", "translation") and profile.provider == "localai":
        try:
            record = _read_reasoning_raw()["verifications"].get(
                _verification_key(profile.profile_id, stage))
            if isinstance(record, dict) and _valid_verification(record) and record.get("status") == "supported":
                binding = _verification_binding(profile, stage)
                if record.get("binding") == binding:
                    evidence = json.dumps(record, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"))
        except (OSError, ValueError):
            evidence = None
    import hashlib
    identity_input = encoded if evidence is None else encoded + "\0" + evidence
    identity = hashlib.sha256(identity_input.encode("utf-8")).hexdigest()
    return ProfileSnapshot(encoded, profile.credential_ref, identity, evidence)


def resolve_credential(credential_ref: str | None, base_url: str) -> str:
    if not credential_ref:
        return ""
    with _LOCK:
        raw = _read_raw()
        entry = raw["credentials"].get(credential_ref)
        if not isinstance(entry, dict) or entry.get("base_url") != base_url:
            raise ValueError("模型服務位址已變更，請重新輸入 API 金鑰")
        if entry.get("environment"):
            secret = os.environ.get(entry["environment"], "")
            if len(secret) > 4096 or re.search(r"[\x00-\x20\x7f]", secret):
                raise ValueError("環境 API 金鑰格式錯誤")
            if not secret:
                raise ValueError("環境 API 金鑰不存在，請重新設定金鑰")
            return secret
        if entry.get("environment_provider"):
            secret = settings._environment_key(entry["environment_provider"])
            if secret:
                return secret
        if "encrypted" not in entry:
            raise ValueError("環境 API 金鑰不存在，請重新設定金鑰")
        try:
            return asr._decrypt_secret(entry["encrypted"])
        except Exception:
            raise ValueError("模型服務 API 金鑰無法解密，請重新輸入") from None


def capability_report(profile_snapshot: ProfileSnapshot) -> dict:
    """Report adapter support separately from any model-list discovery."""
    try:
        profile = json.loads(profile_snapshot.profile_json)
    except (AttributeError, TypeError, json.JSONDecodeError):
        raise ValueError("模型設定快照格式錯誤") from None
    if not isinstance(profile, dict):
        raise ValueError("模型設定快照格式錯誤")
    provider, kind = profile.get("provider"), profile.get("kind")
    if kind == "vad" and provider == "localai":
        supported = {"vad"}
    elif kind == "asr" and provider in {"localai", "openai-compatible"}:
        supported = {"asr"}
    elif kind == "text" and provider in {
            "openai", "anthropic", "gemini", "openai-compatible", "localai",
            "ollama", "lmstudio"}:
        supported = {"correction", "translation"}
    else:
        supported = set()
    declared = profile.get("capabilities", ())
    if not isinstance(declared, list):
        declared = ()
    stages = tuple(stage for stage in ("vad", "asr", "correction", "translation")
                   if stage in supported and stage in declared)
    return {
        "provider": provider,
        "model": profile.get("model"),
        "stages": stages,
        "reasoning_disable": (
            "requires_successful_native_request" if provider == "lmstudio" else
            "api_show_thinking_values_must_include_false_then_think_false" if provider == "ollama" else
            "documented_request_controls_without_machine_confirmation" if provider == "localai" else
            "unknown"),
        "vad_parameters": (
            "service_side_model_configuration" if kind == "vad" and provider == "localai" else None),
    }

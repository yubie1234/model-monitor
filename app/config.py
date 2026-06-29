"""설정 로딩.

서버/LiteLLM/k8s 공통 설정은 환경변수(pydantic-settings)로, backend_count·backends·
namespace_overrides 같은 풍부한 중첩 설정은 설정 파일(.json/.yaml)로 받는다.
우선순위: 환경변수 > 설정 파일 > 기본값 (원래 CLI 가 사라진 자리를 env 가 대신).

수집기(app.services.*)와 K8sClient.from_settings 는 평범한 dict 를 받으므로,
build_collector_settings() 가 Settings + 파일을 합쳐 그 dict 를 만들어 준다.
이렇게 하면 단위 테스트는 여전히 순수 dict 를 넘겨 검증할 수 있다.
"""

import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class Settings(BaseSettings):
    """환경변수 기반 설정. .env 파일도 읽는다."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 웹 서버 ---
    host: str = Field("0.0.0.0",
                      validation_alias=AliasChoices("MONITOR_HOST", "HOST"))
    port: int = Field(8088,
                      validation_alias=AliasChoices("MONITOR_PORT", "PORT"))
    interval: float = Field(5.0, validation_alias=AliasChoices("MONITOR_INTERVAL"))
    demo: bool = Field(False, validation_alias=AliasChoices("MONITOR_DEMO"))

    # --- LiteLLM ---
    litellm_url: Optional[str] = Field(
        None, validation_alias=AliasChoices("LITELLM_BASE_URL", "MONITOR_LITELLM_URL"))
    api_key: Optional[str] = Field(
        None, validation_alias=AliasChoices("LITELLM_API_KEY", "MONITOR_API_KEY"))
    timeout: float = Field(10.0, validation_alias=AliasChoices("MONITOR_TIMEOUT"))
    health: bool = Field(True, validation_alias=AliasChoices("MONITOR_HEALTH"))
    health_timeout: float = Field(
        90.0, validation_alias=AliasChoices("MONITOR_HEALTH_TIMEOUT"))

    # --- 백엔드 직접 probe ---
    probe_backends: bool = Field(
        False, validation_alias=AliasChoices("MONITOR_PROBE_BACKENDS"))

    # --- k8s backend 개수(LB 뒤 Pod 수) ---
    backend_count: bool = Field(
        True, validation_alias=AliasChoices("MONITOR_BACKEND_COUNT"))
    gpu_info: bool = Field(
        True, validation_alias=AliasChoices("MONITOR_GPU_INFO"))
    k8s_api_server: Optional[str] = Field(
        None, validation_alias=AliasChoices("MONITOR_K8S_API_SERVER"))
    k8s_token_file: str = Field(
        _SA_DIR + "/token",
        validation_alias=AliasChoices("MONITOR_K8S_TOKEN_FILE"))
    k8s_ca_file: str = Field(
        _SA_DIR + "/ca.crt",
        validation_alias=AliasChoices("MONITOR_K8S_CA_FILE"))
    k8s_insecure: bool = Field(
        False, validation_alias=AliasChoices("MONITOR_K8S_INSECURE"))
    k8s_timeout: float = Field(5.0, validation_alias=AliasChoices("MONITOR_K8S_TIMEOUT"))

    # --- per-user(키별) 뷰 ---
    user_view: bool = Field(
        False, validation_alias=AliasChoices("MONITOR_USER_VIEW"))
    user_view_show_internal: bool = Field(
        False, validation_alias=AliasChoices("MONITOR_USER_VIEW_SHOW_INTERNAL"))
    user_view_cache_ttl: float = Field(
        30.0, validation_alias=AliasChoices("MONITOR_USER_VIEW_CACHE_TTL"))

    # --- Prometheus /metrics ---
    metrics: bool = Field(
        True, validation_alias=AliasChoices("MONITOR_METRICS"))

    # --- 설정 파일 경로 (중첩 설정 출처) ---
    config_file: Optional[str] = Field(
        None, validation_alias=AliasChoices("MONITOR_CONFIG_FILE", "CONFIG_FILE"))


def load_config_file(path: str) -> Dict[str, Any]:
    """JSON 우선, .yaml/.yml 은 PyYAML 있으면 사용."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "config '%s' 는 YAML 인데 PyYAML 이 없습니다. "
                "JSON 설정(.json)을 쓰거나 PyYAML 을 설치하세요." % path
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _env_set(*names: str) -> bool:
    """env 변수 중 하나라도 설정돼 있으면 True (env > file 판정용)."""
    return any(n in os.environ for n in names)


def _pick(env_present: bool, env_value, file_value, default):
    """env 가 명시됐으면 env, 아니면 파일, 아니면 기본값."""
    if env_present:
        return env_value
    if file_value is not None:
        return file_value
    return default


def build_collector_settings(settings: Settings) -> Dict[str, Any]:
    """Settings(env) + 설정 파일을 합쳐 수집기가 쓰는 평범한 dict 로 변환.

    수집기/K8sClient.from_settings 가 기대하는 키 형태를 그대로 만든다.
    """
    cfg = load_config_file(settings.config_file) if settings.config_file else {}
    litellm = cfg.get("litellm") if isinstance(cfg.get("litellm"), dict) else {}
    bc = cfg.get("backend_count") if isinstance(cfg.get("backend_count"), dict) else {}
    uv = cfg.get("user_view") if isinstance(cfg.get("user_view"), dict) else {}
    mt = cfg.get("metrics") if isinstance(cfg.get("metrics"), dict) else {}

    backend_count = _pick(
        _env_set("MONITOR_BACKEND_COUNT"),
        settings.backend_count, bc.get("enabled"), True)
    # GPU 수집은 backend_count 가 켜져 있어야 의미가 있다(같은 k8s 클라이언트 사용).
    gpu_info = backend_count and _pick(
        _env_set("MONITOR_GPU_INFO"),
        settings.gpu_info, bc.get("gpu_info"), True)
    user_view = _pick(
        _env_set("MONITOR_USER_VIEW"),
        settings.user_view, uv.get("enabled"), False)
    show_internal = _pick(
        _env_set("MONITOR_USER_VIEW_SHOW_INTERNAL"),
        settings.user_view_show_internal, uv.get("show_internal"), False)

    return {
        "litellm_url": _pick(
            _env_set("LITELLM_BASE_URL", "MONITOR_LITELLM_URL"),
            settings.litellm_url, litellm.get("url"), None),
        "api_key": _pick(
            _env_set("LITELLM_API_KEY", "MONITOR_API_KEY"),
            settings.api_key, litellm.get("api_key"), None),
        "backends": cfg.get("backends", []) or [],
        "probe_backends": _pick(
            _env_set("MONITOR_PROBE_BACKENDS"),
            settings.probe_backends, cfg.get("probe_backends"), False),
        "timeout": _pick(
            _env_set("MONITOR_TIMEOUT"),
            settings.timeout, litellm.get("timeout"), 10.0),
        "health": _pick(
            _env_set("MONITOR_HEALTH"),
            settings.health, litellm.get("health"), True),
        "health_timeout": float(_pick(
            _env_set("MONITOR_HEALTH_TIMEOUT"),
            settings.health_timeout, litellm.get("health_timeout"), 90.0)),
        # --- k8s backend 개수 / GPU ---
        "backend_count": backend_count,
        "gpu_info": gpu_info,
        "k8s_api_server": _pick(
            _env_set("MONITOR_K8S_API_SERVER"),
            settings.k8s_api_server, bc.get("api_server"), None),
        "k8s_token_file": _pick(
            _env_set("MONITOR_K8S_TOKEN_FILE"),
            settings.k8s_token_file, bc.get("token_file"),
            _SA_DIR + "/token"),
        "k8s_ca_file": _pick(
            _env_set("MONITOR_K8S_CA_FILE"),
            settings.k8s_ca_file, bc.get("ca_file"), _SA_DIR + "/ca.crt"),
        "k8s_insecure": bool(_pick(
            _env_set("MONITOR_K8S_INSECURE"),
            settings.k8s_insecure, bc.get("insecure"), False)),
        "k8s_timeout": float(_pick(
            _env_set("MONITOR_K8S_TIMEOUT"),
            settings.k8s_timeout, bc.get("timeout"), 5.0)),
        "default_namespace": bc.get("default_namespace"),
        "namespace_overrides": bc.get("namespace_overrides", {}) or {},
        "activator_namespace": bc.get("activator_namespace", "knative-serving"),
        # --- per-user(키별) 뷰 ---
        "user_view": user_view,
        "user_view_hide_internal": not show_internal,
        "user_view_cache_ttl": float(_pick(
            _env_set("MONITOR_USER_VIEW_CACHE_TTL"),
            settings.user_view_cache_ttl, uv.get("cache_ttl"), 30.0)),
        # --- Prometheus /metrics ---
        "metrics": _pick(
            _env_set("MONITOR_METRICS"),
            settings.metrics, mt.get("enabled"), True),
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()

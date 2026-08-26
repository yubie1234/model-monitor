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
    # path prefix 뒤에 서비스를 둘 때(예: example.com/service/model-monitor).
    # FastAPI root_path + 대시보드 자기 호출 URL 접두사로 쓰인다. 비우면 루트(/).
    # Ingress 가 이 prefix 를 떼고(rewrite) 앱에 전달하는 전제.
    root_path: str = Field("", validation_alias=AliasChoices("MONITOR_ROOT_PATH"))

    # --- LiteLLM ---
    litellm_url: Optional[str] = Field(
        None, validation_alias=AliasChoices("LITELLM_BASE_URL", "MONITOR_LITELLM_URL"))
    api_key: Optional[str] = Field(
        None, validation_alias=AliasChoices("LITELLM_API_KEY", "MONITOR_API_KEY"))
    timeout: float = Field(10.0, validation_alias=AliasChoices("MONITOR_TIMEOUT"))
    # 전량 /health 는 LiteLLM 이 **모든** 백엔드를 실제 ping 한다 — Serverless
    # (scale-to-zero) 백엔드를 30초마다 깨우거나 scale-down 을 영구히 막는 부하라,
    # 모르는 채 켜지지 않게 기본 off. 필요하면 명시적으로 MONITOR_HEALTH=true.
    health: bool = Field(False, validation_alias=AliasChoices("MONITOR_HEALTH"))
    health_timeout: float = Field(
        90.0, validation_alias=AliasChoices("MONITOR_HEALTH_TIMEOUT"))
    # 선택적 health: 전량 /health 가 꺼져 있을 때(health=false), k8s 판정으로
    # 안전한 모델만 /health?model= 개별 체크 (scale-to-zero 는 깨우지 않음).
    # 설계상 fail-safe(위험 판정·판정불가 KServe 는 체크 안 함)라 기본 on — 전량
    # /health 를 끈 기본 상태에서도 능동 체크가 통째로 사라지지 않게 한다.
    selective_health: bool = Field(
        True, validation_alias=AliasChoices("MONITOR_SELECTIVE_HEALTH"))

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

    # --- 지금 부하(백엔드 엔진 게이지) ---
    # vLLM/SGLang 이 노출하는 /metrics 를 Pod 마다 읽어 "지금 바쁜가"를 판정한다.
    # Pod 주소는 GPU 집계가 이미 받아오는 Pod 목록에서 나오므로 k8s 호출이 늘지 않고,
    # 백엔드에는 Pod 당 사이클마다 1회 GET 이 추가된다(응답은 보통 수십 KB).
    load: bool = Field(True, validation_alias=AliasChoices("MONITOR_LOAD"))
    # 죽은 Pod 하나가 사이클을 잡아먹지 않게 짧게. 한 라운드 최악 =
    # ceil(Pod수/load_threads) * load_timeout.
    # 부하 조회 주기(초). 비우면 스냅샷 갱신 주기(MONITOR_INTERVAL)를 따른다.
    # Pod 마다 /metrics 를 읽는 팬아웃이라, 백엔드 부담을 줄이려면 여기만 늘리면 된다
    # (스냅샷 갱신은 그대로 두고).
    load_interval: Optional[float] = Field(
        None, validation_alias=AliasChoices("MONITOR_LOAD_INTERVAL"))
    load_timeout: float = Field(
        3.0, validation_alias=AliasChoices("MONITOR_LOAD_TIMEOUT"))
    load_threads: int = Field(
        12, validation_alias=AliasChoices("MONITOR_LOAD_THREADS"))
    # Pod 직접 조회가 막힌 환경(NetworkPolicy·mTLS)에서 같은 게이지를 대신 읽을
    # 외부 Prometheus. 출처가 같아 정확도는 동일하고 스크레이프 주기만큼 늦다.
    prometheus_url: Optional[str] = Field(
        None, validation_alias=AliasChoices("MONITOR_PROMETHEUS_URL",
                                            "PROMETHEUS_URL"))
    # 한 model_name 에 backend 가 여러 개일 때 모델 등급을 무엇으로 볼지.
    # least-busy(기본): 다음 요청이 갈 가장 한가한 backend 기준.
    # shuffle: LiteLLM 기본 라우팅(simple-shuffle)처럼 요청이 흩어지면 가장 나쁜
    #          backend 기준이 정직하다. LiteLLM 의 routing_strategy 에 맞춘다.
    load_routing: str = Field(
        "least-busy", validation_alias=AliasChoices("MONITOR_LOAD_ROUTING"))
    prometheus_first: bool = Field(
        False, validation_alias=AliasChoices("MONITOR_PROMETHEUS_FIRST"))
    prometheus_lookback: str = Field(
        "2m", validation_alias=AliasChoices("MONITOR_PROMETHEUS_LOOKBACK"))

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
    # 키 필수 모드에서 /metrics 스크레이프용 Bearer 토큰(admin 키 없이 인증).
    # Prometheus scrape 의 authorization credentials / PodMonitor secretKeyRef 로 전달.
    metrics_token: Optional[str] = Field(
        None, validation_alias=AliasChoices("MONITOR_METRICS_TOKEN"))

    # --- 설정 파일 경로 (중첩 설정 출처) ---
    config_file: Optional[str] = Field(
        None, validation_alias=AliasChoices("MONITOR_CONFIG_FILE", "CONFIG_FILE"))


def normalize_root_path(root_path: str) -> str:
    """root_path 정규화: 앞에 '/' 보장, 뒤 '/' 제거. 비면 '' (루트).

    예) 'service/model-monitor/' -> '/service/model-monitor', '' -> '', '/' -> ''
    """
    rp = (root_path or "").strip()
    if not rp or rp == "/":
        return ""
    if not rp.startswith("/"):
        rp = "/" + rp
    return rp.rstrip("/")


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
    ld = cfg.get("load") if isinstance(cfg.get("load"), dict) else {}
    pm = cfg.get("prometheus") if isinstance(cfg.get("prometheus"), dict) else {}

    backend_count = _pick(
        _env_set("MONITOR_BACKEND_COUNT"),
        settings.backend_count, bc.get("enabled"), True)
    # GPU 수집은 backend_count 가 켜져 있어야 의미가 있다(같은 k8s 클라이언트 사용).
    gpu_info = backend_count and _pick(
        _env_set("MONITOR_GPU_INFO"),
        settings.gpu_info, bc.get("gpu_info"), True)
    # 부하 수집은 Pod 주소(=k8s 조회)에 기댄다. backend_count 가 꺼지면 Pod 주소를
    # 못 얻어 LB 폴백만 남는데, 그건 scale-to-zero 를 깨울 수 있어 허용하지 않는다.
    # -> gpu_info 와 같은 종속 규칙.
    load_enabled = backend_count and _pick(
        _env_set("MONITOR_LOAD"), settings.load, ld.get("enabled"), True)
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
            settings.health, litellm.get("health"), False),
        "health_timeout": float(_pick(
            _env_set("MONITOR_HEALTH_TIMEOUT"),
            settings.health_timeout, litellm.get("health_timeout"), 90.0)),
        "selective_health": _pick(
            _env_set("MONITOR_SELECTIVE_HEALTH"),
            settings.selective_health, litellm.get("selective_health"), True),
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
        # --- 지금 부하 ---
        "load": load_enabled,
        "load_timeout": float(_pick(
            _env_set("MONITOR_LOAD_TIMEOUT"),
            settings.load_timeout, ld.get("timeout"), 3.0)),
        "load_threads": int(_pick(
            _env_set("MONITOR_LOAD_THREADS"),
            settings.load_threads, ld.get("threads"), 12)),
        "load_interval": _pick(
            _env_set("MONITOR_LOAD_INTERVAL"),
            settings.load_interval, ld.get("interval"), None),
        "load_thresholds": ld.get("thresholds") or {},
        "load_routing": _pick(
            _env_set("MONITOR_LOAD_ROUTING"),
            settings.load_routing, ld.get("routing"), "least-busy"),
        "prometheus_url": _pick(
            _env_set("MONITOR_PROMETHEUS_URL", "PROMETHEUS_URL"),
            settings.prometheus_url, pm.get("url"), None),
        "prometheus_first": _pick(
            _env_set("MONITOR_PROMETHEUS_FIRST"),
            settings.prometheus_first, pm.get("first"), False),
        "prometheus_lookback": _pick(
            _env_set("MONITOR_PROMETHEUS_LOOKBACK"),
            settings.prometheus_lookback, pm.get("lookback"), "2m"),
        "prometheus_timeout": float(pm.get("timeout") or 10.0),
        "prometheus_labels": pm.get("labels") or {},
        "prometheus_api_key": pm.get("api_key"),
        "user_view": user_view,
        "user_view_hide_internal": not show_internal,
        "user_view_cache_ttl": float(_pick(
            _env_set("MONITOR_USER_VIEW_CACHE_TTL"),
            settings.user_view_cache_ttl, uv.get("cache_ttl"), 30.0)),
        # --- Prometheus /metrics ---
        "metrics": _pick(
            _env_set("MONITOR_METRICS"),
            settings.metrics, mt.get("enabled"), True),
        "metrics_token": _pick(
            _env_set("MONITOR_METRICS_TOKEN"),
            settings.metrics_token, mt.get("token"), None),
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()

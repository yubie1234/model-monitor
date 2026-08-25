"""스냅샷 API 응답 스키마 (OpenAPI 문서/타입용).

스냅샷은 수집 결과를 담는 동적 dict 라서, 모델은 모든 필드를 Optional 로 두고
extra='allow' 로 둔다. 새 필드가 추가돼도 응답에서 누락되지 않게 하기 위함이다.
(핵심 수집 로직은 dict 로 동작하고, 이 모델은 경계에서 문서화/검증만 담당한다.)
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class Summary(_Loose):
    model_groups: int = 0
    deployments_registered: int = 0
    deployments_total: int = 0
    deployments_healthy: int = 0
    deployments_unhealthy: int = 0
    deployments_blocked: int = 0
    blocked_known: bool = False
    backends_up: int = 0
    backends_total: int = 0
    backend_models: int = 0
    backend_pods_ready: int = 0
    backend_pods_desired: int = 0
    backend_pods_known: bool = False
    gpu_total: int = 0
    gpu_products: Dict[str, int] = {}
    gpu_known: bool = False


class Deployment(_Loose):
    model_name: Optional[str] = None
    underlying: Optional[str] = None
    api_base: Optional[str] = None
    id: Optional[str] = None
    type: Optional[str] = None            # (레거시) 혼합 분류 — 호환용 유지
    network_type: Optional[str] = None    # kserve | service | external | '-'
    network_type_error: Optional[str] = None    # '-' 일 때 ISVC 조회 실패 원인
    backend_type: Optional[str] = None    # vllm | sglang | '-'
    backend_type_source: Optional[str] = None   # pod(컨테이너 이미지) | name(휴리스틱)
    backend_ref: Optional[str] = None     # per-user 뷰 전용: 익명 백엔드 식별자
    backends_ready: Optional[int] = None
    backends_desired: Optional[int] = None
    backend_source: Optional[str] = None
    mode: Optional[str] = None
    scale_to_zero: Optional[bool] = None
    namespace: Optional[str] = None
    service: Optional[str] = None
    k8s_error: Optional[str] = None
    status: Optional[str] = None       # UP | DOWN | PAUSED | ?
    status_source: Optional[str] = None
    blocked: Optional[bool] = None     # LiteLLM 관리자 일시중지. None=알 수 없음
    health_status: Optional[str] = None  # PAUSED 이전의 원래 health 판정
    # 그 판정의 근거(health/k8s/unknown). status_source 가 "blocked" 로
    # 덮이므로 이게 없으면 실측/추정 구분이 사라진다.
    health_status_source: Optional[str] = None
    gpu_ready: Optional[int] = None
    gpu_products: Dict[str, int] = {}
    gpu_error: Optional[str] = None


class ModelGroup(_Loose):
    model_group: Optional[str] = None
    providers: List[str] = []
    mode: Optional[str] = None


class HealthEndpoint(_Loose):
    model: Optional[str] = None
    api_base: Optional[str] = None
    error: Optional[str] = None


class Health(_Loose):
    healthy_count: Optional[int] = None
    unhealthy_count: Optional[int] = None
    healthy_endpoints: List[HealthEndpoint] = []
    unhealthy_endpoints: List[HealthEndpoint] = []


class LiteLLM(_Loose):
    url: Optional[str] = None
    reachable: bool = False
    groups: List[ModelGroup] = []
    deployments: List[Deployment] = []
    health: Optional[Health] = None
    models: List[str] = []
    errors: List[str] = []


class BackendProbe(_Loose):
    name: Optional[str] = None
    url: Optional[str] = None
    type: Optional[str] = None
    up: bool = False
    models: List[str] = []
    error: Optional[str] = None


class Snapshot(_Loose):
    version: Optional[str] = None
    ts: Optional[str] = None
    litellm: Optional[LiteLLM] = None
    backends: List[BackendProbe] = []
    summary: Summary = Summary()
    backend_count_enabled: Optional[bool] = None
    demo: Optional[bool] = None
    # 수집 상태 플래그
    loading: Optional[bool] = None
    error: Optional[str] = None
    collect_error: Optional[str] = None
    # per-user(키별) 뷰 필드
    user_view: Optional[bool] = None
    admin_view: Optional[bool] = None
    needs_key: Optional[bool] = None
    key_info: Optional[Dict[str, Any]] = None
    accessible_count: Optional[int] = None

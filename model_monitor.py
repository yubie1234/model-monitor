#!/usr/bin/env python3
"""
model_monitor.py — LiteLLM -> KServe -> vLLM/SGLang 백엔드에서 실제로 떠 있는 모델 현황을 조회하는 모니터.

특징
  - 외부 패키지 0개 (Python 3.6+ 표준 라이브러리만 사용 -> air-gapped 노드에서 설치 없이 실행)
  - 데이터 소스
      * LiteLLM gateway:  GET /model_group/info  (등록된 모델 그룹)
                          GET /health             (백엔드 실제 health = "떠 있음"의 근거)
                          GET /v1/models          (OpenAI 호환 모델 목록)
      * (옵션) 백엔드 직접 probe: 각 vLLM/SGLang 엔드포인트의 GET /v1/models, /health
  - 출력: 1회 스냅샷 / --json / --watch(실시간) 지원
  - --demo: 라이브 엔드포인트 없이 샘플 데이터로 출력 미리보기

사용 예
  python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234
  python3 model_monitor.py --config config.yaml --watch
  python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234 --json
  python3 model_monitor.py --demo --watch
"""

import argparse
import copy
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

__version__ = "0.4.0"

# ----------------------------------------------------------------------------
# HTTP (stdlib only)
# ----------------------------------------------------------------------------


def http_get_json(url, api_key=None, timeout=10):
    """GET url -> (ok: bool, data: dict|list|None, error: str|None)."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
        headers["x-api-key"] = api_key  # LiteLLM accepts either
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, json.loads(raw), None
            except ValueError:
                return False, None, "non-JSON response: %s" % raw[:200]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, None, "HTTP %s %s %s" % (e.code, e.reason, body)
    except urllib.error.URLError as e:
        return False, None, "connection error: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, e)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


def load_config(path):
    """JSON 우선, .yaml/.yml 은 PyYAML 있으면 사용. 둘 다 안되면 안내."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit(
                "config '%s' 는 YAML 인데 PyYAML 이 없습니다. "
                "JSON 설정(.json)을 쓰거나 PyYAML 을 설치하세요." % path
            )
        return yaml.safe_load(text) or {}
    return json.loads(text)


def resolve_settings(args):
    """CLI > env > config 파일 순으로 설정 병합."""
    cfg = {}
    if args.config:
        cfg = load_config(args.config) or {}

    litellm = cfg.get("litellm", {}) if isinstance(cfg.get("litellm"), dict) else {}
    bc = cfg.get("backend_count", {}) if isinstance(cfg.get("backend_count"), dict) else {}
    uv = cfg.get("user_view", {}) if isinstance(cfg.get("user_view"), dict) else {}
    mt = cfg.get("metrics", {}) if isinstance(cfg.get("metrics"), dict) else {}

    # backend 개수 수집(k8s API) 사용 여부:
    #   --no-backend-count 면 off, 기본은 auto(= in-cluster SA 토큰 있으면 자동 on)
    bc_enabled = True
    if getattr(args, "no_backend_count", False):
        bc_enabled = False

    settings = {
        "litellm_url": (
            args.litellm_url
            or os.environ.get("LITELLM_BASE_URL")
            or litellm.get("url")
        ),
        "api_key": (
            args.api_key
            or os.environ.get("LITELLM_API_KEY")
            or litellm.get("api_key")
        ),
        "backends": cfg.get("backends", []),  # [{name,url,type}]
        "probe_backends": args.probe_backends or bool(cfg.get("probe_backends")),
        "timeout": args.timeout,
        # /health 는 백엔드를 전부 ping 해서 느림(수십 초) -> 별도 큰 타임아웃
        "health": (not getattr(args, "no_health", False)
                   and litellm.get("health", True)),
        "health_timeout": float(
            args.health_timeout or litellm.get("health_timeout") or 90.0),
        # --- backend 개수(LB 뒤 Pod 수) 수집 설정 ---
        "backend_count": bc_enabled,
        # GPU 개수+장치명(Pod nvidia.com/gpu + 노드 라벨) 수집. 기본 ON,
        # --no-gpu-info 면 off. Pod/Node 읽기 권한이 없으면 조용히 ? 폴백.
        "gpu_info": (bc_enabled
                     and not getattr(args, "no_gpu_info", False)
                     and bc.get("gpu_info", True)),
        "k8s_api_server": (args.k8s_api_server or bc.get("api_server")),
        "k8s_token_file": (args.k8s_token_file or bc.get("token_file")
                           or "/var/run/secrets/kubernetes.io/serviceaccount/token"),
        "k8s_ca_file": (args.k8s_ca_file or bc.get("ca_file")
                        or "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
        "k8s_insecure": bool(args.k8s_insecure or bc.get("insecure")),
        "k8s_timeout": float(args.k8s_timeout or bc.get("timeout") or 5.0),
        "default_namespace": bc.get("default_namespace"),
        "namespace_overrides": bc.get("namespace_overrides", {}) or {},
        "activator_namespace": bc.get("activator_namespace", "knative-serving"),
        # --- per-user(키별) 뷰 ---
        # 기본 OFF: Go/No-Go 게이트(/v1/models 키별 필터 동작) + TLS 종단을 운영자가
        # 확인한 뒤에만 켠다. 켜지 않으면 POST /api/snapshot/user 는 비활성.
        "user_view": bool(getattr(args, "enable_user_view", False)
                          or uv.get("enabled")),
        # 비-admin 뷰에서 내부 api_base/namespace 숨김(기본 True). 내부 도구라
        # 토폴로지를 보여줘도 되면 --user-view-show-internal 로 끈다.
        "user_view_hide_internal": not (
            getattr(args, "user_view_show_internal", False)
            or uv.get("show_internal")),
        # 키별 접근 캐시 TTL(초). 폴링 중복 /v1/models 호출을 줄인다.
        "user_view_cache_ttl": float(uv.get("cache_ttl") or 30.0),
        # --- Prometheus 메트릭(/metrics) ---
        # 기본 ON(--serve 시). 캐시된 스냅샷을 노출만 하므로 /api/snapshot 과 같은
        # 수준의 정보 — 끄려면 --no-metrics. user_view(키 필수) 모드에선 다른 global
        # export 처럼 admin 키 헤더가 있어야 노출된다.
        "metrics": (not getattr(args, "no_metrics", False)) and mt.get("enabled", True),
    }
    return settings


# ----------------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------------


def _classify_backend(model_name, underlying, api_base):
    """model_name 접두사/underlying model 로 백엔드 종류 추정 (표시용)."""
    blob = ("%s %s %s" % (model_name, underlying, api_base)).lower()
    if "sglang" in blob:
        return "sglang"
    if "kserve" in blob:
        return "kserve"
    if "vllm" in blob:
        return "vllm"
    return "-"


def fetch_health(url, api_key, timeout):
    """LiteLLM /health 만 단독 조회 (느려서 비동기로 돌릴 때 사용). dict|None."""
    ok, data, err = http_get_json(url.rstrip("/") + "/health", api_key, timeout)
    return data if (ok and isinstance(data, dict)) else None


def collect_litellm(url, api_key, timeout, health_timeout=None, with_health=True):
    """LiteLLM 게이트웨이에서 모델 그룹 + deployment(api_base) + health 수집.

    핵심: api_base 는 /v1/models 가 아니라 /model/info 에서 나온다.
    /health 는 모든 백엔드를 실제 ping 하므로 느리다(수십 초). with_health=False 면
    건너뛰고, 호출측에서 fetch_health 로 비동기 수집해 주입한다.
    """
    if health_timeout is None:
        health_timeout = max(timeout, 90.0)
    base = url.rstrip("/")
    result = {
        "url": base,
        "reachable": False,
        "groups": [],          # model_group/info data
        "deployments": [],     # /model/info 정규화: model_name -> api_base 등
        "health": None,        # /health raw
        "models": [],          # /v1/models ids (이름만)
        "errors": [],
    }

    ok, data, err = http_get_json(base + "/model_group/info", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        # 이름순 정렬: LiteLLM 응답 순서가 replica 구성에 따라 바뀌어도 표시 고정
        result["groups"] = sorted(
            data.get("data", []) or [],
            key=lambda g: str(g.get("model_group") or "").lower())
    elif err:
        result["errors"].append("model_group/info: %s" % err)

    # /model/info -> 실제 api_base 가 여기서 나온다 (admin 권한 키 권장)
    ok, data, err = http_get_json(base + "/model/info", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        for d in data.get("data", []) or []:
            lp = d.get("litellm_params", {}) or {}
            mi = d.get("model_info", {}) or {}
            name = d.get("model_name", "?")
            underlying = lp.get("model", "")
            api_base = lp.get("api_base")
            result["deployments"].append({
                "model_name": name,
                "underlying": underlying,
                "api_base": api_base,
                "id": mi.get("id"),
                "type": _classify_backend(name, underlying, api_base or ""),
            })
    elif err:
        result["errors"].append("model/info: %s" % err)

    if with_health:
        ok, data, err = http_get_json(base + "/health", api_key, health_timeout)
        if ok and isinstance(data, dict):
            result["reachable"] = True
            result["health"] = data
        elif err:
            result["errors"].append(
                "health: %s (모델 많으면 --health-timeout 늘리기)" % err)

    ok, data, err = http_get_json(base + "/v1/models", api_key, timeout)
    if ok and isinstance(data, dict):
        result["reachable"] = True
        result["models"] = [m.get("id") for m in data.get("data", []) if m.get("id")]
    elif err:
        result["errors"].append("v1/models: %s" % err)

    return result


def collect_user_access(url, user_key, timeout):
    """사용자 본인 키로 접근 가능한 모델 집합 + 키 메타를 수집(per-user 뷰용).

    권한 판정의 **단일 출처는 `/v1/models`** — LiteLLM 이 키의 models/팀 상속/
    `*`·`openai/*` 와일드카드/access group 을 **이미 해석한 결과**다. 우리가 이를
    재유도하지 않는다(`/key/info.models` 로 접근권을 다시 풀면 피하려던 해석이 되살아남).

    `/key/info` 는 메타(spend/budget/limit) 표시용으로만 쓴다 — best-effort 라
    실패해도 모델 목록(접근권)에는 영향 없다.

    fail-closed: `/v1/models` 가 실패(키 무효/만료/네트워크)하면 ok=False, accessible
    은 빈 집합으로 둔다. 호출측은 절대 unfiltered global 로 폴백하면 안 된다.
    """
    base = url.rstrip("/")
    out = {"ok": False, "error": None, "accessible": [], "key_info": None}

    ok, data, err = http_get_json(base + "/v1/models", user_key, timeout)
    if not ok:
        out["error"] = err or "/v1/models 조회 실패"
        return out
    if not isinstance(data, dict):
        out["error"] = "예상치 못한 /v1/models 응답"
        return out
    # /v1/models 의 id == /model/info 의 model_name(public name) 으로 조인한다.
    out["accessible"] = sorted(
        {m.get("id") for m in (data.get("data") or []) if m.get("id")})
    out["ok"] = True

    # 키 메타: 비-admin 키가 자기 키 정보를 못 읽는 버전도 있어 best-effort.
    ok2, ki, _ = http_get_json(base + "/key/info", user_key, timeout)
    if ok2 and isinstance(ki, dict):
        info = ki.get("info") if isinstance(ki.get("info"), dict) else ki
        out["key_info"] = {
            "spend": info.get("spend"),
            "max_budget": info.get("max_budget"),
            "tpm_limit": info.get("tpm_limit"),
            "rpm_limit": info.get("rpm_limit"),
            "expires": info.get("expires"),
            "key_alias": info.get("key_alias"),
        }
    return out


class AccessCache:
    """키별 접근 결과(`collect_user_access`)를 짧게 캐시 — 폴링 중복 호출 제거.

    캐시 키는 **원문 키가 아니라 sha256 해시**(키는 절대 저장 안 함).
    **성공(ok=True) 응답만** 캐시한다 — 무효/만료 키는 매 요청 재검증되어 fail-closed
    즉시성이 유지된다. 대신 취소/만료된 *유효했던* 키는 최대 TTL 동안 stale 할 수 있다.
    """

    def __init__(self, ttl=30.0, maxsize=512):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d = {}            # sha256(key) -> (expiry, access)
        self._lock = threading.Lock()

    def get_or_collect(self, key, collect, now):
        """캐시에 살아있으면 그대로, 아니면 collect() 호출 후(성공 시) 캐시."""
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._lock:
            ent = self._d.get(h)
            if ent and ent[0] > now:
                return ent[1]
        access = collect()
        if isinstance(access, dict) and access.get("ok"):
            with self._lock:
                self._prune(now)
                self._d[h] = (now + self.ttl, access)
        return access

    def _prune(self, now):
        if len(self._d) < self.maxsize:
            return
        for k in [k for k, (e, _) in self._d.items() if e <= now]:
            self._d.pop(k, None)
        while len(self._d) >= self.maxsize:
            self._d.pop(next(iter(self._d)), None)


def collect_backend(backend, timeout):
    """개별 vLLM/SGLang 백엔드 직접 probe: /v1/models + /health."""
    name = backend.get("name") or backend.get("url")
    base = (backend.get("url") or "").rstrip("/")
    api_key = backend.get("api_key")
    btype = backend.get("type", "vllm")
    out = {"name": name, "url": base, "type": btype, "up": False,
           "models": [], "error": None}

    ok, data, err = http_get_json(base + "/v1/models", api_key, timeout)
    if ok and isinstance(data, dict):
        out["up"] = True
        out["models"] = [m.get("id") for m in data.get("data", []) if m.get("id")]
    else:
        out["error"] = err

    if not out["up"]:  # /v1/models 실패 시 /health 라도 확인
        ok, _, _ = http_get_json(base + "/health", api_key, timeout)
        out["up"] = ok

    return out


def _strip_openai_suffix(api_base):
    """api_base 에 붙은 OpenAI 경로(/v1, /openai/v1)를 떼어 베이스만 남긴다."""
    base = api_base.rstrip("/")
    for suffix in ("/openai/v1", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def discover_backends(litellm_result):
    """LiteLLM 에서 받은 api_base 로 probe 대상 백엔드를 자동 발견.

    -> backends 를 수동으로 적을 필요가 없다. 주소의 원천은 LiteLLM 설정의
       litellm_params.api_base 이며, /model/info(우선) 또는 /health 가 그대로 돌려준다.
    """
    discovered = {}
    # 1순위: /model/info 의 deployments (api_base 평문 + 종류 분류 포함)
    for d in litellm_result.get("deployments") or []:
        api_base = d.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        discovered.setdefault(base, {
            "name": d.get("model_name") or base,
            "url": base,
            "type": d.get("type", "-"),
        })
    # 2순위 보강: /health 에만 있는 주소
    health = litellm_result.get("health") or {}
    for ep in (health.get("healthy_endpoints") or []) + (
            health.get("unhealthy_endpoints") or []):
        api_base = ep.get("api_base")
        if not api_base:
            continue
        base = _strip_openai_suffix(api_base)
        if base in discovered:
            continue
        model = ep.get("model", "")
        btype = _classify_backend(model, model, base)
        discovered[base] = {"name": model or base, "url": base, "type": btype}
    return list(discovered.values())


# ----------------------------------------------------------------------------
# Backend 개수: api_base(LB) 뒤의 실제 Pod/replica 수
#   1순위 EndpointSlice(ready 주소 수) -> Serverless 면 Knative PodAutoscaler
#   -> RawDeployment Deployment -> none
#   외부 패키지 0개: urllib + ssl(표준 라이브러리)로 in-cluster k8s API 호출
# ----------------------------------------------------------------------------

import ssl  # noqa: E402  (표준 라이브러리)
import urllib.parse  # noqa: E402


class K8sClient:
    """in-cluster Kubernetes API 를 표준 라이브러리만으로 호출."""

    def __init__(self, api_server, token, ssl_ctx, timeout, default_namespace):
        self.api_server = api_server.rstrip("/") if api_server else None
        self.token = token
        self.ssl_ctx = ssl_ctx
        self.timeout = timeout
        self.default_namespace = default_namespace or "default"

    @property
    def enabled(self):
        return bool(self.api_server)

    @classmethod
    def from_settings(cls, settings):
        """in-cluster ServiceAccount 토큰/CA 가 있으면 활성, 없으면 None."""
        if not settings.get("backend_count"):
            return None

        # API server 주소: 명시 > env > 기본 in-cluster DNS
        api_server = settings.get("k8s_api_server")
        if not api_server:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if host:
                api_server = "https://%s:%s" % (host, port)

        token = None
        token_file = settings.get("k8s_token_file")
        if token_file and os.path.exists(token_file):
            try:
                with open(token_file) as f:
                    token = f.read().strip()
            except OSError:
                token = None

        # 토큰이 없으면 클러스터 밖(개발환경)으로 보고 k8s API 비활성
        if not token:
            api_server = None

        ssl_ctx = None
        if api_server:
            if settings.get("k8s_insecure"):
                ssl_ctx = ssl._create_unverified_context()
            else:
                ca = settings.get("k8s_ca_file")
                try:
                    if ca and os.path.exists(ca):
                        ssl_ctx = ssl.create_default_context(cafile=ca)
                    else:
                        ssl_ctx = ssl.create_default_context()
                except Exception:
                    ssl_ctx = ssl._create_unverified_context()

        # 네임스페이스 폴백 최종값: 설정 > SA namespace 파일 > default
        ns = settings.get("default_namespace")
        if not ns:
            ns_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
            if os.path.exists(ns_file):
                try:
                    with open(ns_file) as f:
                        ns = f.read().strip()
                except OSError:
                    ns = None

        if not api_server:   # 토큰/주소 없으면(클러스터 밖) backend 개수 수집 불가
            return None
        return cls(
            api_server=api_server, token=token, ssl_ctx=ssl_ctx,
            timeout=settings.get("k8s_timeout", 5.0), default_namespace=ns,
        )

    def get(self, path):
        """k8s API GET -> (ok, data, err)."""
        if not self.api_server:
            return False, None, "k8s api server not configured"
        url = self.api_server + path
        headers = {"Accept": "application/json",
                   "Authorization": "Bearer %s" % self.token}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                return True, json.loads(resp.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            return False, None, "HTTP %s %s" % (e.code, e.reason)
        except Exception as e:  # noqa: BLE001
            return False, None, "%s: %s" % (type(e).__name__, e)


def parse_api_base(api_base, default_namespace="default", overrides=None):
    """api_base URL -> {service, namespace, port, kind}.

    예: http://qwen36-35b-predictor.kserve.svc:8080/v1
        -> service=qwen36-35b-predictor, namespace=kserve, port=8080, kind=k8s-svc
    클러스터 Service 가 아니면 kind='external'.
    """
    overrides = overrides or {}
    try:
        parts = urllib.parse.urlsplit(api_base)
    except Exception:
        return {"service": None, "namespace": None, "port": None, "kind": "external"}
    host = parts.hostname or ""
    port = parts.port
    if not host:
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    if host in overrides:  # host 통째 override 우선
        return {"service": host.split(".")[0],
                "namespace": overrides[host], "port": port, "kind": "k8s-svc"}

    host = host.rstrip(".")            # FQDN 절대표기 'svc.cluster.local.' 처리
    tokens = host.split(".")

    # IP 주소거나 공인 도메인(.com/.net 등)이면 클러스터 Service 아님
    if all(t.isdigit() for t in tokens) or ":" in host:
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    service = tokens[0]
    namespace = None
    if "svc" in tokens:
        # [svc].[ns].svc[.cluster.local]
        idx = tokens.index("svc")
        if idx >= 2:
            namespace = tokens[idx - 1]
    elif len(tokens) >= 2 and tokens[-1] not in (
            "com", "net", "org", "io", "ai", "co", "dev", "local"):
        # [svc].[ns] 단축형 (위험 — overrides 권장)
        namespace = tokens[1]
    elif len(tokens) >= 3:
        # 외부 도메인으로 판단
        return {"service": None, "namespace": None, "port": port, "kind": "external"}

    # service 이름 override (ns 추론 불가한 단축형 보강)
    if service in overrides:
        namespace = overrides[service]
    if not namespace:
        namespace = default_namespace
    return {"service": service, "namespace": namespace, "port": port,
            "kind": "k8s-svc"}


def _is_activator(ep, activator_ns):
    ref = ep.get("targetRef") or {}
    if ref.get("namespace") == activator_ns:
        return True
    name = (ref.get("name") or "").lower()
    return name.startswith("activator")


def count_via_endpointslice(client, ns, svc, activator_ns):
    """EndpointSlice ready 주소 수 합산. activator 만 있으면 activator_only=True."""
    path = ("/apis/discovery.k8s.io/v1/namespaces/%s/endpointslices"
            "?labelSelector=kubernetes.io/service-name=%s" % (ns, svc))
    ok, data, err = client.get(path)
    if not ok:
        return None, err
    ready = 0
    saw_activator = False
    saw_real = False
    for item in data.get("items", []) or []:
        for ep in item.get("endpoints", []) or []:
            cond = ep.get("conditions") or {}
            if cond.get("ready") is False:
                continue
            addrs = ep.get("addresses") or []
            if _is_activator(ep, activator_ns):
                saw_activator = True
                continue
            saw_real = True
            ready += len(addrs)
    return {"ready": ready, "activator_only": saw_activator and not saw_real}, None


def count_via_endpoints(client, ns, svc):
    """core/v1 Endpoints 폴백(구버전 k8s): subsets addresses 고유 IP 수."""
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/endpoints/%s" % (ns, svc))
    if not ok:
        return None, err
    ips = set()
    for sub in data.get("subsets", []) or []:
        for a in sub.get("addresses", []) or []:
            if a.get("ip"):
                ips.add(a["ip"])
    return {"ready": len(ips)}, None


def detect_mode_and_revision(client, ns, svc):
    """service 이름에서 ISVC 추정 -> deploymentMode + revision + found 여부."""
    isvc = svc
    for suffix in ("-predictor", "-transformer", "-explainer"):
        if isvc.endswith(suffix):
            isvc = isvc[: -len(suffix)]
            break
    ok, data, err = client.get(
        "/apis/serving.kserve.io/v1beta1/namespaces/%s/inferenceservices/%s"
        % (ns, isvc))
    if not ok:
        return {"mode": "Unknown", "revision": None, "isvc": isvc,
                "found": False}, err
    status = data.get("status") or {}
    mode = status.get("deploymentMode") or (
        data.get("metadata", {}).get("annotations", {})
        .get("serving.kserve.io/deploymentMode")) or "Unknown"
    pred = (status.get("components") or {}).get("predictor") or {}
    # revision 필드는 KServe/Knative 버전마다 달라 여러 후보를 순차 시도
    rev = (pred.get("latestReadyRevision")
           or pred.get("latestCreatedRevision")
           or pred.get("latestRolledoutRevision"))
    if not rev:
        for t in (pred.get("traffic") or []):
            if t.get("revisionName"):
                rev = t["revisionName"]
                break
    return {"mode": mode, "revision": rev, "isvc": isvc, "found": True}, None


def _is_serverless(mode, revision):
    """Knative/Serverless 여부. revision 이 있으면 Knative-backed 으로 본다."""
    if revision:
        return True
    m = (mode or "").lower()
    return ("serverless" in m) or ("knative" in m)


def count_via_deployment_label(client, ns, isvc):
    """KServe Deployment 들을 라벨로 합산 — raw/serverless 공통, revision 이름 불필요.

    KServe 가 predictor Deployment 에 serving.kserve.io/inferenceservice=<isvc>
    라벨을 붙이므로, Knative 네이밍/activator 를 몰라도 ready Pod 수를 합산할 수 있다.
    """
    sel = "serving.kserve.io/inferenceservice=%s" % isvc
    ok, data, err = client.get(
        "/apis/apps/v1/namespaces/%s/deployments?labelSelector=%s" % (ns, sel))
    if not ok:
        return None, "deployments(label): %s" % err
    items = data.get("items") or []
    if not items:
        return None, "deployments(label): no match (%s)" % sel
    ready = sum(int((d.get("status") or {}).get("readyReplicas") or 0)
                for d in items)
    desired = sum(int((d.get("spec") or {}).get("replicas") or 0)
                  for d in items)
    return {"ready": ready, "desired": desired, "source": "deployment"}, None


def count_via_knative(client, ns, revision):
    """Knative PodAutoscaler actualScale(실시간) 우선, 실패 시 Revision."""
    if not revision:
        return None, "no revision"
    ok, data, err = client.get(
        "/apis/autoscaling.internal.knative.dev/v1alpha1/namespaces/%s/podautoscalers/%s"
        % (ns, revision))
    if ok:
        st = data.get("status") or {}
        actual = st.get("actualScale")
        desired = st.get("desiredScale")
        if actual is not None:
            return {"ready": int(actual), "desired": _int_or_none(desired),
                    "source": "knative-pa",
                    "scale_to_zero": int(actual) == 0}, None
    ok2, data2, err2 = client.get(
        "/apis/serving.knative.dev/v1/namespaces/%s/revisions/%s" % (ns, revision))
    if ok2:
        st = data2.get("status") or {}
        actual = st.get("actualReplicas")
        if actual is not None:
            return {"ready": int(actual),
                    "desired": _int_or_none(st.get("desiredReplicas")),
                    "source": "knative-revision",
                    "scale_to_zero": int(actual) == 0}, None
        return None, "pa/revision 에 actualScale 필드 없음"
    return None, (err or err2 or "knative 조회 실패")


def count_via_deployment(client, ns, svc):
    """RawDeployment 보정: Deployment status.readyReplicas / spec.replicas."""
    dep = svc  # KServe RawDeployment 는 보통 service 이름과 동일({isvc}-predictor)
    ok, data, err = client.get(
        "/apis/apps/v1/namespaces/%s/deployments/%s" % (ns, dep))
    if not ok:
        return None, err
    spec = data.get("spec") or {}
    st = data.get("status") or {}
    return {"ready": int(st.get("readyReplicas") or 0),
            "desired": _int_or_none(spec.get("replicas")),
            "source": "deployment"}, None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# GPU: backend Pod 의 nvidia.com/gpu 개수 + 장치 모델명(H100/B200 ...)
#   개수 -> Pod spec resources.limits["nvidia.com/gpu"]
#   장치 -> Pod 가 뜬 노드의 라벨 nvidia.com/gpu.product (GPU Operator/GFD)
#   멀티노드 GPU 환경 없음 전제: Pod 1개 = 노드 1개.
# ----------------------------------------------------------------------------

GPU_RESOURCE = "nvidia.com/gpu"
GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"


def _gpu_qty(v):
    """nvidia.com/gpu 수량 문자열("1","8")을 int 로. 실패하면 0."""
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _pod_gpu(pod):
    """Pod 한 개가 점유하는 GPU 수 = 컨테이너 limits(없으면 requests) 의 nvidia.com/gpu 합."""
    total = 0
    for ctr in ((pod.get("spec") or {}).get("containers") or []):
        res = ctr.get("resources") or {}
        q = (res.get("limits") or {}).get(GPU_RESOURCE)
        if q is None:
            q = (res.get("requests") or {}).get(GPU_RESOURCE)
        total += _gpu_qty(q)
    return total


def _pod_ready(pod):
    """Running + Ready condition True 인 Pod 만 '서빙 중'으로 본다(backends_ready 와 동일 기준)."""
    st = pod.get("status") or {}
    if st.get("phase") != "Running":
        return False
    for cnd in st.get("conditions") or []:
        if cnd.get("type") == "Ready":
            return cnd.get("status") == "True"
    return False


def _short_gpu_product(prod):
    """NVIDIA-H100-80GB-HBM3 -> H100, NVIDIA-B200 -> B200, NVIDIA-A100-SXM4-80GB -> A100."""
    if not prod:
        return None
    s = prod
    if s.upper().startswith("NVIDIA-"):
        s = s[len("NVIDIA-"):]
    return s.split("-")[0] or prod


def _node_gpu_product(client, node_name, cache):
    """노드 라벨 nvidia.com/gpu.product (캐시). 실패/없음이면 None."""
    if not node_name:
        return None
    if node_name in cache:
        return cache[node_name]
    prod = None
    ok, data, _ = client.get("/api/v1/nodes/%s" % node_name)
    if ok:
        labels = (data.get("metadata") or {}).get("labels") or {}
        prod = labels.get(GPU_PRODUCT_LABEL)
    cache[node_name] = prod
    return prod


def collect_gpu_for_service(client, ns, svc, isvc, found, node_cache):
    """(ns,svc) 뒤 ready Pod 들의 GPU 수 합 + 장치별 집계.

    -> {"gpu_ready": int|None, "gpu_products": {short: count}, "gpu_error": str|None}
       gpu_ready=None 은 조회 실패(?), 0 은 GPU 없음/scale-to-zero.
    Pod 선택: KServe(ISVC found)면 serving.kserve.io/inferenceservice 라벨,
    아니면 Service 의 spec.selector 로 labelSelector 를 만든다.
    """
    out = {"gpu_ready": None, "gpu_products": {}, "gpu_error": None}
    if found:
        sel = "serving.kserve.io/inferenceservice=%s" % isvc
    else:
        ok, sdata, serr = client.get(
            "/api/v1/namespaces/%s/services/%s" % (ns, svc))
        if not ok:
            out["gpu_error"] = "service: %s" % serr
            return out
        seldict = ((sdata.get("spec") or {}).get("selector")) or {}
        if not seldict:
            out["gpu_error"] = "service 에 selector 없음"
            return out
        sel = ",".join("%s=%s" % (k, v) for k, v in sorted(seldict.items()))
    ok, data, err = client.get(
        "/api/v1/namespaces/%s/pods?labelSelector=%s"
        % (ns, urllib.parse.quote(sel, safe="=,")))
    if not ok:
        out["gpu_error"] = "pods: %s" % err
        return out
    total = 0
    products = {}
    any_ready = False
    for pod in data.get("items") or []:
        if not _pod_ready(pod):
            continue
        any_ready = True
        g = _pod_gpu(pod)
        if g <= 0:
            continue
        total += g
        prod = _short_gpu_product(_node_gpu_product(
            client, (pod.get("spec") or {}).get("nodeName"), node_cache)) or "GPU"
        products[prod] = products.get(prod, 0) + g
    out["gpu_ready"] = total if any_ready else 0
    out["gpu_products"] = products
    return out


def resolve_backend_count(deployment, client, settings, cache=None):
    """우선순위 체인으로 한 deployment 의 LB 뒤 backend 개수 산출 -> 필드 dict.

    cache={(ns,svc): out} 를 주면 같은 Service 를 가리키는 여러 model_name 이
    한 스냅샷 빌드 안에서 k8s API 를 중복 조회하지 않고 결과를 재사용한다.
    """
    out = {"backends_ready": None, "backends_desired": None,
           "backend_source": "none", "mode": "Unknown",
           "scale_to_zero": False, "namespace": None, "service": None,
           "k8s_error": None,
           "gpu_ready": None, "gpu_products": {}, "gpu_error": None}
    api_base = deployment.get("api_base")
    if not api_base:
        return out

    parsed = parse_api_base(api_base, client.default_namespace,
                            settings.get("namespace_overrides"))
    out["namespace"] = parsed["namespace"]
    out["service"] = parsed["service"]
    if parsed["kind"] != "k8s-svc" or not parsed["service"]:
        out["backend_source"] = "external"
        return out

    if not client.enabled:
        return out

    ns, svc = parsed["namespace"], parsed["service"]
    if cache is not None and (ns, svc) in cache:
        return dict(cache[(ns, svc)])   # 같은 Service 는 재조회 생략
    activator_ns = settings.get("activator_namespace", "knative-serving")
    errors = []

    info, _ = detect_mode_and_revision(client, ns, svc)
    out["mode"] = info["mode"]
    isvc, revision = info["isvc"], info["revision"]
    serverless = _is_serverless(info["mode"], revision)

    def setres(r):
        out["backends_ready"] = r["ready"]
        if r.get("desired") is not None:
            out["backends_desired"] = r["desired"]
        out["backend_source"] = r["source"]
        if r.get("scale_to_zero"):
            out["scale_to_zero"] = True

    if info["found"]:
        # --- KServe ISVC: Deployment 라벨 합산이 raw/serverless 공통으로 가장 견고 ---
        dep, err = count_via_deployment_label(client, ns, isvc)
        if dep is not None:
            setres(dep)
        else:
            errors.append(err)
            # serverless 면 Knative PodAutoscaler/Revision 으로 보강
            if revision:
                kn, kerr = count_via_knative(client, ns, revision)
                if kn is not None:
                    setres(kn)
                else:
                    errors.append("knative: %s" % kerr)
            else:
                errors.append("no revision (ISVC status 에 revision 없음)")
        # serverless 이고 0 이면 scale-to-zero 로 표기(장애 아님)
        if serverless and out["backends_ready"] == 0:
            out["scale_to_zero"] = True
    else:
        # --- 일반 Service(비 KServe): EndpointSlice 의 ready 주소 수 ---
        es, err = count_via_endpointslice(client, ns, svc, activator_ns)
        if es is not None and not es.get("activator_only"):
            out["backends_ready"] = es["ready"]
            out["backend_source"] = "endpointslice"
        elif es is not None and es.get("activator_only"):
            errors.append("endpointslice: activator only (serverless?)")
        elif es is None and err and "404" in err:
            eps, eerr = count_via_endpoints(client, ns, svc)
            if eps is not None:
                out["backends_ready"] = eps["ready"]
                out["backend_source"] = "endpoints"
            else:
                errors.append("endpoints: %s" % eerr)
        else:
            errors.append("endpointslice: %s" % err)
        # desired 보강(같은 이름 Deployment), ready 미상이면 그걸로 대체
        dep, derr = count_via_deployment(client, ns, svc)
        if dep is not None:
            if out["backends_desired"] is None:
                out["backends_desired"] = dep.get("desired")
            if out["backends_ready"] is None:
                out["backends_ready"] = dep["ready"]
                out["backend_source"] = "deployment"

    if out["backends_ready"] is None and errors:
        out["k8s_error"] = "; ".join(errors)

    # GPU 개수 + 장치명 (기본 ON; --no-gpu-info 면 settings["gpu_info"]=False).
    # 한 건 실패가 전체를 막지 않게 try/except -> gpu_ready=None(=?) 폴백.
    if settings.get("gpu_info"):
        try:
            node_cache = getattr(client, "_node_cache", None)
            if node_cache is None:
                node_cache = {}
                setattr(client, "_node_cache", node_cache)
            g = collect_gpu_for_service(
                client, ns, svc, isvc, info["found"], node_cache)
            out["gpu_ready"] = g["gpu_ready"]
            out["gpu_products"] = g["gpu_products"]
            out["gpu_error"] = g["gpu_error"]
        except Exception as e:  # noqa: BLE001
            out["gpu_error"] = "%s: %s" % (type(e).__name__, e)

    if cache is not None:
        cache[(ns, svc)] = dict(out)
    return out


def build_snapshot(settings, with_health=True):
    """전체 수집 -> 스냅샷 dict.

    with_health=False 면 느린 /health 를 건너뛴다(웹은 health 를 별도 스레드로 주입).
    """
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "litellm": None,
        "backends": [],
        "summary": {},
    }

    if settings["litellm_url"]:
        snap["litellm"] = collect_litellm(
            settings["litellm_url"], settings["api_key"], settings["timeout"],
            settings.get("health_timeout"), with_health=with_health
        )

    if settings["probe_backends"]:
        targets = settings["backends"]
        # 수동 목록이 없으면 LiteLLM /model/info + /health 에서 자동 발견.
        if not targets and snap["litellm"]:
            targets = discover_backends(snap["litellm"])
        for b in targets:
            snap["backends"].append(collect_backend(b, settings["timeout"]))

    # 각 deployment 의 LB(api_base) 뒤 backend Pod 개수 채우기
    client = K8sClient.from_settings(settings)
    snap["backend_count_enabled"] = bool(client)
    if client and snap["litellm"]:
        bc_cache = {}  # (ns,svc) -> 결과: 같은 Service 중복 k8s 조회 방지
        for d in snap["litellm"].get("deployments") or []:
            try:
                d.update(resolve_backend_count(d, client, settings, bc_cache))
            except Exception as e:  # noqa: BLE001  (한 건 실패가 전체를 막지 않게)
                d["k8s_error"] = "%s: %s" % (type(e).__name__, e)

    # /health 상태 + backend readiness 를 합쳐 deployment 에 status 부여
    # (TUI/웹/JSON 모두 동일한 status 를 쓰도록 여기서 한 번에 적용)
    if snap["litellm"]:
        snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])

    snap["summary"] = summarize(snap)
    return snap


def merge_deployments_with_health(ll):
    """/model/info(api_base) 와 /health(상태)를 api_base 기준으로 합친 뷰."""
    health = ll.get("health") or {}
    healthy = {_strip_openai_suffix(ep["api_base"])
               for ep in (health.get("healthy_endpoints") or [])
               if ep.get("api_base")}
    unhealthy = {_strip_openai_suffix(ep["api_base"])
                 for ep in (health.get("unhealthy_endpoints") or [])
                 if ep.get("api_base")}
    merged = []
    for d in ll.get("deployments") or []:
        base = _strip_openai_suffix(d["api_base"]) if d.get("api_base") else None
        if base in healthy:
            status, src = "UP", "health"
        elif base in unhealthy:
            status, src = "DOWN", "health"
        else:
            # /health 미조회(타임아웃/권한)거나 매칭 실패 -> backend readiness 로 추정
            r = d.get("backends_ready")
            if r is not None and d.get("backend_source") != "external":
                if r > 0:
                    status, src = "UP", "k8s"
                elif d.get("scale_to_zero"):
                    status, src = "?", "k8s"      # scale-to-zero = 정상 idle
                else:
                    status, src = "DOWN", "k8s"
            else:
                status, src = "?", "unknown"
        merged.append({**d, "status": status, "status_source": src})
    # LiteLLM 은 replica 구성에 따라 model/info 순서가 매번 달라질 수 있어
    # model_name 기준으로 정렬해 표시 순서를 안정화한다(TUI/웹/JSON 공통).
    merged.sort(key=lambda x: str(x.get("model_name") or "").lower())
    return merged


def summarize(snap):
    """핵심 지표 계산: 모델 그룹 수, 등록 deployment, 떠 있는 모델(healthy) 수."""
    s = {
        "model_groups": 0,
        "deployments_registered": 0,
        "deployments_total": 0,
        "deployments_healthy": 0,
        "deployments_unhealthy": 0,
        "backends_up": 0,
        "backends_total": 0,
        "backend_models": 0,
        "backend_pods_ready": 0,     # 모든 LB 뒤 ready Pod 합계
        "backend_pods_desired": 0,   # 목표 replica 합계
        "backend_pods_known": False,
        "gpu_total": 0,              # 모든 backend 의 ready GPU 합 (Service dedup)
        "gpu_products": {},          # {장치명: 개수}
        "gpu_known": False,
    }
    ll = snap.get("litellm")
    if ll:
        s["model_groups"] = len(ll.get("groups") or [])
        deps = ll.get("deployments") or []
        s["deployments_registered"] = len(deps)
        health = ll.get("health") or {}

        # 카드 수치를 표(merge_deployments_with_health 의 per-row status)와 항상
        # 일치시킨다. deployment 가 있으면 merged status 로 집계(=/health 가
        # 타임아웃해도 k8s readiness 보정이 반영됨), 없으면 /health 카운트로 폴백.
        if deps and any("status" in d for d in deps):
            s["deployments_healthy"] = sum(1 for d in deps if d.get("status") == "UP")
            s["deployments_unhealthy"] = sum(1 for d in deps if d.get("status") == "DOWN")
            s["deployments_total"] = len(deps)
        else:
            hc = health.get("healthy_count")
            uc = health.get("unhealthy_count")
            if hc is None:
                hc = len(health.get("healthy_endpoints") or [])
            if uc is None:
                uc = len(health.get("unhealthy_endpoints") or [])
            s["deployments_healthy"] = hc
            s["deployments_unhealthy"] = uc
            s["deployments_total"] = hc + uc

        # LB 뒤 backend Pod 집계 (값이 있는 deployment 만).
        # 여러 model_name 이 같은 백엔드 Service 를 공유할 수 있으므로
        # (namespace, service) 유일 기준으로 한 번만 더한다 — 안 그러면
        # 공유 Service 의 물리 Pod 가 model_name 수만큼 이중 집계된다.
        # service 식별이 안 되면(external 등) api_base 로 폴백해 그래도 dedup.
        seen_svc = set()
        seen_gpu = set()
        for d in ll.get("deployments") or []:
            key = (d.get("namespace"), d.get("service"))
            if key == (None, None):
                key = ("", d.get("api_base"))
            if d.get("backends_ready") is not None and key not in seen_svc:
                seen_svc.add(key)
                s["backend_pods_ready"] += d["backends_ready"]
                s["backend_pods_known"] = True
                if d.get("backends_desired") is not None:
                    s["backend_pods_desired"] += d["backends_desired"]
            # GPU 도 (ns,svc) 기준 dedup — 공유 백엔드의 물리 GPU 이중 집계 방지.
            if d.get("gpu_ready") is not None and key not in seen_gpu:
                seen_gpu.add(key)
                s["gpu_total"] += d["gpu_ready"]
                s["gpu_known"] = True
                for prod, n in (d.get("gpu_products") or {}).items():
                    s["gpu_products"][prod] = s["gpu_products"].get(prod, 0) + n

    backends = snap.get("backends") or []
    s["backends_total"] = len(backends)
    s["backends_up"] = sum(1 for b in backends if b.get("up"))
    s["backend_models"] = sum(len(b.get("models") or []) for b in backends)
    return s


# ----------------------------------------------------------------------------
# Prometheus 메트릭 (text exposition format 0.0.4) — stdlib 만 사용
# ----------------------------------------------------------------------------

# 상태 -> 게이지 값. UP=1, DOWN=0, 그 외(?/unknown/scale-to-zero idle)=-1.
_STATUS_GAUGE = {"UP": 1, "DOWN": 0}


def _prom_label(v):
    """Prometheus 라벨 값 이스케이프(역슬래시/따옴표/개행)."""
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prom_num(v):
    """게이지 값 직렬화. bool -> 0/1, 정수형 float -> 정수, 그 외 그대로."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _dedup_samples(samples, reduce_fn):
    """같은 라벨 셋의 중복 series 를 reduce_fn 으로 합쳐 valid exposition 을 보장한다.

    LiteLLM 은 한 model_name 에 여러 deployment 를 둘 수 있고(=로드밸런싱), 그러면
    동일 라벨 series 가 중복돼 Prometheus 스크레이프가 깨진다. 값이 None 인 샘플은 버린다.
    """
    acc, order = {}, []
    for labels, value in samples:
        if value is None:
            continue
        key = tuple(sorted(labels.items()))
        if key not in acc:
            acc[key] = [labels, value]
            order.append(key)
        else:
            acc[key][1] = reduce_fn(acc[key][1], value)
    return [(acc[k][0], acc[k][1]) for k in order]


def _status_reduce(a, b):
    """같은 라벨의 상태 충돌 시: DOWN(0) 우선, 다음 UP(1), 그다음 미상(-1)."""
    if 0 in (a, b):
        return 0
    if 1 in (a, b):
        return 1
    return -1


def render_prometheus_metrics(snap):
    """캐시된 스냅샷을 Prometheus text exposition(0.0.4) 으로 변환한다.

    수집은 하지 않는다 — 이미 만들어진 snap 을 문자열로 포맷만 한다(요청 경로 비차단).
    상태 인코딩: UP=1, DOWN=0, ?(미상/scale-to-zero idle)=-1.
    카디널리티: 라벨은 model/namespace/service/backend_source/status_source 로 한정하고
    api_base(내부 URL)는 노출하지 않는다(per-user 뷰에서 숨기는 내부 정보).
    """
    lines = []

    def emit(name, help_text, mtype, samples):
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, mtype))
        for labels, value in samples:
            if value is None:
                continue
            if labels:
                lbl = ",".join('%s="%s"' % (k, _prom_label(val))
                               for k, val in labels.items())
                lines.append("%s{%s} %s" % (name, lbl, _prom_num(value)))
            else:
                lines.append("%s %s" % (name, _prom_num(value)))

    s = snap.get("summary") or {}
    ll = snap.get("litellm") or {}
    deps = ll.get("deployments") or []

    # --- 모니터 자체 메타(스크레이프 신뢰도) ---
    emit("model_monitor_up",
         "모니터가 스냅샷을 갖고 응답 중이면 1, 첫 수집 전(loading)이면 0.", "gauge",
         [({}, 0 if snap.get("loading") else 1)])
    emit("model_monitor_build_info",
         "버전 정보. 값은 항상 1, version 라벨로 식별.", "gauge",
         [({"version": snap.get("version") or __version__}, 1)])
    emit("model_monitor_backend_count_enabled",
         "k8s 백엔드 Pod 수 수집이 켜져 있으면 1.", "gauge",
         [({}, 1 if snap.get("backend_count_enabled") else 0)])
    emit("model_monitor_collect_errors",
         "k8s 조회 에러가 기록된 deployment 수(>0 이면 일부 Pod 수가 부정확).", "gauge",
         [({}, sum(1 for d in deps if d.get("k8s_error")))])

    # --- 요약(summary) 게이지 ---
    emit("model_monitor_deployments_total",
         "집계된 deployment(모델) 총 수.", "gauge",
         [({}, s.get("deployments_total", 0))])
    emit("model_monitor_deployments_healthy",
         "상태 UP 인 deployment 수.", "gauge",
         [({}, s.get("deployments_healthy", 0))])
    emit("model_monitor_deployments_unhealthy",
         "상태 DOWN 인 deployment 수.", "gauge",
         [({}, s.get("deployments_unhealthy", 0))])
    emit("model_monitor_model_groups",
         "LiteLLM 모델 그룹 수.", "gauge",
         [({}, s.get("model_groups", 0))])
    emit("model_monitor_backend_pods_ready_total",
         "모든 LB 뒤 ready Pod 합계(공유 Service 는 1회만 집계).", "gauge",
         [({}, s.get("backend_pods_ready", 0))])
    emit("model_monitor_backend_pods_desired_total",
         "모든 LB 뒤 목표 replica 합계(공유 Service 는 1회만 집계).", "gauge",
         [({}, s.get("backend_pods_desired", 0))])
    emit("model_monitor_backend_pods_known",
         "backend Pod 수를 하나라도 알아냈으면 1.", "gauge",
         [({}, 1 if s.get("backend_pods_known") else 0)])

    # --- deployment(모델) 단위 ---
    def base_labels(d):
        lab = {"model": d.get("model_name") or ""}
        if d.get("namespace"):
            lab["namespace"] = d["namespace"]
        if d.get("service"):
            lab["service"] = d["service"]
        return lab

    up_s, ready_s, desired_s, s2z_s = [], [], [], []
    for d in deps:
        lab = base_labels(d)
        up_lab = dict(lab)
        if d.get("status_source"):
            up_lab["status_source"] = d["status_source"]
        up_s.append((up_lab, _STATUS_GAUGE.get(d.get("status"), -1)))
        pod_lab = dict(lab)
        if d.get("backend_source"):
            pod_lab["backend_source"] = d["backend_source"]
        ready_s.append((pod_lab, d.get("backends_ready")))
        desired_s.append((pod_lab, d.get("backends_desired")))
        s2z_s.append(({"model": d.get("model_name") or ""},
                      1 if d.get("scale_to_zero") else 0))

    emit("model_monitor_model_up",
         "모델 상태: UP=1, DOWN=0, 미상/idle=-1.", "gauge",
         _dedup_samples(up_s, _status_reduce))
    emit("model_monitor_model_backend_pods_ready",
         "이 모델 LB 뒤 ready Pod 수. 여러 모델이 같은 Service 를 공유할 수 있어 "
         "단순 합산은 물리 Pod 를 중복 집계한다 — 총합은 *_total 사용.", "gauge",
         _dedup_samples(ready_s, max))
    emit("model_monitor_model_backend_pods_desired",
         "이 모델 LB 뒤 목표 replica 수.", "gauge",
         _dedup_samples(desired_s, max))
    emit("model_monitor_model_scale_to_zero",
         "scale-to-zero 로 0 Pod 가 정상 idle 이면 1(장애 0 Pod 와 구분).", "gauge",
         _dedup_samples(s2z_s, max))

    return "\n".join(lines) + "\n"


def _redact_deployment_for_user(d):
    """per-user 뷰에서 내부 토폴로지(api_base/underlying/namespace/내부 URL)를 떼고
    상태·종류·backend Pod 수만 남긴다(비-admin 에 클러스터 구조 비노출)."""
    return {
        "model_name": d.get("model_name"),
        "type": d.get("type", "-"),
        "status": d.get("status", "?"),
        "status_source": d.get("status_source"),
        "backends_ready": d.get("backends_ready"),
        "backends_desired": d.get("backends_desired"),
        "backend_source": d.get("backend_source"),
        "scale_to_zero": d.get("scale_to_zero"),
        "mode": d.get("mode"),
    }


def filter_snapshot_for_user(global_snap, access, hide_internal=True):
    """global 스냅샷을 사용자가 접근 가능한 모델로 필터한 per-user 뷰를 만든다.

    핵심: 상태·Pod 수는 **deployment 단위라 키와 무관** → global 값을 그대로 join 하고,
    "이 키가 접근 가능한 model_name 집합" 으로 걸러내기만 한다(얇은 레이어).

    ⚠️ **공유 캐시 오염 주의** — 서버는 단일 `state["snap"]` 을 공유한다(얕은 복사).
    반드시 **deepcopy 한 사본 위에서** 필터할 것. global 의 deployments/groups 를
    제자리(in-place) 로 필터하면 모든 사용자의 global 뷰가 깨진다.
    """
    accessible = set(access.get("accessible") or [])
    snap = copy.deepcopy(global_snap)
    snap["user_view"] = True
    snap.pop("loading", None)
    if hide_internal:
        # 백그라운드 수집 에러 문자열에 내부 주소가 섞일 수 있어 비-admin 뷰에선 숨긴다.
        snap.pop("collect_error", None)
    ll = snap.get("litellm")
    if ll:
        deps = [d for d in (ll.get("deployments") or [])
                if d.get("model_name") in accessible]
        ll["deployments"] = ([_redact_deployment_for_user(d) for d in deps]
                             if hide_internal else deps)
        ll["groups"] = [g for g in (ll.get("groups") or [])
                        if g.get("model_group") in accessible]
        # /v1/models 목록은 사용자 키 기준으로 교체(global admin 목록 노출 금지).
        ll["models"] = sorted(accessible)
        if hide_internal:
            # 수집 에러 문자열에 내부 api_base 가 섞일 수 있어 비-admin 뷰에선 숨긴다.
            ll["errors"] = []
            ll.pop("health", None)
            ll.pop("url", None)
    # 필터된 deployments 기준으로 summary 재계산(카드 수치가 표와 일치).
    snap["summary"] = summarize(snap)
    snap["key_info"] = access.get("key_info")
    snap["accessible_count"] = len(accessible)
    return snap


# ----------------------------------------------------------------------------
# Rendering (ANSI, no deps)
# ----------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, color):
    if not _USE_COLOR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36",
             "bold": "1", "dim": "2", "magenta": "35"}
    return "\033[%sm%s\033[0m" % (codes.get(color, "0"), text)


def _fmt_backends(d):
    """deployment 의 LB 뒤 backend 개수를 컬러 셀로 포맷."""
    ready = d.get("backends_ready")
    desired = d.get("backends_desired")
    src = d.get("backend_source", "none")

    if src == "external":
        return c("external", "dim")
    if d.get("scale_to_zero"):
        return c("0 (scaled-to-zero)", "yellow")
    if ready is None:
        # /health UP 인데 0/미상이면 activator 경유 가능성 힌트
        if d.get("status") == "UP" and src == "none":
            return c("? (via activator?)", "yellow")
        return c("?", "dim")

    if desired is None:
        body = "%d" % ready
    else:
        body = "%d/%d" % (ready, desired)

    if ready == 0:
        color = "red"
    elif desired is not None and ready < desired:
        color = "yellow"
    else:
        color = "green"
    return c(body, color)


# --- 모델 기준 그룹 뷰 헬퍼 (TUI/웹 공통 개념; 웹은 JS 에 동일 로직) ---
def _svc_key(d):
    """deployment 의 백엔드 Service 식별자. (ns,svc) 우선, 없으면 api_base 폴백."""
    ns, svc = d.get("namespace"), d.get("service")
    if ns or svc:
        return (ns or "", svc or "")
    return ("api", d.get("api_base") or "external")


def _shared_map(deps):
    """(ns,svc) -> 그 Service 를 쓰는 model_name 집합. 2개 이상이면 공유."""
    m = {}
    for d in deps:
        m.setdefault(_svc_key(d), set()).add(d.get("model_name"))
    return m


def _composite_status(bes):
    """한 model_name 의 여러 백엔드 상태를 합성: 전부 UP→UP, 전부 DOWN→DOWN,
    섞이면 DEGRADED, 그 외 ?."""
    up = sum(1 for b in bes if b.get("status") == "UP")
    down = sum(1 for b in bes if b.get("status") == "DOWN")
    n = len(bes)
    if n and up == n:
        return "UP"
    if n and down == n:
        return "DOWN"
    if up > 0:
        return "DEGRADED"
    return "?"


def _sum_backends(bes):
    """그 model_name 의 백엔드 ready/desired 합(값 있는 것만)."""
    r = d = None
    for b in bes:
        if b.get("backends_ready") is not None:
            r = (r or 0) + b["backends_ready"]
        if b.get("backends_desired") is not None:
            d = (d or 0) + b["backends_desired"]
    return r, d


def _fmt_agg_backends(ready, desired):
    """모델 그룹 행의 Σ ready/desired 컬러 셀."""
    if ready is None:
        return c("?", "dim")
    body = ("Σ %d/%d" % (ready, desired)) if desired is not None else ("Σ %d" % ready)
    if ready == 0:
        color = "red"
    elif desired is not None and ready < desired:
        color = "yellow"
    else:
        color = "green"
    return c(body, color)


# TUI 장치 색(제한된 ANSI 팔레트 내에서 장치 구분). 상태색(green/red) 회피.
_GPU_TUI_COLOR = {"H100": "magenta", "B200": "cyan", "H200": "yellow",
                  "A100": "cyan", "L40S": "yellow"}


def _gpu_tokens(products):
    """장치별 색 토큰: "H100×4 B200×2" (혼합이면 공백 구분)."""
    toks = []
    for k in sorted(products or {}):
        toks.append(c("%s×%d" % (k, products[k]), _GPU_TUI_COLOR.get(k, "magenta")))
    return " ".join(toks)


def _fmt_gpu(d):
    """deployment 한 행의 GPU 셀 — 장치별 색 칩(텍스트)."""
    gpu = d.get("gpu_ready")
    if gpu is None:
        return c("?" + (" ⚠" if d.get("gpu_error") else ""), "dim")
    if gpu == 0:
        return c("-", "dim")
    return _gpu_tokens(d.get("gpu_products")) or c(str(gpu), "magenta")


def _sum_gpu(bes):
    """모델 그룹의 GPU 합 + 장치별 집계(자식 백엔드는 서로 다른 Service 라 직접 합산)."""
    total = None
    products = {}
    for b in bes:
        g = b.get("gpu_ready")
        if g is None:
            continue
        total = (total or 0) + g
        for p, n in (b.get("gpu_products") or {}).items():
            products[p] = products.get(p, 0) + n
    return total, products


def _fmt_agg_gpu(gpu, products):
    """모델 그룹 행의 Σ GPU 셀 — 장치별 색 칩(텍스트)."""
    if gpu is None:
        return c("?", "dim")
    if gpu == 0:
        return c("-", "dim")
    toks = _gpu_tokens(products)
    return c("Σ ", "dim") + (toks or c(str(gpu), "magenta"))


def _table(headers, rows, aligns=None):
    """간단한 모노스페이스 테이블 렌더."""
    cols = len(headers)
    aligns = aligns or ["l"] * cols
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(_plain(str(row[i]))))

    def fmt(cells, bold=False):
        parts = []
        for i in range(cols):
            cell = str(cells[i])
            pad = widths[i] - len(_plain(cell))
            if aligns[i] == "r":
                parts.append(" " * pad + cell)
            else:
                parts.append(cell + " " * pad)
        line = "  ".join(parts)
        return c(line, "bold") if bold else line

    out = [fmt(headers, bold=True)]
    out.append(c("-" * (sum(widths) + 2 * (cols - 1)), "dim"))
    for row in rows:
        out.append(fmt(row))
    return "\n".join(out)


def _plain(s):
    """ANSI 코드 제거한 표시 길이 계산용."""
    res = []
    i = 0
    while i < len(s):
        if s[i] == "\033":
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1
        else:
            res.append(s[i])
            i += 1
    return "".join(res)


def render(snap, settings):
    lines = []
    s = snap["summary"]
    ver = snap.get("version", __version__)
    lines.append(c("=== Model Monitor ===", "bold")
                 + c(" v%s" % ver, "cyan")
                 + c("  %s" % snap["ts"], "dim"))
    lines.append("")

    # 핵심 요약
    running = c(str(s["deployments_healthy"]), "green")
    unhealthy = c(str(s["deployments_unhealthy"]),
                  "red" if s["deployments_unhealthy"] else "dim")
    summary_bits = [
        "model groups: %s" % c(str(s["model_groups"]), "cyan"),
        "registered: %s" % c(str(s["deployments_registered"]), "cyan"),
        "running(healthy): %s" % running,
        "unhealthy: %s" % unhealthy,
    ]
    if s.get("backend_pods_known"):
        summary_bits.append(
            "replicas: %s/%s" % (
                c(str(s["backend_pods_ready"]), "green"),
                s["backend_pods_desired"] or "?")
        )
    if s.get("gpu_known"):
        prods = s.get("gpu_products") or {}
        detail = (" (%s)" % ",".join("%s×%d" % (p, n)
                                     for p, n in sorted(prods.items()))) if prods else ""
        summary_bits.append(
            "gpu: %s%s" % (c(str(s["gpu_total"]), "green"), c(detail, "dim")))
    if settings["probe_backends"]:
        summary_bits.append(
            "backends up: %s/%s" % (
                c(str(s["backends_up"]), "green"), s["backends_total"])
        )
    lines.append("  " + "   ".join(summary_bits))
    lines.append("")

    ll = snap.get("litellm")
    if ll is None:
        lines.append(c("  (LiteLLM URL 미설정 — --litellm-url 또는 config 필요)", "yellow"))
    else:
        if not ll["reachable"]:
            lines.append(c("  [LiteLLM] 연결 실패: %s" % ll["url"], "red"))
            for e in ll["errors"]:
                lines.append(c("    - %s" % e, "red"))
        else:
            # 모델 그룹 테이블
            rows = []
            for g in ll["groups"]:
                providers = ",".join(g.get("providers") or []) or "-"
                rows.append([
                    g.get("model_group", "?"),
                    providers,
                    g.get("mode") or "-",
                ])
            if rows:
                lines.append(c("  [Model Groups] (LiteLLM /model_group/info)", "bold"))
                lines.append(indent(_table(
                    ["MODEL_GROUP", "PROVIDERS", "MODE"], rows), 2))
                lines.append("")

            # Deployments: model_name -> api_base (/model/info) + 상태(/health)
            #              + LB 뒤 backend Pod 개수
            merged = merge_deployments_with_health(ll)
            show_backends = snap.get("backend_count_enabled")
            show_gpu = bool((snap.get("summary") or {}).get("gpu_known"))
            if merged:
                # model_name 으로 묶어 표시. 백엔드가 1개뿐이고 공유도 아니면 한 줄로
                # 간결히, 여러 백엔드(로드밸런싱)면 그룹 헤더(합성 상태 + Σ) + 자식 줄.
                # 여러 model_name 이 같은 Service 를 공유하면 ⇄shared 로 명시.
                shared = _shared_map(merged)
                order, groups = [], {}
                for d in merged:
                    nm = d.get("model_name", "?")
                    if nm not in groups:
                        groups[nm] = []
                        order.append(nm)
                    groups[nm].append(d)

                def _shared_suffix(b, nm):
                    others = sorted(x for x in shared.get(_svc_key(b), ()) if x != nm)
                    return c("  ⇄shared:%s" % ",".join(others), "yellow") if others else ""

                drows = []
                for nm in order:
                    bes = groups[nm]
                    multi = len(bes) > 1
                    if not multi:
                        # 단일 백엔드: 한 줄(기존 평면 형태) + 공유 시 마커
                        d = bes[0]
                        color = {"UP": "green", "DOWN": "red"}.get(d["status"], "yellow")
                        row = [c(d["status"], color), nm, d.get("type", "-")]
                        if show_backends:
                            row.append(_fmt_backends(d))
                            if show_gpu:
                                row.append(_fmt_gpu(d))
                            row.append(c(d.get("backend_source", "-"), "dim"))
                        row.append((d.get("api_base") or "-") + _shared_suffix(d, nm))
                        drows.append(row)
                        continue
                    # 여러 백엔드: 그룹 헤더 + 자식
                    st = _composite_status(bes)
                    scolor = {"UP": "green", "DOWN": "red"}.get(st, "yellow")
                    grow = [c(st, scolor),
                            "%s %s" % (nm, c("(%d)" % len(bes), "dim")),
                            bes[0].get("type", "-")]
                    if show_backends:
                        r, dd = _sum_backends(bes)
                        grow.append(_fmt_agg_backends(r, dd))
                        if show_gpu:
                            g, gp = _sum_gpu(bes)
                            grow.append(_fmt_agg_gpu(g, gp))
                        grow.append("")
                    grow.append("")
                    drows.append(grow)
                    for b in bes:
                        bcolor = {"UP": "green", "DOWN": "red"}.get(b.get("status"), "yellow")
                        label = "  ↳ %s%s" % (
                            b.get("service") or b.get("api_base") or "-",
                            _shared_suffix(b, nm))
                        crow = [c(b.get("status", "?"), bcolor), label, ""]
                        if show_backends:
                            crow.append(_fmt_backends(b))
                            if show_gpu:
                                crow.append(_fmt_gpu(b))
                            crow.append(c(b.get("backend_source", "-"), "dim"))
                        crow.append(b.get("api_base") or "-")
                        drows.append(crow)
                hdr = ["STATUS", "MODEL_NAME", "TYPE"]
                if show_backends:
                    hdr += ["REPLICAS"]
                    if show_gpu:
                        hdr += ["GPU"]
                    hdr += ["SRC"]
                hdr.append("API_BASE")
                title = ("  [Deployments] (/model/info api_base + /health status"
                         + (" + k8s replicas/GPU)" if show_backends else ")"))
                lines.append(c(title, "bold"))
                lines.append(indent(_table(hdr, drows), 2))
                lines.append("")
            else:
                # /model/info 가 없으면 /health 의 raw 엔드포인트라도 표시
                health = ll.get("health") or {}
                hrows = []
                for ep in health.get("healthy_endpoints") or []:
                    hrows.append([c("UP", "green"), ep.get("model", "?"),
                                  ep.get("api_base", "-")])
                for ep in health.get("unhealthy_endpoints") or []:
                    hrows.append([c("DOWN", "red"), ep.get("model", "?"),
                                  ep.get("api_base", ep.get("error", "-"))])
                if hrows:
                    lines.append(c("  [Deployments Health] (LiteLLM /health)", "bold"))
                    lines.append(indent(_table(
                        ["STATUS", "MODEL", "API_BASE"], hrows), 2))
                    lines.append("")
            if ll["errors"]:
                for e in ll["errors"]:
                    lines.append(c("  ! %s" % e, "yellow"))

    # 백엔드 직접 probe
    if settings["probe_backends"]:
        brows = []
        for b in snap["backends"]:
            status = c("UP", "green") if b["up"] else c("DOWN", "red")
            models = ",".join(b["models"]) if b["models"] else (b.get("error") or "-")
            brows.append([status, b["name"], b["type"], models])
        lines.append(c("  [Backends] (direct /v1/models probe)", "bold"))
        lines.append(indent(_table(
            ["STATUS", "NAME", "TYPE", "MODELS"], brows), 2))

    return "\n".join(lines)


def indent(text, n):
    pad = " " * n
    return "\n".join(pad + ln for ln in text.split("\n"))


# ----------------------------------------------------------------------------
# Demo data
# ----------------------------------------------------------------------------


def demo_snapshot():
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "litellm": {
            "url": "http://litellm:4000 (demo)",
            "reachable": True,
            "groups": [
                {"model_group": "KServe-Qwen3.6-35B-A3B-FP8",
                 "providers": ["openai"], "mode": "chat"},
                {"model_group": "SGlang-Qwen3.6-27B-FP8",
                 "providers": ["openai"], "mode": "chat"},
                {"model_group": "Qwen3-Embedding-8B",
                 "providers": ["openai"], "mode": "embedding"},
            ],
            # /model/info: model_name -> api_base (여기서 실제 주소가 나온다)
            #              + LB 뒤 backend Pod 개수 (k8s EndpointSlice 등)
            "deployments": [
                {"model_name": "KServe-Qwen3.6-35B-A3B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                 "api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1",
                 "id": "a1b2c3", "type": "kserve",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "kserve",
                 "service": "qwen36-35b-predictor",
                 "gpu_ready": 6, "gpu_products": {"H100": 6}},
                {"model_name": "SGlang-Qwen3.6-27B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-27B-FP8",
                 "api_base": "http://qwen36-27b-sglang.serving.svc:30000/v1",
                 "id": "d4e5f6", "type": "sglang",
                 "backends_ready": 1, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen36-27b-sglang",
                 "gpu_ready": 4, "gpu_products": {"H100": 4}},
                {"model_name": "vLLM-Stack-Qwen3-32B-AWQ",
                 "underlying": "hosted_vllm/Qwen3-32B-AWQ",
                 "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1",
                 "id": "g7h8i9", "type": "vllm",
                 "backends_ready": 0, "backends_desired": 2,
                 "backend_source": "deployment", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen3-32b-vllm",
                 "gpu_ready": 0, "gpu_products": {}},
                {"model_name": "Qwen3-Embedding-8B",
                 "underlying": "openai/Qwen3-Embedding-8B",
                 "api_base": "http://qwen3-embd-predictor.kserve.svc:8080/v1",
                 "id": "j1k2l3", "type": "kserve",
                 "backends_ready": 0, "backends_desired": 0,
                 "backend_source": "knative-pa", "mode": "Serverless",
                 "scale_to_zero": True, "namespace": "kserve",
                 "service": "qwen3-embd-predictor",
                 "gpu_ready": 0, "gpu_products": {}},
                # 같은 model_name 에 백엔드 2개 (로드밸런싱) — 모델 그룹 뷰의 1:N 팬아웃 예시
                {"model_name": "KServe-Qwen3.6-35B-A3B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                 "api_base": "http://qwen36-35b-predictor-2.kserve.svc:8080/v1",
                 "id": "a1b2c4", "type": "kserve",
                 "backends_ready": 2, "backends_desired": 2,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "kserve",
                 "service": "qwen36-35b-predictor-2",
                 "gpu_ready": 2, "gpu_products": {"B200": 2}},
                # 다른 model_name 이 위 predictor Service 를 공유 — 그래프/SHARED 배지 예시
                {"model_name": "Router-Qwen3.6-35B",
                 "underlying": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                 "api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1",
                 "id": "a1b2c5", "type": "vllm",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "kserve",
                 "service": "qwen36-35b-predictor",
                 "gpu_ready": 6, "gpu_products": {"H100": 6}},
            ],
            "health": {
                "healthy_count": 3,
                "unhealthy_count": 1,
                "healthy_endpoints": [
                    {"model": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                     "api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"},
                    {"model": "hosted_vllm/Qwen3.6-27B-FP8",
                     "api_base": "http://qwen36-27b-sglang.serving.svc:30000/v1"},
                    {"model": "openai/Qwen3-Embedding-8B",
                     "api_base": "http://qwen3-embd-predictor.kserve.svc:8080/v1"},
                    {"model": "hosted_vllm/Qwen3.6-35B-A3B-FP8",
                     "api_base": "http://qwen36-35b-predictor-2.kserve.svc:8080/v1"},
                ],
                "unhealthy_endpoints": [
                    {"model": "hosted_vllm/Qwen3-32B-AWQ",
                     "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1"},
                ],
            },
            "models": ["KServe-Qwen3.6-35B-A3B-FP8", "SGlang-Qwen3.6-27B-FP8",
                       "vLLM-Stack-Qwen3-32B-AWQ", "Qwen3-Embedding-8B"],
            "errors": [],
        },
        "backends": [],
        "backend_count_enabled": True,
        "summary": {},
    }
    # 데모에서도 자동 발견 + probe 결과처럼 보이게 backends 채움
    snap["backends"] = [
        {"name": "KServe-Qwen3.6-35B-A3B-FP8",
         "url": "http://qwen36-35b-predictor.kserve.svc:8080",
         "type": "kserve", "up": True,
         "models": ["Qwen3.6-35B-A3B-FP8"], "error": None},
        {"name": "SGlang-Qwen3.6-27B-FP8",
         "url": "http://qwen36-27b-sglang.serving.svc:30000",
         "type": "sglang", "up": True,
         "models": ["Qwen3.6-27B-FP8"], "error": None},
        {"name": "vLLM-Stack-Qwen3-32B-AWQ",
         "url": "http://qwen3-32b-vllm.serving.svc:8000",
         "type": "vllm", "up": False, "models": [], "error": "connection error"},
    ]
    snap["litellm"]["groups"].sort(
        key=lambda g: str(g.get("model_group") or "").lower())
    snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])
    snap["summary"] = summarize(snap)
    return snap


# ----------------------------------------------------------------------------
# Web 대시보드 (--serve): stdlib http.server, 인라인 self-contained 페이지
# ----------------------------------------------------------------------------

import http.server  # noqa: E402

_DASHBOARD_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Monitor — LiteLLM / KServe / vLLM·SGLang</title>
<style>
  :root{
    --bg:#0d1117; --surface:#161b22; --surface2:#1b222c; --border:#232c38;
    --text:#e6edf3; --muted:#8b97a7; --faint:#5b6675;
    --accent:#6e8bff; --up:#3fb950; --down:#f85149; --warn:#d29922;
    --mono:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
    font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1240px;margin:0 auto;padding:20px 22px 60px}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
    padding-bottom:16px;border-bottom:1px solid var(--border);margin-bottom:20px}
  h1{font-size:17px;font-weight:650;margin:0;letter-spacing:-.01em}
  h1 .dim{color:var(--faint);font-weight:400}
  h1 .ver{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent);
    background:rgba(110,139,255,.12);border:1px solid rgba(110,139,255,.3);
    border-radius:5px;padding:1px 6px;vertical-align:middle}
  .chain{font-family:var(--mono);font-size:11.5px;color:var(--muted);
    background:var(--surface);border:1px solid var(--border);border-radius:6px;
    padding:3px 9px}
  .spacer{flex:1}
  .meta{font-family:var(--mono);font-size:12px;color:var(--muted);
    display:flex;align-items:center;gap:14px}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
  .dot.live{background:var(--up);box-shadow:0 0 0 0 rgba(63,185,80,.5);
    animation:pulse 2s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(63,185,80,.45)}
    70%{box-shadow:0 0 0 6px rgba(63,185,80,0)}100%{box-shadow:0 0 0 0 rgba(63,185,80,0)}}
  @media (prefers-reduced-motion:reduce){.dot.live{animation:none}}
  .toggle{font-family:var(--sans);font-size:12px;color:var(--muted);cursor:pointer;
    user-select:none;display:flex;align-items:center;gap:6px}
  .toggle input{accent-color:var(--accent)}
  a.exp{font-family:var(--sans);font-size:12px;color:var(--muted);text-decoration:none;
    border:1px solid var(--border);border-radius:6px;padding:3px 9px;cursor:pointer}
  a.exp:hover{color:var(--text);border-color:var(--accent)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:12px;margin-bottom:22px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:9px;
    padding:14px 16px}
  .card .label{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
    color:var(--muted);margin-bottom:7px}
  .card .val{font-family:var(--mono);font-size:26px;font-weight:600;
    font-variant-numeric:tabular-nums;line-height:1}
  .card .sub{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin-top:6px}
  .val.good{color:var(--up)} .val.bad{color:var(--down)} .val.warn{color:var(--warn)}
  .val.accent{color:var(--accent)}

  section{margin-bottom:26px}
  .sec-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
    color:var(--muted);margin:0 0 10px;display:flex;align-items:center;gap:8px}
  .sec-title .src{font-family:var(--mono);text-transform:none;letter-spacing:0;
    color:var(--faint);font-size:11px}
  .filters{display:flex;align-items:center;gap:7px;margin-left:auto;
    text-transform:none;letter-spacing:0}
  .filters label{font-size:10px;color:var(--faint)}
  .filters select{font-family:var(--sans);font-size:12px;color:var(--text);
    background:var(--surface2);border:1px solid var(--border);border-radius:6px;
    padding:3px 7px;cursor:pointer}
  .filters .fcount{font-family:var(--mono);font-size:11px;color:var(--faint);
    min-width:54px;text-align:right}

  .tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:9px;
    background:var(--surface)}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
    text-align:left;font-weight:500;padding:10px 14px;border-bottom:1px solid var(--border);
    white-space:nowrap}
  td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tbody tr:hover{background:var(--surface2)}
  td.mono,th.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
  td.api{font-family:var(--mono);font-size:12px;color:var(--muted);max-width:340px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  td.name{font-weight:550}

  .pill{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);
    font-size:11px;font-weight:600;padding:2px 9px;border-radius:20px;
    border:1px solid transparent}
  .pill.up{color:var(--up);background:rgba(63,185,80,.10);border-color:rgba(63,185,80,.3)}
  .pill.down{color:var(--down);background:rgba(248,81,73,.10);border-color:rgba(248,81,73,.3)}
  .pill.unk{color:var(--muted);background:rgba(139,151,167,.1);border-color:var(--border)}
  .chip{font-family:var(--mono);font-size:11px;color:var(--muted);
    background:var(--surface2);border:1px solid var(--border);border-radius:5px;
    padding:1px 7px}

  /* BACKENDS 셀: ready/desired 미니바 + 수치 */
  .bk{display:flex;align-items:center;gap:9px}
  .bk .num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px;
    min-width:46px}
  .bk .bar{flex:1;max-width:90px;height:6px;border-radius:3px;background:var(--surface2);
    overflow:hidden;border:1px solid var(--border)}
  .bk .bar i{display:block;height:100%;border-radius:3px}
  .bk.good .num{color:var(--up)} .bk.good .bar i{background:var(--up)}
  .bk.warn .num{color:var(--warn)} .bk.warn .bar i{background:var(--warn)}
  .bk.bad .num{color:var(--down)} .bk.bad .bar i{background:var(--down)}
  .bk.zero .num{color:var(--warn)}
  .bk .note{font-size:10.5px;color:var(--faint)}
  .srccol{font-family:var(--mono);font-size:11px;color:var(--faint)}
  .val.gpuval{color:#b083f0}
  /* GPU 장치 칩 (혼합 GPU: 장치별 색) */
  .gchips{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
  .gchip{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10.5px;
    font-weight:600;padding:0 7px;line-height:18px;border-radius:20px;border:1px solid}
  .gchip .dot{width:6px;height:6px;border-radius:50%}
  .gtot{font-family:var(--mono);font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums}
  /* 헤드라인 GPU 카드: 세그먼트 바 + 범례 */
  .gpusub{display:flex;flex-direction:column;gap:6px;margin-top:8px}
  .gbar{display:flex;height:10px;width:100%;max-width:160px;border-radius:4px;
    overflow:hidden;border:1px solid var(--border)}
  .gbar i{display:block;height:100%}
  .glegend{display:flex;gap:9px;flex-wrap:wrap}
  .glegend span{font-family:var(--mono);font-size:10px;color:var(--muted);
    display:inline-flex;align-items:center;gap:4px}
  .glegend i{width:7px;height:7px;border-radius:2px;display:inline-block}

  /* 모델 기준 그룹 뷰 */
  .pill.deg{color:var(--warn);background:rgba(210,153,34,.12);border-color:rgba(210,153,34,.35)}
  tr.grp-row td{background:var(--surface);border-top:2px solid var(--border)}
  tr.grp-row:first-child td{border-top:none}
  tr.grp-row td.name{font-weight:600}
  tr.grp-row .nbk{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:8px}
  tr.child-row td{background:#0f141b;font-size:12.5px;padding-top:7px;padding-bottom:7px}
  tr.child-row td.leg{font-family:var(--mono);color:var(--muted);padding-left:26px;position:relative}
  tr.child-row td.leg::before{content:"↳";position:absolute;left:12px;color:var(--faint)}
  tr.child-row td.leg .svc{color:var(--text)}
  .agg{display:flex;align-items:center;gap:9px}
  .agg .lead{font-family:var(--mono);font-size:11px;color:var(--faint)}
  .shared{font-family:var(--mono);font-size:9.5px;font-weight:700;color:var(--warn);
    background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);border-radius:4px;
    padding:0 5px;margin-left:8px;letter-spacing:.02em;white-space:nowrap}

  /* Model ↔ Backend 그래프 */
  #graph-sec .src{font-family:var(--mono);text-transform:none;letter-spacing:0;
    color:var(--faint);font-size:11px}
  .graphwrap{overflow:auto;max-height:560px;border:1px solid var(--border);border-radius:9px;
    background:#0c1118;padding:6px 8px}
  .graphwrap svg{display:block;width:100%;height:auto;min-width:520px}
  .glabel{font-family:var(--mono);font-size:10px;fill:var(--muted);
    text-transform:uppercase;letter-spacing:.08em}
  .gnode rect{stroke-width:1.5}
  .gnode .ntext{font-family:var(--mono);font-size:12px;font-weight:600;fill:var(--text)}
  .gnode .nsub{font-family:var(--mono);font-size:10px;fill:var(--faint)}
  .gnode.model rect{fill:rgba(110,139,255,.12);stroke:rgba(110,139,255,.55)}
  .gnode.be.up rect{fill:rgba(63,185,80,.10);stroke:rgba(63,185,80,.5)}
  .gnode.be.down rect{fill:rgba(248,81,73,.10);stroke:rgba(248,81,73,.5)}
  .gnode.be.warn rect{fill:rgba(210,153,34,.12);stroke:rgba(210,153,34,.5)}
  .gnode.be.zero rect{fill:rgba(210,153,34,.08);stroke:rgba(210,153,34,.4)}
  .gnode.be.unk rect{fill:var(--surface2);stroke:var(--border)}
  .gnode.be.shared rect{stroke-dasharray:4 3}
  .gedge{stroke:var(--faint);stroke-width:1.6;fill:none;opacity:.5}
  .gedge.shared{stroke:var(--warn);stroke-width:2;opacity:.8}
  .badge-shared{font-family:var(--mono);font-size:9px;font-weight:700;fill:var(--warn)}
  svg.focusing .gedge{opacity:.07}
  svg.focusing .gnode{opacity:.28}
  svg.focusing .gedge.on{opacity:.95}
  svg.focusing .gnode.on{opacity:1}
  @media (prefers-reduced-motion:no-preference){.gedge,.gnode{transition:opacity .12s ease}}

  .empty{color:var(--muted);padding:18px;text-align:center;font-style:italic}
  .err{color:var(--down);font-family:var(--mono);font-size:12px;
    background:rgba(248,81,73,.07);border:1px solid rgba(248,81,73,.25);
    border-radius:7px;padding:9px 12px;margin-top:8px}
  #banner:not(:empty){margin-bottom:16px}
  .note-banner{color:var(--warn);font-family:var(--mono);font-size:12px;
    background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.3);
    border-radius:7px;padding:9px 12px}
  footer{margin-top:30px;color:var(--faint);font-size:11.5px;font-family:var(--mono);
    border-top:1px solid var(--border);padding-top:14px}

  /* per-user(키별) 뷰 바 */
  #userbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    background:var(--surface);border:1px solid var(--border);border-radius:9px;
    padding:10px 14px;margin-bottom:18px}
  #userbar .uv-title{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
    color:var(--muted)}
  #userbar input[type=password]{font-family:var(--mono);font-size:12.5px;
    color:var(--text);background:var(--surface2);border:1px solid var(--border);
    border-radius:6px;padding:5px 10px;min-width:240px;flex:1;max-width:360px}
  #userbar input[type=password]:focus{outline:none;border-color:var(--accent)}
  #userbar button.exp{background:var(--surface2)}
  .uv-status{font-family:var(--mono);font-size:11.5px;color:var(--faint)}
  .uv-status.ok{color:var(--up)} .uv-status.bad{color:var(--down)}
  .uv-note{font-size:10.5px;color:var(--faint)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Model Monitor <span class="ver" id="ver"></span>
      <span class="dim">· 떠 있는 모델 &amp; LB 뒤 backend</span></h1>
    <span class="chain">LiteLLM → KServe → vLLM / SGLang</span>
    <span class="spacer"></span>
    <label class="toggle"><input type="checkbox" id="auto" checked> auto-refresh</label>
    <a class="exp" id="exp-json" href="/snapshot.json" title="현재 상태를 raw JSON 파일로 다운로드 (공유용)">💾 JSON</a>
    <a class="exp" id="exp-html" href="/snapshot.html" target="_blank" title="현재 상태를 정지된 self-contained 페이지로 열기 (저장해서 공유)">정지 페이지</a>
    <div class="meta">
      <span><span class="dot live" id="livedot"></span><span id="updated">…</span></span>
    </div>
  </header>

  <div id="userbar" style="display:none">
    <span class="uv-title">🔑 키로 조회</span>
    <input id="uv-key" type="password" autocomplete="off" spellcheck="false"
           placeholder="LiteLLM 키 입력 후 Enter (sk-…)">
    <button id="uv-go" class="exp" type="button">조회</button>
    <button id="uv-clear" class="exp" type="button">지우기</button>
    <span class="uv-status" id="uv-status"></span>
    <span class="uv-note">키는 이 브라우저(탭)에만 보관 · 매 요청 헤더로만 전송 · 서버 저장 안 함 · admin 키면 전체 뷰</span>
  </div>

  <div id="banner"></div>

  <div class="cards" id="cards"></div>

  <section id="graph-sec" style="display:none">
    <div class="sec-title">Model ↔ Backend
      <span class="src">model_name → api_base(Service) 라우팅 · ⇄ 공유 백엔드</span>
      <span class="filters">
        <label class="toggle"><input type="checkbox" id="f-graph" checked> show graph</label>
      </span>
    </div>
    <div class="graphwrap" id="graphwrap"></div>
  </section>

  <section id="deployments-sec">
    <div class="sec-title">Deployments
      <span class="src">/model/info api_base · /health status · k8s replicas · GPU</span>
      <span class="filters">
        <label class="toggle"><input type="checkbox" id="f-group" checked> group by model</label>
        <label for="f-status">status</label>
        <select id="f-status">
          <option value="">all</option>
          <option value="UP">UP</option>
          <option value="DOWN">DOWN</option>
          <option value="?">?</option>
        </select>
        <label for="f-type">type</label>
        <select id="f-type">
          <option value="">all</option>
          <option value="vllm">vllm</option>
          <option value="sglang">sglang</option>
          <option value="kserve">kserve</option>
          <option value="-">-</option>
        </select>
        <span class="fcount" id="f-count"></span>
      </span>
    </div>
    <div class="tablewrap"><table id="deployments">
      <thead></thead><tbody></tbody>
    </table></div>
    <div id="dep-err"></div>
  </section>

  <section id="groups-sec">
    <div class="sec-title">Model Groups <span class="src">/model_group/info</span></div>
    <div class="tablewrap"><table id="groups"><thead></thead><tbody></tbody></table></div>
  </section>

  <footer id="foot">model_monitor · 표준 라이브러리만 사용 · 데이터 출처는 LiteLLM + Kubernetes API</footer>
</div>

<script>
const REFRESH_MS = __INTERVAL_MS__;
const USER_VIEW = __USER_VIEW__;   // 서버에서 per-user 뷰 활성 여부 주입
const UV_KEY = "llm_monitor_key";  // 키는 sessionStorage 에만(탭 닫으면 소멸)
const $ = (s)=>document.querySelector(s);
let lastSnap = null;   // 필터 변경 시 재수집 없이 다시 렌더하려고 보관

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

function fmtNum(n){
  if(n==null || isNaN(Number(n))) return String(n);
  return (Math.round(Number(n)*100)/100).toLocaleString();
}
function uvKey(){ return (sessionStorage.getItem(UV_KEY)||"").trim(); }
// 키 필수 모드: 키가 저장돼 있으면 키로 조회한다(있으면 active).
function uvActive(){ return USER_VIEW && !!uvKey(); }
function setUvStatus(msg, cls){ const el=$("#uv-status");
  if(el){ el.textContent=msg||""; el.className="uv-status "+(cls||""); } }

// 키 필수 모드 초기/클리어 상태: 목록 대신 "키 입력" 안내만 보인다(폴링·노출 없음).
function showNeedKey(){
  lastSnap = null;
  $("#banner").innerHTML = '<div class="note-banner">🔑 키를 입력하면 '
    + '내 모델 목록이 보입니다. (admin 키를 넣으면 전체 뷰)</div>';
  $("#cards").innerHTML = "";
  $("#f-count").textContent = "";
  const dt=$("#deployments");
  dt.querySelector("thead").innerHTML="";
  dt.querySelector("tbody").innerHTML='<tr><td class="empty">키 입력 대기 중</td></tr>';
  const gt=$("#groups");
  gt.querySelector("thead").innerHTML="";
  gt.querySelector("tbody").innerHTML='<tr><td class="empty">—</td></tr>';
  setUvStatus("", "");
}

// export 는 admin 키일 때만. 링크 클릭을 가로채 헤더에 키를 실어 fetch -> blob 다운로드/열기.
function exportWithKey(path, open){
  const key=uvKey(); if(!key) return;
  fetch(path,{headers:{"X-LiteLLM-Key":key},cache:"no-store"}).then(r=>{
    if(!r.ok){ setUvStatus("export 는 admin 키가 필요합니다","bad"); return null; }
    return r.blob();
  }).then(b=>{
    if(!b) return;
    const u=URL.createObjectURL(b);
    if(open){ window.open(u,"_blank"); }
    else { const a=document.createElement("a"); a.href=u;
           a.download="model-monitor-snapshot.json"; document.body.appendChild(a);
           a.click(); a.remove(); }
    setTimeout(()=>URL.revokeObjectURL(u), 15000);
  }).catch(()=>setUvStatus("export 실패","bad"));
}

function backendCell(d){
  const r=d.backends_ready, des=d.backends_desired, src=d.backend_source||"none";
  const errTip = d.k8s_error ? ' title="'+esc(d.k8s_error)+'"' : '';
  if(src==="external") return '<span class="srccol">external</span>';
  if(d.scale_to_zero) return '<div class="bk zero"><span class="num">0</span>'
    +'<span class="note">scaled-to-zero</span></div>';
  if(r==null){
    // 수집 실패 -> ? 에 마우스 올리면 원인(k8s_error) 표시
    return '<span class="srccol"'+errTip+'>?'+(d.k8s_error?' ⚠':'')+'</span>';
  }
  let cls="good";
  if(r===0) cls="bad"; else if(des!=null && r<des) cls="warn";
  const num = des!=null ? (r+"/"+des) : (""+r);
  let pct = des && des>0 ? Math.min(100, Math.round(r/des*100)) : (r>0?100:0);
  return '<div class="bk '+cls+'"><span class="num">'+num+'</span>'
    +'<span class="bar"><i style="width:'+pct+'%"></i></span></div>';
}

function statusPill(s){
  const cls = s==="UP"?"up":(s==="DOWN"?"down":"unk");
  return '<span class="pill '+cls+'">'+esc(s)+'</span>';
}

function card(label,val,cls,sub){
  return '<div class="card"><div class="label">'+esc(label)+'</div>'
    +'<div class="val '+(cls||"")+'">'+val+'</div>'
    +(sub?'<div class="sub">'+sub+'</div>':'')+'</div>';
}

// ── 모델 기준 그룹 뷰 헬퍼 ──────────────────────────────────────────────
// 같은 백엔드(Service)를 여러 model_name 이 공유할 수 있으므로 (ns,svc) 로 식별.
function svcKeyOf(d){
  if(d.namespace || d.service) return (d.namespace||"")+"/"+(d.service||"");
  return "api:"+(d.api_base||"external");
}
function trunc(s,n){s=String(s==null?"":s);return s.length>n?s.slice(0,n-1)+"…":s;}
function backendHost(api){ if(!api) return "";
  return String(api).replace(/^[a-z]+:\/\//,"").replace(/[:\/].*$/,""); }
function compositeStatus(bes){
  let up=0,down=0;
  bes.forEach(b=>{const s=b.status||"?"; if(s==="UP")up++; else if(s==="DOWN")down++;});
  if(up===bes.length) return "UP";
  if(down===bes.length && down>0) return "DOWN";
  if(up>0) return "DEGRADED";
  return "?";
}
function statusPillCls(s){return s==="UP"?"up":(s==="DOWN"?"down":(s==="DEGRADED"?"deg":"unk"));}
function compositePill(s){return '<span class="pill '+statusPillCls(s)+'">'+esc(s)+'</span>';}
function sumBackends(bes){
  let r=null,d=null;
  bes.forEach(b=>{ if(b.backends_ready!=null) r=(r||0)+b.backends_ready;
    if(b.backends_desired!=null) d=(d||0)+b.backends_desired; });
  return {r:r,d:d};
}
function aggCell(r,d){
  if(r==null) return '<span class="srccol">?</span>';
  let cls="good"; if(r===0) cls="bad"; else if(d!=null && r<d) cls="warn";
  const num = d!=null?(r+"/"+d):(""+r);
  const pct = d&&d>0?Math.min(100,Math.round(r/d*100)):(r>0?100:0);
  return '<div class="bk '+cls+'"><span class="num">Σ '+num+'</span>'
    +'<span class="bar"><i style="width:'+pct+'%"></i></span></div>';
}
function sharedMap(all){
  const mp={};
  (all||[]).forEach(d=>{const k=svcKeyOf(d);(mp[k]=mp[k]||new Set()).add(d.model_name);});
  return mp;
}
// 장치명 -> 색(고정 매핑 + 해시 폴백). 상태색(green/red)과 겹치지 않는 계열.
const DEV_COLORS={H100:"#b083f0",B200:"#56d4dd",A100:"#6e8bff",L40S:"#d29922",H200:"#f0883e"};
const DEV_POOL=["#b083f0","#56d4dd","#6e8bff","#d29922","#3fb950","#f0883e"];
function devColor(n){ if(DEV_COLORS[n]) return DEV_COLORS[n];
  let h=0; for(const ch of String(n)) h=(h*31+ch.charCodeAt(0))>>>0; return DEV_POOL[h%DEV_POOL.length]; }
// 혼합 GPU: 장치별 색 칩. lead 가 있으면 앞에 총합/Σ 표시.
function gpuChips(products, lead){
  const keys=Object.keys(products||{}).sort();
  const chips=keys.map(k=>{const col=devColor(k);
    return '<span class="gchip" style="color:'+col+';border-color:'+col+'66;background:'+col+'1a">'
      +'<i class="dot" style="background:'+col+'"></i>'+esc(k)+'×'+products[k]+'</span>';}).join("");
  return '<span class="gchips">'+(lead?'<span class="gtot">'+esc(lead)+'</span>':'')+chips+'</span>';
}
// 그래프 노드용 축약 텍스트
function gpuText(g, products){
  if(g==null) return "?"; if(g===0) return "-";
  const ps=products||{}, keys=Object.keys(ps).sort();
  if(keys.length===1) return keys[0]+"×"+ps[keys[0]];
  return keys.map(k=>k+"×"+ps[k]).join("·");
}
function gpuCell(d){
  const g=d.gpu_ready;
  if(g==null) return '<span class="srccol"'+(d.gpu_error?' title="'+esc(d.gpu_error)+'"':'')+'>?'+(d.gpu_error?' ⚠':'')+'</span>';
  if(g===0) return '<span class="srccol">-</span>';
  const keys=Object.keys(d.gpu_products||{});
  return gpuChips(d.gpu_products, keys.length>1?String(g):"");   // 혼합이면 총합 표시
}
function sumGpu(bes){
  let total=null; const products={};
  bes.forEach(b=>{ if(b.gpu_ready!=null){ total=(total||0)+b.gpu_ready;
    const ps=b.gpu_products||{}; for(const k in ps) products[k]=(products[k]||0)+ps[k]; } });
  return {g:total, products:products};
}
function aggGpuCell(g, products){
  if(g==null) return '<span class="srccol">?</span>';
  if(g===0) return '<span class="srccol">-</span>';
  const keys=Object.keys(products||{});
  return gpuChips(products, keys.length>1?("Σ "+g):"Σ");
}
// 헤드라인 GPU 카드: 장치 비율 세그먼트 바 + 범례
function gpuBar(products){
  const keys=Object.keys(products||{}).sort();
  const t=keys.reduce((a,k)=>a+products[k],0)||1;
  const segs=keys.map(k=>'<i style="width:'+(products[k]/t*100)+'%;background:'+devColor(k)
    +'" title="'+esc(k)+'×'+products[k]+'"></i>').join("");
  const leg=keys.map(k=>'<span><i style="background:'+devColor(k)+'"></i>'+esc(k)+'×'+products[k]+'</span>').join("");
  return '<span class="gbar">'+segs+'</span><span class="glegend">'+leg+'</span>';
}
// model_name 으로 묶은 tbody 행들(부모 그룹 행 + 자식 백엔드 행)
function groupedRows(merged, shared, showBk, showGpu){
  const order=[], groups={};
  merged.forEach(d=>{ if(!groups[d.model_name]){groups[d.model_name]=[];order.push(d.model_name);}
    groups[d.model_name].push(d); });
  return order.map(name=>{
    const bes=groups[name];
    const st=compositeStatus(bes), agg=sumBackends(bes), type=bes[0].type||"-";
    let html='<tr class="grp-row"><td>'+compositePill(st)+'</td>'
      +'<td class="name">'+esc(name)
        +'<span class="nbk">'+bes.length+' backend'+(bes.length>1?'s':'')+'</span></td>'
      +'<td><span class="chip">'+esc(type)+'</span></td>';
    if(showBk){ html+='<td>'+aggCell(agg.r,agg.d)+'</td>';
      if(showGpu){const sg=sumGpu(bes); html+='<td>'+aggGpuCell(sg.g,sg.products)+'</td>';}
      html+='<td></td><td></td>'; }
    html+='<td></td></tr>';
    bes.forEach(b=>{
      const k=svcKeyOf(b), sh=shared[k]&&shared[k].size>1;
      const others=sh?[...shared[k]].filter(x=>x!==name):[];
      const svcLabel=b.service||backendHost(b.api_base)||"—";
      html+='<tr class="child-row"><td>'+statusPill(b.status||"?")+'</td>'
        +'<td class="leg"><span class="svc">'+esc(svcLabel)+'</span>'
          +(sh?'<span class="shared">⇄ '+esc(others.join(", "))+'</span>':'')+'</td><td></td>';
      if(showBk){ html+='<td>'+backendCell(b)+'</td>';
        if(showGpu) html+='<td>'+gpuCell(b)+'</td>';
        html+='<td class="mono" style="font-size:12px;color:var(--muted)">'+esc(b.mode||"-")+'</td>'
          +'<td class="srccol">'+esc(b.backend_source||"-")+'</td>'; }
      html+='<td class="api" title="'+esc(b.api_base)+'">'+esc(b.api_base||"-")+'</td></tr>';
    });
    return html;
  }).join("");
}

// ── Model ↔ Backend 이분 그래프 (SVG, 외부 의존성 0) ─────────────────────
function beNodeCls(o){
  if(o.ext) return "unk";
  if(o.stz) return "zero";
  if(o.r==null) return "unk";
  if(o.r===0) return "down";
  if(o.des!=null && o.r<o.des) return "warn";
  return "up";
}
function attrId(s){return String(s).replace(/[^a-zA-Z0-9_-]/g,"_");}
function buildGraph(deps){
  if(!deps || !deps.length) return "";
  const names=[], seenN={};
  deps.forEach(d=>{ if(!seenN[d.model_name]){seenN[d.model_name]=1;names.push(d.model_name);} });
  names.sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase()));
  const svcOrder=[], svc={};
  deps.forEach(d=>{ const k=svcKeyOf(d);
    if(!svc[k]){ svc[k]={key:k,label:d.service||backendHost(d.api_base)||"external",
      r:d.backends_ready,des:d.backends_desired,status:d.status,
      gpu:d.gpu_ready,gpu_products:d.gpu_products,
      ext:(d.backend_source==="external"),stz:d.scale_to_zero,models:new Set()}; svcOrder.push(k); }
    svc[k].models.add(d.model_name); });
  const NW=176, NH=42, GAP=16, PADX=16, PADY=34, W=600;
  const colL=PADX, colR=W-PADX-NW;
  const rows=Math.max(names.length, svcOrder.length);
  const H=PADY+rows*NH+(rows>0?(rows-1)*GAP:0)+18;
  const colY=(k,total)=>{const t=total*NH+(total-1)*GAP; const s=(H-PADY-18-t)/2+PADY; return s+k*(NH+GAP);};
  const mY={}, sY={};
  names.forEach((n,i)=>mY[n]=colY(i,names.length));
  svcOrder.forEach((k,i)=>sY[k]=colY(i,svcOrder.length));
  let edges="";
  deps.forEach(d=>{ const k=svcKeyOf(d), sh=svc[k].models.size>1;
    const y1=mY[d.model_name]+NH/2, y2=sY[k]+NH/2, x1=colL+NW, x2=colR, mx=(x1+x2)/2;
    edges+='<path class="gedge'+(sh?' shared':'')+'" data-m="'+attrId(d.model_name)+'" data-b="'+attrId(k)+'" '
      +'d="M'+x1+' '+y1+' C'+mx+' '+y1+' '+mx+' '+y2+' '+x2+' '+y2+'"></path>'; });
  let mnodes="";
  names.forEach(n=>{ const y=mY[n];
    mnodes+='<g class="gnode model" data-key="'+attrId(n)+'" data-kind="m">'
      +'<rect x="'+colL+'" y="'+y+'" width="'+NW+'" height="'+NH+'" rx="8"></rect>'
      +'<text class="ntext" x="'+(colL+12)+'" y="'+(y+19)+'">'+esc(trunc(n,22))+'</text>'
      +'<text class="nsub" x="'+(colL+12)+'" y="'+(y+33)+'">model_name</text></g>'; });
  let snodes="";
  svcOrder.forEach(k=>{ const o=svc[k], sh=o.models.size>1, y=sY[k];
    const gtxt=(o.gpu!=null && o.gpu>0)?("  ·  "+gpuText(o.gpu,o.gpu_products)):"";
    const sub=o.ext?"external":((o.r==null?"?":o.r+(o.des!=null?"/"+o.des:""))+" pods"+gtxt+(sh?"  ·  shared ×"+o.models.size:""));
    snodes+='<g class="gnode be '+beNodeCls(o)+(sh?' shared':'')+'" data-key="'+attrId(k)+'" data-kind="b">'
      +'<rect x="'+colR+'" y="'+y+'" width="'+NW+'" height="'+NH+'" rx="8"></rect>'
      +'<text class="ntext" x="'+(colR+12)+'" y="'+(y+19)+'">'+esc(trunc(o.label,22))+'</text>'
      +'<text class="nsub" x="'+(colR+12)+'" y="'+(y+33)+'">'+esc(sub)+'</text>'
      +(sh?'<text class="badge-shared" x="'+(colR+NW-8)+'" y="'+(y+15)+'" text-anchor="end">⇄</text>':'')
      +'</g>'; });
  return '<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Model 과 Backend 라우팅 그래프">'
    +'<text class="glabel" x="'+colL+'" y="20">Models</text>'
    +'<text class="glabel" x="'+colR+'" y="20">Backends (Service)</text>'
    +edges+mnodes+snodes+'</svg>';
}
function wireGraph(svg){
  if(!svg) return;
  const clear=()=>{svg.classList.remove("focusing");
    svg.querySelectorAll(".on").forEach(e=>e.classList.remove("on"));};
  svg.querySelectorAll(".gnode").forEach(node=>{
    node.addEventListener("mouseenter",()=>{
      const kind=node.getAttribute("data-kind"), key=node.getAttribute("data-key");
      svg.classList.add("focusing"); node.classList.add("on");
      svg.querySelectorAll(".gedge").forEach(ed=>{
        const match = kind==="m" ? ed.getAttribute("data-m")===key : ed.getAttribute("data-b")===key;
        if(match){ ed.classList.add("on");
          const other = kind==="m" ? ed.getAttribute("data-b") : ed.getAttribute("data-m");
          const on=svg.querySelector((kind==="m"?'.gnode.be[data-key="':'.gnode.model[data-key="')+other+'"]');
          if(on) on.classList.add("on"); }
      });
    });
    node.addEventListener("mouseleave",clear);
  });
}

function render(snap){
  const s = snap.summary||{};
  const ll = snap.litellm;
  // 수집 상태 배너: 초기 로딩 / 백그라운드 수집 실패를 화면에 노출한다.
  const banner = $("#banner");
  if(snap.loading){
    banner.innerHTML = '<div class="note-banner">⏳ 첫 스냅샷 수집 중…'
      + (snap.error ? ' ('+esc(snap.error)+')' : '') + '</div>';
    $("#updated").textContent = "loading…";
    $("#livedot").style.background = "var(--warn)";
    return;
  }
  banner.innerHTML = snap.collect_error
    ? '<div class="err">⚠ 백그라운드 수집 실패(직전 스냅샷 표시 중): '
      + esc(snap.collect_error) + '</div>'
    : "";
  lastSnap = snap;
  // summary cards
  let cards = "";
  // per-user 뷰: 맨 앞에 "내 키" 카드(접근 모델 수 / spend·예산 / rate limit·만료)
  if(snap.user_view){
    const ki = snap.key_info || {};
    cards += card("내 접근 모델", (snap.accessible_count||0), "accent",
      ki.key_alias ? esc(ki.key_alias) : "내 키 기준");
    if(ki.spend!=null || ki.max_budget!=null){
      const sp = ki.spend!=null ? "$"+fmtNum(ki.spend) : "—";
      let sub = "&nbsp;";
      if(ki.max_budget!=null){
        const rem = ki.spend!=null ? (ki.max_budget - ki.spend) : null;
        sub = "예산 $"+fmtNum(ki.max_budget)
            + (rem!=null ? " · 잔액 $"+fmtNum(rem) : "");
      }
      cards += card("Spend", sp, "", sub);
    }
    if(ki.tpm_limit!=null || ki.rpm_limit!=null || ki.expires){
      const tpm = ki.tpm_limit!=null ? fmtNum(ki.tpm_limit) : "∞";
      const rpm = ki.rpm_limit!=null ? fmtNum(ki.rpm_limit) : "∞";
      const exp = ki.expires ? "만료 "+esc(String(ki.expires).slice(0,10)) : "";
      cards += card("Rate limit",
        tpm+'<span style="color:var(--faint);font-size:14px"> tpm</span>',
        "", "rpm "+rpm+(exp?" · "+exp:""));
    }
  }
  cards += card("Model Groups", s.model_groups||0, "accent");
  cards += card("Registered", s.deployments_registered||0, "");
  cards += card("Running (healthy)", s.deployments_healthy||0, "good",
    "unhealthy "+(s.deployments_unhealthy||0));
  if(s.backend_pods_known)
    cards += card("Replicas", (s.backend_pods_ready||0)
      +'<span style="color:var(--faint);font-size:16px"> / '
      +(s.backend_pods_desired||"?")+'</span>', "",
      "LB 뒤 ready / desired");
  // GPU 카드는 전체(admin) 뷰에서만 — per-user 뷰는 내부정보로 숨김.
  // 혼합 GPU 는 세그먼트 바 + 범례로 비중을 보여준다.
  if(s.gpu_known && !snap.user_view){
    const gp=s.gpu_products||{};
    const sub = Object.keys(gp).length
      ? '<div class="sub gpusub">'+gpuBar(gp)+'</div>' : '<div class="sub">장치 미상</div>';
    cards += '<div class="card"><div class="label">GPU</div>'
      +'<div class="val gpuval">'+(s.gpu_total||0)+'</div>'+sub+'</div>';
  }
  $("#cards").innerHTML = cards;

  // deployments
  const showBk = !!snap.backend_count_enabled;
  const uHide = !!snap.user_view;   // per-user 뷰는 내부 컬럼(MODE/SRC/API_BASE) 숨김
  const showGpu = !!s.gpu_known && !uHide;   // GPU 컬럼도 내부정보 — user 뷰 숨김
  const dt = $("#deployments");
  if(ll && ll.deployments && ll.deployments.length){
    const all = ll.deployments;
    const fS = $("#f-status").value, fT = $("#f-type").value;
    const merged = all.filter(d=>
      (!fS || (d.status||"?")===fS) && (!fT || (d.type||"-")===fT));
    $("#f-count").textContent = (fS||fT)
      ? merged.length+" / "+all.length : all.length+"";
    let head = "<tr><th>STATUS</th><th>MODEL_NAME</th><th>TYPE</th>";
    if(showBk) head += '<th>REPLICAS (ready/desired)</th>';
    if(showBk && showGpu) head += '<th>GPU</th>';
    if(showBk && !uHide) head += '<th>MODE</th><th>SRC</th>';
    if(!uHide) head += "<th>API_BASE</th>";
    head += "</tr>";
    const ncol = 3 + (showBk?1:0) + (showBk&&showGpu?1:0)
      + (showBk&&!uHide?2:0) + (uHide?0:1);
    dt.querySelector("thead").innerHTML = head;
    // per-user 뷰(uHide)는 내부 토폴로지(Service/api_base)를 숨기므로 그룹/그래프를
    // 쓰지 않고 컬럼 축소된 평면 행을 그린다. 전체(admin) 뷰에서만 모델 그룹핑을 적용.
    const grouped = $("#f-group").checked && !uHide;
    let body;
    if(!merged.length){
      body = '<tr><td class="empty" colspan="'+ncol+'">필터 결과 없음</td></tr>';
    } else if(grouped){
      // model_name 으로 묶어 표시. 공유 백엔드(여러 모델이 한 Service)는 SHARED 로
      // 명시하고, 헤드라인 Pod 합계(summary)는 이미 Service 기준 dedup 됨.
      body = groupedRows(merged, sharedMap(all), showBk, showGpu);
    } else {
      body = merged.map(d=>{
        let row = "<tr><td>"+statusPill(d.status||"?")+"</td>"
          +'<td class="name">'+esc(d.model_name)+"</td>"
          +'<td><span class="chip">'+esc(d.type||"-")+"</span></td>";
        if(showBk) row += "<td>"+backendCell(d)+"</td>";
        if(showBk && showGpu) row += "<td>"+gpuCell(d)+"</td>";
        if(showBk && !uHide) row +=
          '<td class="mono" style="font-size:12px;color:var(--muted)">'+esc(d.mode||"-")+"</td>"
          +'<td class="srccol">'+esc(d.backend_source||"-")+"</td>";
        if(!uHide) row += '<td class="api" title="'+esc(d.api_base)+'">'
          +esc(d.api_base||"-")+"</td>";
        row += "</tr>";
        return row;
      }).join("");
    }
    dt.querySelector("tbody").innerHTML = body;
  } else {
    $("#f-count").textContent="";
    dt.querySelector("thead").innerHTML="";
    dt.querySelector("tbody").innerHTML='<tr><td class="empty">deployment 없음 (LiteLLM /model/info 응답 비어있음 또는 미연결)</td></tr>';
  }
  $("#dep-err").innerHTML = (ll && ll.errors && ll.errors.length)
    ? ll.errors.map(e=>'<div class="err">! '+esc(e)+'</div>').join("") : "";

  // Model ↔ Backend 그래프 (스냅샷 데이터만으로 그림 — 추가 수집 없음).
  // per-user 뷰(uHide)는 내부 토폴로지(Service 이름)를 숨기므로 그래프도 감춘다.
  const gsec = $("#graph-sec"), gwrap = $("#graphwrap");
  const deps = (ll && ll.deployments) || [];
  if(uHide || !deps.length){
    gsec.style.display = "none";
  } else if($("#f-graph").checked){
    gsec.style.display = "";
    gwrap.style.display = "";
    gwrap.innerHTML = buildGraph(deps);
    wireGraph(gwrap.querySelector("svg"));
  } else {
    gsec.style.display = "";        // 섹션(토글)은 두고 그래프만 숨김
    gwrap.style.display = "none";
  }

  // groups
  const gt = $("#groups");
  if(ll && ll.groups && ll.groups.length){
    gt.querySelector("thead").innerHTML="<tr><th>MODEL_GROUP</th><th>PROVIDERS</th><th>MODE</th></tr>";
    gt.querySelector("tbody").innerHTML = ll.groups.map(g=>
      '<tr><td class="name">'+esc(g.model_group)+"</td>"
      +'<td class="mono" style="color:var(--muted)">'+esc((g.providers||[]).join(", ")||"-")+"</td>"
      +"<td>"+esc(g.mode||"-")+"</td></tr>").join("");
  } else {
    gt.querySelector("thead").innerHTML="";
    gt.querySelector("tbody").innerHTML='<tr><td class="empty">model group 없음</td></tr>';
  }

  // 키 필수 모드: export(JSON/정지페이지) 버튼은 admin 키로 본 전체 뷰일 때만 노출.
  if(USER_VIEW){
    const show = snap.admin_view ? "" : "none";
    const ej=$("#exp-json"), eh=$("#exp-html");
    if(ej) ej.style.display=show; if(eh) eh.style.display=show;
  }
  if(snap.version) $("#ver").textContent = "v"+snap.version;
  $("#foot").textContent = "model_monitor v"+(snap.version||"?")
    +" · 표준 라이브러리만 사용 · 데이터 출처는 LiteLLM + Kubernetes API";
  $("#updated").textContent = (snap.ts||"") + (snap.demo?"  (demo)":"");
}

function showUvError(msg){
  // fail-closed: 키 검증 실패 시 절대 global 뷰로 폴백하지 않는다 — 화면을 비우고 에러만.
  lastSnap = null;
  $("#banner").innerHTML = '<div class="err">⚠ 내 모델 보기 실패: '+esc(msg)
    + ' — 키를 확인하세요. (전체 뷰로 폴백하지 않습니다)</div>';
  $("#cards").innerHTML = "";
  $("#f-count").textContent = "";
  const dt=$("#deployments");
  dt.querySelector("thead").innerHTML="";
  dt.querySelector("tbody").innerHTML=
    '<tr><td class="empty">키 검증 실패 — 표시할 데이터 없음</td></tr>';
  const gt=$("#groups");
  gt.querySelector("thead").innerHTML="";
  gt.querySelector("tbody").innerHTML='<tr><td class="empty">—</td></tr>';
  setUvStatus("키 검증 실패", "bad");
}

async function tick(){
  // 정지 스냅샷(/snapshot.html)으로 열렸으면 폴링 없이 박제된 데이터만 렌더한다.
  if(window.__SNAPSHOT__){
    render(window.__SNAPSHOT__);
    $("#livedot").style.background = "var(--warn)";
    document.querySelectorAll(".exp").forEach(e=>e.style.display="none");
    const ub=$("#userbar"); if(ub) ub.style.display="none";  // 정지 페이지는 global 전용
    const a=$("#auto"); if(a){ a.checked=false; a.disabled=true; }
    const u=$("#updated"); if(u) u.textContent += "  · saved snapshot (frozen)";
    return;
  }
  // 키 필수 모드: 키가 없으면 무인증 호출을 아예 하지 않고 안내만.
  if(USER_VIEW && !uvActive()){
    showNeedKey();
    $("#livedot").style.background = "var(--warn)";
    return;
  }
  try{
    let snap;
    if(uvActive()){
      // 키는 헤더 전용(쿼리 금지). 매 요청에 실어 보내고 서버는 저장하지 않는다.
      const r = await fetch("/api/snapshot/user",
        {method:"POST", cache:"no-store",
         headers:{"X-LiteLLM-Key": uvKey()}});
      snap = await r.json().catch(()=>({}));
      if(!r.ok){
        showUvError((snap && snap.error) ? snap.error : ("HTTP "+r.status));
        $("#livedot").style.background = "var(--down)";
        return;
      }
      setUvStatus(snap.admin_view ? "관리자 전체 뷰"
        : "내 모델만 보는 중 ("+(snap.accessible_count||0)+"개)", "ok");
    } else {
      // 레거시(키 필수 OFF): 무인증 global 뷰.
      const r = await fetch("/api/snapshot",{cache:"no-store"});
      snap = await r.json();
    }
    render(snap);
    $("#livedot").style.background = "var(--up)";
  }catch(e){
    $("#livedot").style.background = "var(--down)";
    $("#updated").textContent = "수집 실패: "+e;
  }
}

let timer=null;
function loop(){ if(window.__SNAPSHOT__) return;   // frozen: 폴링 안 함
  if(timer) clearInterval(timer);
  if($("#auto").checked){ timer=setInterval(tick, REFRESH_MS); } }
$("#auto").addEventListener("change", ()=>{ loop(); if($("#auto").checked) tick(); });
// 필터 변경: 재수집 없이 마지막 스냅샷으로 즉시 다시 렌더
$("#f-status").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });
$("#f-type").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });
$("#f-group").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });
$("#f-graph").addEventListener("change", ()=>{ if(lastSnap) render(lastSnap); });

// 키 필수 모드: 키 입력 바 노출 + 조회/지우기/Enter, export 는 admin 키 헤더로.
if(USER_VIEW){
  $("#userbar").style.display = "";
  // 초기엔 export 버튼 숨김(admin 뷰가 로드되면 render 가 노출).
  const ej=$("#exp-json"), eh=$("#exp-html");
  if(ej) ej.style.display="none"; if(eh) eh.style.display="none";
  const keyInput = $("#uv-key");
  keyInput.value = uvKey();   // 같은 탭 내 새로고침 시 복원(sessionStorage)
  // 키는 "조회"(또는 Enter) 시에만 저장·전송한다(타이핑 중 부분키 조회 방지).
  function submitKey(){
    const v = keyInput.value.trim();
    if(!v){ setUvStatus("키를 입력하세요", "bad"); keyInput.focus(); return; }
    sessionStorage.setItem(UV_KEY, v);   // 탭 닫으면 소멸
    tick();
  }
  $("#uv-go").addEventListener("click", submitKey);
  keyInput.addEventListener("keydown", e=>{ if(e.key==="Enter") submitKey(); });
  $("#uv-clear").addEventListener("click", ()=>{
    sessionStorage.removeItem(UV_KEY); keyInput.value=""; tick();
  });
  if(ej) ej.addEventListener("click", e=>{ e.preventDefault();
    exportWithKey("/snapshot.json", false); });
  if(eh) eh.addEventListener("click", e=>{ e.preventDefault();
    exportWithKey("/snapshot.html", true); });
}
tick(); loop();
</script>
</body>
</html>
"""


def serve_dashboard(settings, host, port, interval, demo):
    """웹 대시보드 서버.

    수집(특히 LiteLLM /health 는 느림)을 백그라운드 스레드에서 주기적으로 수행하고,
    HTTP 요청에는 마지막으로 수집한 스냅샷을 즉시 돌려준다 -> 브라우저가 멈추거나
    BrokenPipe 가 나지 않는다.
    """
    # user_view_on = "키 필수 모드". 켜지면 무인증 global 데이터 경로를 잠그고,
    # 모든 데이터는 키로만(POST /api/snapshot/user) 나간다. admin 키는 전체 뷰 해제.
    user_view_on = bool(settings.get("user_view")) and not demo
    hide_internal = bool(settings.get("user_view_hide_internal", True))
    metrics_on = bool(settings.get("metrics", True))
    admin_key = settings.get("api_key") or ""
    uaccess = AccessCache(ttl=float(settings.get("user_view_cache_ttl", 30.0)))

    def is_admin_key(key):
        # 모니터를 띄울 때 쓴 admin 키와 동일하면 admin (상수시간 비교).
        return bool(admin_key) and bool(key) and hmac.compare_digest(key, admin_key)

    def cached_access(key):
        return uaccess.get_or_collect(
            key,
            lambda: collect_user_access(settings.get("litellm_url"), key,
                                        settings.get("timeout", 10.0)),
            time.monotonic())

    html = (_DASHBOARD_HTML
            .replace("__INTERVAL_MS__", str(int(interval * 1000)))
            .replace("__USER_VIEW__", "true" if user_view_on else "false"))

    def frozen_html(snap):
        """현재 스냅샷을 페이지에 박제 -> 폴링 없이 그대로 렌더되는 self-contained HTML.

        '<' 를 이스케이프해 데이터 안의 </script> 등이 HTML 을 깨지 않게 한다.
        라이브 대시보드와 같은 렌더 코드를 쓰므로 stale 될 일이 없다.
        """
        blob = json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c")
        inject = "<script>window.__SNAPSHOT__=%s;</script>\n</head>" % blob
        return html.replace("</head>", inject, 1)

    state = {"snap": None, "err": None}
    lock = threading.Lock()
    # /health 는 수십 초 걸려서 메인 수집을 막지 않도록 별도 스레드가 캐시에 채운다.
    hcache = {"data": None}
    hlock = threading.Lock()

    def collect_once():
        # 메인 수집은 health 없이(빠름). backend 개수·모델 목록은 interval 마다 갱신.
        snap = demo_snapshot() if demo else build_snapshot(settings, with_health=False)
        snap["demo"] = bool(demo)
        if not demo and snap.get("litellm"):
            with hlock:
                h = hcache["data"]
            if h is not None:
                # 비동기로 받아둔 /health 를 주입하고 status/summary 재계산
                snap["litellm"]["health"] = h
                snap["litellm"]["deployments"] = merge_deployments_with_health(
                    snap["litellm"])
                snap["summary"] = summarize(snap)
        with lock:
            state["snap"] = snap
            state["err"] = None

    def health_loop():
        # 느린 /health 를 천천히(>=30s) 따로 수집. 도착하면 다음 collect 에 반영됨.
        url, key = settings.get("litellm_url"), settings.get("api_key")
        ht = settings.get("health_timeout", 90.0)
        while True:
            try:
                h = fetch_health(url, key, ht)
                if h is not None:
                    with hlock:
                        hcache["data"] = h
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(30.0, interval))

    # 첫 페이지 로드에 데이터가 바로 보이도록 1회 동기 수집(health 제외 -> 즉시)
    try:
        collect_once()
    except Exception as e:  # noqa: BLE001
        state["err"] = "%s: %s" % (type(e).__name__, e)

    def refresh_loop():
        while True:
            time.sleep(max(1.0, interval))
            try:
                collect_once()
            except Exception as e:  # noqa: BLE001
                with lock:
                    state["err"] = "%s: %s" % (type(e).__name__, e)

    threading.Thread(target=refresh_loop, daemon=True).start()
    # health 수집은 settings.health 가 켜져 있고 데모가 아닐 때만
    if not demo and settings.get("health", True) and settings.get("litellm_url"):
        threading.Thread(target=health_loop, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype, extra_headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 클라이언트가 응답 전에 끊음(폴링 취소 등) — 무시

        def _snapshot(self):
            """캐시된 스냅샷(없으면 loading, 수집오류면 collect_error 부착)."""
            with lock:
                snap, err = state["snap"], state["err"]
            if snap is None:
                return {"version": __version__, "loading": True,
                        "error": err, "summary": {}, "litellm": None}
            if err:
                return dict(snap, collect_error=err)
            return snap

        def _admin_ok(self):
            """export/잠긴 global 접근 허용 여부 — admin 키 헤더가 맞아야 True."""
            return is_admin_key((self.headers.get("X-LiteLLM-Key") or "").strip())

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                # 키 필수 모드: 무인증 global 데이터 경로를 잠근다(키로만 조회).
                if user_view_on:
                    self._json(403, {"error": "키 필수 모드입니다 — "
                                              "POST /api/snapshot/user 로 조회하세요.",
                                     "needs_key": True})
                    return
                self._send(200, json.dumps(self._snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            elif path == "/snapshot.json":
                # 키 필수 모드면 admin 키 헤더가 있어야 export 허용(전체 데이터 보호).
                if user_view_on and not self._admin_ok():
                    self._json(403, {"error": "export 는 admin 키가 필요합니다."})
                    return
                # 브라우저에서 클릭 한 번에 파일로 받게 attachment 로 내려준다.
                self._send(200, json.dumps(self._snapshot(), ensure_ascii=False),
                           "application/json; charset=utf-8",
                           {"Content-Disposition":
                            'attachment; filename="model-monitor-snapshot.json"'})
            elif path in ("/snapshot.html", "/export"):
                if user_view_on and not self._admin_ok():
                    self._send(403, "export 는 admin 키가 필요합니다.", "text/plain")
                    return
                # 데이터가 박제된 self-contained 페이지(폴링 없음) — 저장해서 공유용.
                self._send(200, frozen_html(self._snapshot()),
                           "text/html; charset=utf-8")
            elif path == "/metrics":
                # Prometheus 스크레이프. 캐시 스냅샷을 포맷만(수집 안 함).
                if not metrics_on:
                    self._send(404, "not found", "text/plain")
                    return
                # 키 필수 모드면 다른 global export 처럼 admin 키 헤더가 있어야 노출.
                if user_view_on and not self._admin_ok():
                    self._send(403, "metrics 는 admin 키가 필요합니다.", "text/plain")
                    return
                self._send(200, render_prometheus_metrics(self._snapshot()),
                           "text/plain; version=0.0.4; charset=utf-8")
            elif path in ("/healthz", "/readyz"):
                self._send(200, "ok", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot/user":
                self._handle_user_snapshot()
            else:
                self._send(404, "not found", "text/plain")

        def _drain_body(self):
            """요청 본문을 읽어 버린다(연결 정리). 본문은 쓰지 않는다."""
            try:
                clen = int(self.headers.get("Content-Length") or 0)
                if clen > 0:
                    self.rfile.read(clen)
            except (ValueError, OSError):
                pass

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8")

        def _handle_user_snapshot(self):
            """키별 per-user 뷰. 키는 **헤더(X-LiteLLM-Key) 전용**(쿼리 금지),
            저장·로그 없이 pass-through. fail-closed: 키 무효면 global 폴백 금지."""
            self._drain_body()
            # 게이트: 운영자가 명시적으로 켜지 않으면 노출 안 함(기본 OFF).
            if not user_view_on:
                self._json(403, {"error": "per-user 뷰가 비활성입니다 "
                                          "(--enable-user-view).", "user_view": True})
                return
            # 키는 헤더 전용 — 쿼리스트링은 프록시/LB 액세스 로그에 남아 금지.
            key = (self.headers.get("X-LiteLLM-Key") or "").strip()
            if not key:
                self._json(400, {"error": "X-LiteLLM-Key 헤더가 필요합니다.",
                                 "user_view": True})
                return
            # admin 키(= 모니터 구동 키)면 전체 global 뷰를 비-redacted 로 돌려준다.
            if is_admin_key(key):
                self._json(200, dict(self._snapshot(), admin_view=True))
                return
            url = settings.get("litellm_url")
            if not url:
                self._json(503, {"error": "LiteLLM 이 설정되지 않았습니다.",
                                 "user_view": True})
                return
            # 일반 키: 접근 목록을 짧은 TTL 캐시로 조회(폴링 중복 호출 제거).
            access = cached_access(key)
            if not access["ok"]:
                # fail-closed: 절대 unfiltered global 로 폴백하지 않는다.
                # 내부 토폴로지(주소) 누출 방지 위해 사유는 일반화한 메시지로만.
                self._json(401, {"error": "유효하지 않거나 만료된 키이거나 "
                                          "LiteLLM 조회에 실패했습니다.",
                                 "user_view": True})
                return
            snap = filter_snapshot_for_user(self._snapshot(), access,
                                            hide_internal=hide_internal)
            self._json(200, snap)

        def log_message(self, *a):  # 액세스 로그 억제
            pass

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d" % ("localhost" if host == "0.0.0.0" else host, port)
    print("Model Monitor 웹 대시보드: %s  (%.0fs 갱신, Ctrl+C 종료)"
          % (url, interval))
    print("  스냅샷 내보내기: %s/snapshot.json (raw JSON 다운로드)"
          "  ·  %s/snapshot.html (정지 페이지)" % (url, url))
    if metrics_on:
        print("  Prometheus 메트릭: %s/metrics%s"
              % (url, " (admin 키 헤더 필요)" if user_view_on else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        httpd.shutdown()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def clear_screen():
    sys.stdout.write("\033[2J\033[H")


def run_once(settings, as_json, demo):
    snap = (demo_snapshot() if demo
            else build_snapshot(settings, with_health=settings.get("health", True)))
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        print(render(snap, settings if not demo else dict(settings, probe_backends=True)))
    return snap


def main():
    p = argparse.ArgumentParser(
        description="LiteLLM/KServe/vLLM-SGLang 모델 현황 모니터 (v%s)" % __version__)
    p.add_argument("--version", action="version",
                   version="model_monitor %s" % __version__)
    p.add_argument("--litellm-url", help="LiteLLM 게이트웨이 URL (예: http://litellm:4000)")
    p.add_argument("--api-key", help="LiteLLM API key (admin 권한 권장)")
    p.add_argument("--config", help="설정 파일 (.json, 또는 PyYAML 있으면 .yaml)")
    p.add_argument("--probe-backends", action="store_true",
                   help="설정의 backends 를 직접 /v1/models probe")
    p.add_argument("--watch", action="store_true", help="실시간 갱신 모드(터미널)")
    p.add_argument("--interval", type=float, default=5.0, help="watch/웹 갱신 주기(초)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--timeout", type=float, default=10.0, help="HTTP 타임아웃(초)")
    p.add_argument("--health-timeout", type=float,
                   help="LiteLLM /health 타임아웃(초, 기본 90 — 모델 많으면 늘리기)")
    p.add_argument("--no-health", action="store_true",
                   help="LiteLLM /health 호출 안 함 (status 는 k8s backend readiness 로 판정)")
    p.add_argument("--demo", action="store_true", help="샘플 데이터로 미리보기")
    # 웹 UI
    p.add_argument("--serve", action="store_true",
                   help="웹 대시보드 모드 (브라우저로 조회)")
    p.add_argument("--host", default="0.0.0.0", help="웹 서버 bind host")
    p.add_argument("--port", type=int, default=8088, help="웹 서버 포트")
    p.add_argument("--no-metrics", action="store_true",
                   help="Prometheus /metrics 엔드포인트 비활성(기본 ON, --serve 시)")
    # per-user(키별) 뷰 — 기본 OFF (Go/No-Go 게이트 + TLS 확인 후 켤 것)
    p.add_argument("--enable-user-view", action="store_true",
                   help="키 입력 per-user 뷰 활성(POST /api/snapshot/user). "
                        "전제: /v1/models 키별 필터 동작 확인 + TLS 종단")
    p.add_argument("--user-view-show-internal", action="store_true",
                   help="per-user 뷰에서 내부 api_base/namespace 도 표시(기본 숨김)")
    # backend 개수(LB 뒤 Pod 수) 수집
    p.add_argument("--no-gpu-info", action="store_true",
                   help="GPU 개수/장치명 수집 끄기 (기본 ON; Pod/Node 읽기 권한 필요)")
    p.add_argument("--no-backend-count", action="store_true",
                   help="LB 뒤 backend Pod 개수 수집 비활성")
    p.add_argument("--k8s-api-server", help="k8s API server URL 오버라이드")
    p.add_argument("--k8s-token-file", help="ServiceAccount 토큰 파일 경로")
    p.add_argument("--k8s-ca-file", help="ServiceAccount CA 파일 경로")
    p.add_argument("--k8s-insecure", action="store_true",
                   help="k8s API TLS 검증 비활성")
    p.add_argument("--k8s-timeout", type=float, help="k8s API 호출 타임아웃(초)")
    args = p.parse_args()

    settings = resolve_settings(args)

    if (not args.demo and not settings["litellm_url"]
            and not settings["backends"]):
        p.error("--litellm-url 또는 --config 가 필요합니다 (또는 --demo).")

    if args.serve:
        serve_dashboard(settings, args.host, args.port, args.interval, args.demo)
        return

    if args.watch:
        try:
            while True:
                clear_screen()
                run_once(settings, args.json, args.demo)
                sys.stdout.write(
                    c("\n(%.0fs 마다 갱신 — Ctrl+C 종료)\n" % args.interval, "dim"))
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return
    else:
        run_once(settings, args.json, args.demo)


if __name__ == "__main__":
    main()

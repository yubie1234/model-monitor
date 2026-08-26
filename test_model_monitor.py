#!/usr/bin/env python3
"""model-monitor 단위 테스트 — 핵심 수집/집계 로직(순수 함수)만 검증.

실행:  python3 -m unittest -v        (또는)  python3 test_model_monitor.py

FastAPI 구조로 재편한 뒤에도 테스트 본문은 그대로 두기 위해, 새 모듈들을
m.* 네임스페이스로 모아 노출한다. (web/route 계층은 별도이고 FastAPI 가 필요해
여기서는 import 하지 않는다 — 핵심 로직은 stdlib 만으로 검증 가능.)
"""

import asyncio
import collections
import copy
import importlib
import json
import os
import re
import tempfile
import time
import types
import unittest
from unittest import mock

import app
from app import auth as _auth
from app.core import k8s as _k8s
from app.services import backend_count as _bc
from app.services import demo as _demo
from app.services import gpu as _gpu
from app.services import litellm as _ll
from app.services import prometheus as _prom
from app.services import snapshot as _snap
from app.services import state as _state
from app.services import user_access as _ua

# config.build_collector_settings 는 순수 dict 병합 함수지만, app.config 모듈을
# import 하려면 pydantic(-settings) 이 있어야 한다. 웹 스택이 없는 최소 환경에서도
# 나머지 테스트는 돌아가도록 import 를 보호하고, 없으면 해당 클래스만 skip 한다.
try:
    from app.config import build_collector_settings as _build_cs
    _HAS_PYDANTIC = True
except Exception:  # pragma: no cover - web 스택 미설치 환경
    _build_cs = None
    _HAS_PYDANTIC = False

m = types.SimpleNamespace(
    parse_api_base=_bc.parse_api_base,
    resolve_backend_count=_bc.resolve_backend_count,
    _is_serverless=_bc._is_serverless,
    _strip_openai_suffix=_ll._strip_openai_suffix,
    _classify_backend=_ll._classify_backend,
    _classify_engine=_ll._classify_engine,
    merge_deployments_with_health=_snap.merge_deployments_with_health,
    summarize=_snap.summarize,
    demo_snapshot=_demo.demo_snapshot,
    K8sClient=_k8s.K8sClient,
    _short_gpu_product=_gpu._short_gpu_product,
    _pod_gpu=_gpu._pod_gpu,
    _pod_ready=_gpu._pod_ready,
    _pod_engine=_gpu._pod_engine,
    collect_user_access=_ua.collect_user_access,
    AccessCache=_ua.AccessCache,
    filter_snapshot_for_user=_ua.filter_snapshot_for_user,
    _backend_ref=_ua._backend_ref,
    _redact_deployment_for_user=_ua._redact_deployment_for_user,
    render_prometheus_metrics=_prom.render_prometheus_metrics,
    is_admin_key=_auth.is_admin_key,
    request_key=_auth.request_key,
    admin_ok=_auth.admin_ok,
    bearer_token=_auth.bearer_token,
    metrics_ok=_auth.metrics_ok,
    SnapshotStore=_state.SnapshotStore,
    Refresher=_state.Refresher,
    build_collector_settings=_build_cs,
    _deployment_health_safe=_ll._deployment_health_safe,
    select_health_check_models=_ll.select_health_check_models,
    health_check_allowed_bases=_ll.health_check_allowed_bases,
    detect_mode_and_revision=_bc.detect_mode_and_revision,
    count_desired_via_selector=_bc.count_desired_via_selector,
    service_pod_selector=_gpu.service_pod_selector,
    collect_gpu_for_service=_gpu.collect_gpu_for_service,
    selector_key=_gpu.selector_key,
    fetch_health=_ll.fetch_health,
    fetch_health_for_model=_ll.fetch_health_for_model,
    aggregate_selective_health=_ll.aggregate_selective_health,
    collect_litellm=_ll.collect_litellm,
    discover_backends=_ll.discover_backends,
)


class FakeClient:
    """K8sClient 흉내 — 경로 substring 으로 미리 정한 응답을 돌려주고 호출을 기록."""

    def __init__(self, routes, default_namespace="default"):
        self.routes = routes            # [(substr, (ok, data, err)), ...]
        self.default_namespace = default_namespace
        self.enabled = True
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        for substr, resp in self.routes:
            if substr in path:
                return resp
        return (False, None, "HTTP 404 Not Found")


SETTINGS = {"namespace_overrides": {}, "activator_namespace": "knative-serving"}


class TestParseApiBase(unittest.TestCase):
    def test_fqdn_svc_cluster_local(self):
        p = m.parse_api_base("http://qwen36-35b-predictor.kserve.svc.cluster.local:8080/v1")
        self.assertEqual(p["kind"], "k8s-svc")
        self.assertEqual(p["service"], "qwen36-35b-predictor")
        self.assertEqual(p["namespace"], "kserve")
        self.assertEqual(p["port"], 8080)

    def test_shortform_svc_ns(self):
        p = m.parse_api_base("http://vllm-qwen3.kind:18080/v1")
        self.assertEqual(p["service"], "vllm-qwen3")
        self.assertEqual(p["namespace"], "kind")

    def test_shortform_single_label_uses_default_ns(self):
        p = m.parse_api_base("http://litellm/v1", default_namespace="defns")
        self.assertEqual(p["service"], "litellm")
        self.assertEqual(p["namespace"], "defns")

    def test_ip_is_external(self):
        p = m.parse_api_base("http://50.50.65.54:8000/v1")
        self.assertEqual(p["kind"], "external")
        self.assertIsNone(p["service"])

    def test_public_domain_is_external(self):
        p = m.parse_api_base("https://api.example.com/v1")
        self.assertEqual(p["kind"], "external")

    def test_service_name_override(self):
        p = m.parse_api_base("http://vllm-qwen3.kind:18080/v1",
                             overrides={"vllm-qwen3": "prod"})
        self.assertEqual(p["service"], "vllm-qwen3")
        self.assertEqual(p["namespace"], "prod")

    def test_trailing_dot_fqdn(self):
        p = m.parse_api_base("http://svc1.nsx.svc.cluster.local./v1")
        self.assertEqual(p["service"], "svc1")
        self.assertEqual(p["namespace"], "nsx")


class TestHelpers(unittest.TestCase):
    def test_strip_openai_suffix(self):
        self.assertEqual(m._strip_openai_suffix("http://a/v1"), "http://a")
        self.assertEqual(m._strip_openai_suffix("http://a/openai/v1"), "http://a")
        self.assertEqual(m._strip_openai_suffix("http://a/"), "http://a")
        self.assertEqual(m._strip_openai_suffix("http://a"), "http://a")

    def test_classify_backend(self):
        self.assertEqual(m._classify_backend("SGlang-X", "", ""), "sglang")
        self.assertEqual(m._classify_backend("KServe-X", "", ""), "kserve")
        self.assertEqual(m._classify_backend("X", "hosted_vllm/Y", ""), "vllm")
        self.assertEqual(m._classify_backend("plain", "openai/Z", ""), "-")

    def test_classify_engine_ignores_infra_keywords(self):
        # 엔진 분류는 인프라 키워드(kserve)를 보지 않는다 — 레거시 type 에서
        # 'KServe-' 접두사가 엔진 정보를 가리던 문제가 재발하지 않게 고정.
        self.assertEqual(m._classify_engine("KServe-gemma", "sglang-gemma", ""),
                         "sglang")
        self.assertEqual(m._classify_engine("KServe-X", "", ""), "-")
        self.assertEqual(m._classify_engine("X", "hosted_vllm/Y", ""), "vllm")
        self.assertEqual(m._classify_engine("plain", "openai/Z", ""), "-")

    def test_is_serverless(self):
        self.assertTrue(m._is_serverless("RawDeployment", "rev-001"))   # revision 있으면 True
        self.assertTrue(m._is_serverless("Serverless", None))
        self.assertFalse(m._is_serverless("RawDeployment", None))


class TestMergeWithHealth(unittest.TestCase):
    def test_health_and_k8s_fallback(self):
        ll = {
            "health": {
                "healthy_endpoints": [{"api_base": "http://a/v1"}],
                "unhealthy_endpoints": [{"api_base": "http://b/v1"}],
            },
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1"},          # health UP
                {"model_name": "B", "api_base": "http://b/v1"},          # health DOWN
                {"model_name": "C", "api_base": "http://c/v1",           # k8s 보정 UP
                 "backends_ready": 2, "backend_source": "deployment"},
                {"model_name": "D", "api_base": "http://d/v1",           # scale-to-zero -> ?
                 "backends_ready": 0, "scale_to_zero": True,
                 "backend_source": "knative-pa"},
                {"model_name": "E", "api_base": "http://e/v1"},          # 정보 없음 -> ?
            ],
        }
        merged = {d["model_name"]: d for d in m.merge_deployments_with_health(ll)}
        self.assertEqual(merged["A"]["status"], "UP")
        self.assertEqual(merged["B"]["status"], "DOWN")
        self.assertEqual(merged["C"]["status"], "UP")
        self.assertEqual(merged["D"]["status"], "?")
        self.assertEqual(merged["E"]["status"], "?")

    def test_down_reason_from_health_error(self):
        # /health unhealthy endpoint 의 error 를 소수 카테고리로 정규화하고
        # 첫 줄만 status_detail 로 실어야 한다(스택트레이스 제거).
        ll = {
            "health": {
                "healthy_endpoints": [],
                "unhealthy_endpoints": [
                    {"api_base": "http://b/v1", "exception_status": "500",
                     "error": "litellm.InternalServerError: OpenAIException - "
                              "Connection error.\nstack trace: Traceback ..."},
                ],
            },
            "deployments": [{"model_name": "B", "api_base": "http://b/v1"}],
        }
        b = m.merge_deployments_with_health(ll)[0]
        self.assertEqual(b["status"], "DOWN")
        self.assertEqual(b["down_reason"], "connection")
        # 첫 줄만 남고 stack trace 는 잘려야 한다.
        self.assertIn("Connection error", b["status_detail"])
        self.assertNotIn("Traceback", b["status_detail"])

    def test_down_reason_http_code_fallback(self):
        # 키워드로 못 잡으면 exception_status(HTTP 코드)로 버킷팅.
        ll = {
            "health": {"healthy_endpoints": [], "unhealthy_endpoints": [
                {"api_base": "http://b/v1", "exception_status": 503,
                 "error": "upstream returned bad gateway"}]},
            "deployments": [{"model_name": "B", "api_base": "http://b/v1"}],
        }
        b = m.merge_deployments_with_health(ll)[0]
        self.assertEqual(b["down_reason"], "server_error")

    def test_down_reason_detail_length_cap(self):
        # status_detail 은 200자 캡(...) — 폭주하는 error 문자열 방어.
        ll = {
            "health": {"healthy_endpoints": [], "unhealthy_endpoints": [
                {"api_base": "http://b/v1", "error": "x" * 500}]},
            "deployments": [{"model_name": "B", "api_base": "http://b/v1"}],
        }
        b = m.merge_deployments_with_health(ll)[0]
        self.assertLessEqual(len(b["status_detail"]), 200)
        self.assertTrue(b["status_detail"].endswith("..."))

    def test_down_reason_no_ready_pods_via_k8s(self):
        # /health 없이 k8s readiness 로 DOWN(ready 0, scale-to-zero 아님)이면
        # 사유는 no_ready_pods 로 표면화.
        ll = {"health": None, "deployments": [
            {"model_name": "C", "api_base": "http://c/v1",
             "backends_ready": 0, "backend_source": "deployment"}]}
        c = m.merge_deployments_with_health(ll)[0]
        self.assertEqual(c["status"], "DOWN")
        self.assertEqual(c["down_reason"], "no_ready_pods")

    def test_up_and_idle_have_no_down_reason(self):
        # UP / '?'(idle·미상) 행에는 사유 필드를 붙이지 않는다.
        ll = {
            "health": {"healthy_endpoints": [{"api_base": "http://a/v1"}],
                       "unhealthy_endpoints": []},
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1"},
                {"model_name": "D", "api_base": "http://d/v1",
                 "backends_ready": 0, "scale_to_zero": True,
                 "backend_source": "knative-pa"},
            ],
        }
        merged = {d["model_name"]: d for d in m.merge_deployments_with_health(ll)}
        self.assertNotIn("down_reason", merged["A"])
        self.assertNotIn("status_detail", merged["A"])
        self.assertNotIn("down_reason", merged["D"])


def _safe_service(**over):
    """안전 판정을 통과하는 일반 Service deployment 최소 dict (테스트 헬퍼)."""
    d = {"model_name": "a", "network_type": "service", "mode": "Unknown",
         "backend_source": "endpointslice", "backends_ready": 2}
    d.update(over)
    return d


class TestSelectHealthCheckModels(unittest.TestCase):
    """선택적 health check 대상 선별 — fail-safe 가 생명.

    잘못 포함하면 scale-to-zero(Knative Serverless) 백엔드를 깨우므로,
    '안전이 양성으로 확인된 것만' 통과해야 한다. 여기 케이스들이 그 계약."""

    def test_raw_deployment_kserve_included(self):
        # KServe 라도 RawDeployment(activator 없음)로 확인되면 체크 대상
        d = {"model_name": "a", "network_type": "kserve",
             "mode": "RawDeployment", "backend_source": "deployment",
             "backends_ready": 1}
        self.assertTrue(m._deployment_health_safe(d))

    def test_raw_deployment_mode_case_insensitive(self):
        # mode 는 클러스터 값을 그대로 echo — 대소문자가 달라도 안전 판정 유지
        d = {"model_name": "a", "network_type": "kserve",
             "mode": "rawDeployment"}
        self.assertTrue(m._deployment_health_safe(d))

    def test_plain_service_with_count_evidence_included(self):
        self.assertTrue(m._deployment_health_safe(_safe_service()))
        self.assertTrue(m._deployment_health_safe(
            _safe_service(backend_source="endpoints")))
        self.assertTrue(m._deployment_health_safe(
            _safe_service(backend_source="deployment")))

    def test_non_predictor_service_included_without_count_evidence(self):
        # 운영 스펙: KServe 판별은 이름 규약(-predictor). 이름이 아니면 카운트
        # 증거가 없어도(비 KServe 확정) LiteLLM 으로 체크한다.
        self.assertTrue(m._deployment_health_safe(
            {"model_name": "a", "network_type": "service", "service": "plain-svc",
             "mode": "Unknown", "backend_source": "none"}))
        self.assertTrue(m._deployment_health_safe(
            _safe_service(backends_ready=None)))

    def test_predictor_name_is_kserve_excluded_unless_raw(self):
        # 운영 규약: KServe svc 는 항상 -predictor. 이름이 걸리면 KServe 로
        # 간주 — RawDeployment 로 k8s 가 양성 확인한 경우만 체크(그 외 제외).
        base = {"model_name": "a", "network_type": "service",
                "service": "foo-predictor", "backend_source": "endpointslice",
                "backends_ready": 2}
        self.assertFalse(m._deployment_health_safe(dict(base, mode="Unknown")))
        self.assertTrue(m._deployment_health_safe(
            dict(base, mode="RawDeployment")))

    def test_predictor_name_from_api_base_without_k8s(self):
        # k8s 조회가 전혀 안 돼도(service/network_type 필드 없음) api_base
        # 호스트 첫 라벨의 이름 규약으로 KServe 를 걸러낸다.
        d = {"model_name": "a",
             "api_base": "http://qwen-predictor.default.svc.cluster.local/v1"}
        self.assertFalse(m._deployment_health_safe(d))
        d2 = {"model_name": "a", "api_base": "http://plain-vllm.kind:18080/v1"}
        self.assertTrue(m._deployment_health_safe(d2))

    def test_misnamed_isvc_guess_404_still_excluded_by_name(self):
        # 실운영 회귀: predictor 이름인데 ISVC 이름 추측이 404 → 'service' 로
        # 분류되던 KServe(Serverless 가능) — 이름 규약이 잡아서 제외해야 한다.
        d = {"model_name": "KServe-x", "network_type": "service",
             "service": "qwen36-27b-fp8-predictor",
             "backend_source": "endpointslice", "backends_ready": 0,
             "mode": "Unknown"}
        self.assertFalse(m._deployment_health_safe(d))

    def test_serverless_mode_excluded(self):
        # Serverless = ping 이 activator 를 깨움/scale-down 저지 → 절대 제외
        d = {"model_name": "a", "network_type": "kserve", "mode": "Serverless"}
        self.assertFalse(m._deployment_health_safe(d))

    def test_serverless_flag_excluded_even_with_marker(self):
        # 회귀: mode 가 Unknown 이어도 revision 기반 Knative 판정(serverless
        # 필드)이 있으면 마커 true 로도 못 뒤집는다.
        d = _safe_service(serverless=True, active_health_check=True)
        self.assertFalse(m._deployment_health_safe(d))

    def test_activator_only_excluded_even_with_marker(self):
        # 회귀: EndpointSlice 가 activator-only(=scale-to-zero 증거)면 제외.
        d = _safe_service(activator_only=True, active_health_check=True)
        self.assertFalse(m._deployment_health_safe(d))

    def test_scale_to_zero_excluded(self):
        self.assertFalse(m._deployment_health_safe(
            _safe_service(scale_to_zero=True)))

    def test_knative_sources_excluded(self):
        # 회귀: knative-pa 뿐 아니라 knative-revision 도 Knative 경유 = 위험.
        for src in ("knative-pa", "knative-revision"):
            d = {"model_name": "a", "network_type": "kserve",
                 "mode": "RawDeployment", "backend_source": src,
                 "backends_ready": 1, "active_health_check": True}
            self.assertFalse(m._deployment_health_safe(d), src)

    def test_external_and_undetermined_included_when_not_kserve(self):
        # 운영 스펙 회귀(GLM-5.2-FP8): api_base 가 노드 IP(NodePort)라 external
        # 로 분류되던 비 KServe backend — 이름 규약에 안 걸리므로 체크 대상.
        # ping 은 LiteLLM 이 대신하므로 모니터가 직접 닿는 게 아니다.
        self.assertTrue(m._deployment_health_safe(
            {"model_name": "GLM-5.2-FP8",
             "api_base": "http://50.50.65.49:30000/v1",
             "network_type": "external", "backend_source": "external"}))
        # 판정불가('-')도 이름이 비 KServe 면 체크
        self.assertTrue(m._deployment_health_safe(
            {"model_name": "a", "network_type": "-", "service": "plain"}))

    def test_kserve_unknown_mode_excluded(self):
        # KServe 인데 mode 판정 실패(Unknown) → Serverless 일 수 있으니 제외
        d = {"model_name": "a", "network_type": "kserve", "mode": "Unknown"}
        self.assertFalse(m._deployment_health_safe(d))

    def test_marker_false_always_excluded(self):
        self.assertFalse(m._deployment_health_safe(
            _safe_service(active_health_check=False)))

    def test_marker_true_rescues_undetermined(self):
        # override true → 판정불가/external 도 체크 허용
        self.assertTrue(m._deployment_health_safe(
            {"model_name": "a", "network_type": "-",
             "active_health_check": True}))
        self.assertTrue(m._deployment_health_safe(
            {"model_name": "a", "network_type": "external",
             "active_health_check": True}))

    def test_marker_true_cannot_override_positive_danger(self):
        # 양성 위험(Serverless/scale-to-zero)은 마커로도 못 뒤집는다
        self.assertFalse(m._deployment_health_safe(
            {"model_name": "a", "mode": "Serverless",
             "active_health_check": True}))
        self.assertFalse(m._deployment_health_safe(
            _safe_service(scale_to_zero=True, active_health_check=True)))

    def test_mixed_name_excluded_entirely(self):
        # /health?model=<name> 은 그 이름의 모든 deployment 를 ping 하므로,
        # 같은 이름에 안전+위험 백엔드가 섞이면 이름 전체를 제외해야 한다.
        deps = [
            {"model_name": "mixed", "network_type": "kserve",
             "mode": "RawDeployment"},
            {"model_name": "mixed", "network_type": "kserve",
             "mode": "Serverless"},
            _safe_service(model_name="safe"),
        ]
        self.assertEqual(m.select_health_check_models(deps), ["safe"])

    def test_shared_underlying_with_unsafe_sibling_excluded(self):
        # 실운영 회귀: LiteLLM /health?model= 은 model_name 보다 넓게 매칭될 수
        # 있다(같은 underlying 의 타 모델 predictor endpoint 가 응답에 포함됨).
        # 안전한 이름이라도 위험 sibling 과 underlying 을 공유하면 그 ping 이
        # sibling(Serverless)을 깨울 수 있으므로 제외해야 한다.
        deps = [
            _safe_service(model_name="plain", underlying="hosted_vllm/m1",
                          api_base="http://plain.kind/v1", service="plain"),
            {"model_name": "kserve-sib", "underlying": "hosted_vllm/m1",
             "api_base": "http://m1-predictor.default.svc/v1",
             "service": "m1-predictor", "mode": "Serverless"},
            _safe_service(model_name="indep", underlying="hosted_vllm/m2",
                          api_base="http://indep.kind/v1", service="indep"),
        ]
        self.assertEqual(m.select_health_check_models(deps), ["indep"])

    def test_underlying_provider_prefix_normalized(self):
        # 실데이터 회귀: 같은 모델인데 provider 접두사 유무가 섞임
        # ("openai/Qwen3-Next-..." vs "Qwen3-Next-...") — 접두사를 떼고
        # 비교해야 교차 ping 이 막힌다.
        deps = [
            _safe_service(model_name="plain", underlying="openai/m1",
                          api_base="http://plain.kind/v1", service="plain"),
            {"model_name": "kserve-sib", "underlying": "m1",
             "api_base": "http://m1-predictor.default.svc/v1",
             "service": "m1-predictor", "mode": "Serverless"},
        ]
        self.assertEqual(m.select_health_check_models(deps), [])

    def test_placeholder_name_skipped(self):
        # 회귀: model_name 없는 항목은 "?" 플레이스홀더가 되는데, 이를 체크하면
        # /health?model=%3F 무의미 조회가 매 주기 나간다 — 제외해야 한다.
        deps = [_safe_service(model_name="?"), _safe_service(model_name="ok")]
        self.assertEqual(m.select_health_check_models(deps), ["ok"])

    def test_fully_blocked_name_skipped(self):
        # 전부 일시중지 = 라우팅 대상 0 -> ping 할 이유가 없다.
        # (LiteLLM /health 는 blocked 를 안 걸러주므로 모니터가 걸러야 한다)
        deps = [_safe_service(model_name="paused", blocked=True),
                _safe_service(model_name="paused", api_base="http://p2/v1",
                              blocked=True)]
        self.assertEqual(m.select_health_check_models(deps), [])

    def test_partially_blocked_name_still_checked(self):
        # 남은 sibling 이 실제 트래픽을 받으므로 상태를 봐야 한다.
        # /health?model= 은 이름 단위라 살아있는 쪽만 골라 ping 할 수단이 없고,
        # blocked 는 '깨우기 위험' 신호가 아니라 이름 전체를 버리지 않는다.
        deps = [_safe_service(model_name="mixed", blocked=True),
                _safe_service(model_name="mixed", api_base="http://m2/v1",
                              blocked=False)]
        self.assertEqual(m.select_health_check_models(deps), ["mixed"])

    def test_blocked_does_not_poison_sibling_names(self):
        # blocked 는 unsafe_underlying/unsafe_base 오염원이 아니다 — 같은
        # underlying 을 쓰는 다른 이름까지 체크가 끊기면 안 된다.
        deps = [_safe_service(model_name="paused", underlying="openai/X",
                              blocked=True),
                _safe_service(model_name="live", api_base="http://l/v1",
                              underlying="openai/X")]
        self.assertEqual(m.select_health_check_models(deps), ["live"])

    def test_allowed_bases_include_paused_safe_backend(self):
        # 회귀: 일시중지라 조회 대상에서 빠진 backend 의 base 가 합집합에서도
        # 빠지면, LiteLLM 의 넓은 ?model= 매칭으로 그 endpoint 가 sibling 응답에
        # 섞여 올 때마다 "Serverless 가 ping 됐다" 는 오경보가 영구히 뜬다.
        # 합집합이 답하는 질문은 '조회 대상인가' 가 아니라 'ping 돼도 되는가'.
        deps = [_safe_service(model_name="paused", underlying="openai/Q",
                              api_base="http://p/v1", blocked=True),
                _safe_service(model_name="live", underlying="Q",
                              api_base="http://l/v1")]
        names = m.select_health_check_models(deps)
        allowed = m.health_check_allowed_bases(deps, names)
        union = {b for s in allowed.values() for b in s}
        self.assertEqual(names, ["live"])          # 조회는 살아있는 것만
        self.assertIn("http://p", union)           # 하지만 ping 돼도 경보는 안 냄
        self.assertIn("http://l", union)

    def test_allowed_bases_exclude_paused_serverless(self):
        # 반대로 일시중지 + Serverless 는 합집합에 넣으면 안 된다 — 그 endpoint 가
        # 응답에 나타났다는 건 실제로 깨어났다는 뜻이라 경보가 맞다.
        deps = [_safe_service(model_name="paused", api_base="http://s/v1",
                              blocked=True, scale_to_zero=True,
                              backend_source="knative-pa", mode="Serverless"),
                _safe_service(model_name="live", api_base="http://l/v1")]
        names = m.select_health_check_models(deps)
        union = {b for s in m.health_check_allowed_bases(deps, names).values()
                 for b in s}
        self.assertNotIn("http://s", union)
        self.assertIn("http://l", union)

    def test_dedup_and_sort(self):
        deps = [
            _safe_service(model_name="b"),
            _safe_service(model_name="b"),
            {"model_name": "a", "network_type": "kserve",
             "mode": "RawDeployment"},
        ]
        self.assertEqual(m.select_health_check_models(deps), ["a", "b"])


class TestFetchHealthForModel(unittest.TestCase):
    """/health?model= 1회 조회 — URL 인코딩과 인자 전달만 책임진다."""

    def test_url_encodes_model_name(self):
        calls = []
        def fake(url, key=None, timeout=10):
            calls.append(url)
            return True, {"healthy_endpoints": [], "unhealthy_endpoints": []}, None
        orig = _ll.http_get_json
        _ll.http_get_json = fake
        self.addCleanup(lambda: setattr(_ll, "http_get_json", orig))
        ok, data, err = m.fetch_health_for_model("http://llm/", "sk", "a b/c", 10)
        self.assertTrue(ok)
        self.assertEqual(calls, ["http://llm/health?model=a%20b%2Fc"])


class TestHealth503Payload(unittest.TestCase):
    """LiteLLM /health 는 unhealthy 백엔드가 있으면 HTTP 200 이 아니라 **503 에
    동일한 health payload** 를 실어 보낸다 — 상태코드만 보고 본문을 버리면
    정작 DOWN 백엔드의 상태 정보를 매번 '조회 실패'로 잃는다(실운영 회귀)."""

    _BODY = {"healthy_endpoints": [],
             "unhealthy_endpoints": [{"model": "m/x", "api_base": "http://x/v1"}]}

    def _patch_urlopen(self, code=503, body=None, raw=None):
        import io
        import urllib.error
        import urllib.request
        payload = raw if raw is not None else json.dumps(body).encode()
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, code, "Service Unavailable", None,
                io.BytesIO(payload))
        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", orig))

    def test_http_get_json_parses_json_error_body(self):
        from app.core.http import http_get_json
        self._patch_urlopen(body=self._BODY)
        ok, data, err = http_get_json("http://llm/health")
        self.assertFalse(ok)                 # ok 계약은 그대로(호출자 호환)
        self.assertEqual(data, self._BODY)   # 본문은 버리지 않고 파싱해 전달
        self.assertIn("HTTP 503", err)

    def test_http_get_json_non_json_error_body(self):
        from app.core.http import http_get_json
        self._patch_urlopen(raw=b"<html>ingress oops</html>")
        ok, data, err = http_get_json("http://llm/health")
        self.assertFalse(ok)
        self.assertIsNone(data)              # 프록시 HTML 등은 그대로 실패

    def test_fetch_health_accepts_503_payload(self):
        # 전량 /health: 모든 백엔드가 unhealthy 인 가장 중요한 순간에 LiteLLM 이
        # 503 을 주는데, 이걸 None 처리하면 health 가 영원히 갱신 안 된다.
        self._patch_urlopen(body=self._BODY)
        self.assertEqual(m.fetch_health("http://llm", "sk", 5), self._BODY)

    def test_fetch_health_for_model_normalizes_503(self):
        self._patch_urlopen(body=self._BODY)
        ok, data, err = m.fetch_health_for_model("http://llm", "sk", "x", 5)
        self.assertTrue(ok)
        self.assertEqual(data, self._BODY)
        self.assertIsNone(err)

    def test_health_shaped_only_other_json_stays_failure(self):
        # JSON 이어도 health 모양이 아니면(진짜 에러 응답) 실패 유지
        self._patch_urlopen(raw=b'{"error": "boom"}')
        ok, data, err = m.fetch_health_for_model("http://llm", "sk", "x", 5)
        self.assertFalse(ok)
        self.assertIn("HTTP 503", err)
        self.assertIsNone(m.fetch_health("http://llm", "sk", 5))

    def test_end_to_end_503_becomes_down_status(self):
        # 503 로 온 unhealthy 가 aggregate → merge 를 거쳐 DOWN 으로 반영
        self._patch_urlopen(body=self._BODY)
        ok, data, err = m.fetch_health_for_model("http://llm", "sk", "x", 5)
        h = m.aggregate_selective_health([("x", ok, data, err)])
        self.assertEqual(h["unhealthy_count"], 1)
        self.assertEqual(h["errors"], [])    # 더는 에러로 기록되지 않는다
        merged = m.merge_deployments_with_health({
            "health": h,
            "deployments": [{"model_name": "x", "api_base": "http://x/v1"}]})
        self.assertEqual(merged[0]["status"], "DOWN")
        self.assertEqual(merged[0]["status_source"], "health")


class TestAggregateSelectiveHealth(unittest.TestCase):
    """/health?model= 응답 집계 — 기존 /health 모양과 호환이어야 merge 재사용."""

    def test_aggregates_and_merge_compat(self):
        results = [
            ("a", True, {"healthy_endpoints": [
                {"model": "m/a", "api_base": "http://a/v1"}],
                "unhealthy_endpoints": []}, None),
            ("b", True, {"healthy_endpoints": [],
                         "unhealthy_endpoints": [
                             {"model": "m/b", "api_base": "http://b/v1"}]}, None),
        ]
        h = m.aggregate_selective_health(results)
        self.assertEqual(h["healthy_count"], 1)
        self.assertEqual(h["unhealthy_count"], 1)
        self.assertTrue(h["selective"])
        self.assertEqual(h["checked_models"], ["a", "b"])
        # merge_deployments_with_health 에 그대로 주입 가능해야 한다
        merged = m.merge_deployments_with_health({
            "health": h,
            "deployments": [
                {"model_name": "a", "api_base": "http://a/v1"},
                {"model_name": "b", "api_base": "http://b/v1"},
                {"model_name": "skipped", "api_base": "http://c/v1"},
            ]})
        st = {d["model_name"]: d["status"] for d in merged}
        self.assertEqual(st, {"a": "UP", "b": "DOWN", "skipped": "?"})

    def test_one_failure_does_not_block_others(self):
        results = [
            ("bad", False, None, "HTTP 500 boom"),
            ("ok", True, {"healthy_endpoints": [
                {"model": "m/ok", "api_base": "http://ok/v1"}],
                "unhealthy_endpoints": []}, None),
        ]
        h = m.aggregate_selective_health(results)
        self.assertEqual(h["healthy_count"], 1)
        self.assertEqual(len(h["errors"]), 1)
        self.assertIn("health?model=bad", h["errors"][0])

    def test_all_failed_returns_none_keeps_last_good(self):
        # 회귀: 전 모델 조회 실패 라운드가 빈 dict 를 반환하면 직전 정상 health
        # 를 덮어써 DOWN 이던 모델이 k8s 폴백으로 UP 으로 뒤집힌다 — None 을
        # 반환해 주입을 생략(fetch_health 의 실패 시 None 과 동일 계약)해야 한다.
        results = [("a", False, None, "connection error"),
                   ("b", False, None, "HTTP 502")]
        self.assertIsNone(m.aggregate_selective_health(results))

    def test_empty_results_returns_empty_dict(self):
        # 체크 대상이 없던 라운드(전부 Serverless 등)는 실패가 아니라 "아무것도
        # 체크 안 함" — 빈 집계를 반환해 전부 k8s 폴백으로 정직하게 흐른다.
        h = m.aggregate_selective_health([])
        self.assertIsNotNone(h)
        self.assertEqual(h["healthy_count"], 0)
        self.assertEqual(h["checked_models"], [])

    def test_dedups_shared_backend_endpoints(self):
        ep = {"model": "m/shared", "api_base": "http://s/v1"}
        results = [
            ("a", True, {"healthy_endpoints": [dict(ep)],
                         "unhealthy_endpoints": []}, None),
            ("b", True, {"healthy_endpoints": [dict(ep)],
                         "unhealthy_endpoints": []}, None),
        ]
        h = m.aggregate_selective_health(results)
        self.assertEqual(h["healthy_count"], 1)

    def test_contradiction_down_wins(self):
        # 회귀: 같은 endpoint 가 한 응답에선 healthy, 다른 응답에선 unhealthy
        # (두 병렬 호출 사이 flap)면 — merge 가 healthy 를 먼저 보므로 —
        # healthy 쪽을 버려 DOWN 이 이겨야 한다(이중 집계도 금지).
        ep = {"model": "m/shared", "api_base": "http://s/v1"}
        results = [
            ("a", True, {"healthy_endpoints": [dict(ep)],
                         "unhealthy_endpoints": []}, None),
            ("b", True, {"healthy_endpoints": [],
                         "unhealthy_endpoints": [dict(ep)]}, None),
        ]
        h = m.aggregate_selective_health(results)
        self.assertEqual(h["healthy_count"], 0)
        self.assertEqual(h["unhealthy_count"], 1)
        merged = m.merge_deployments_with_health({
            "health": h,
            "deployments": [{"model_name": "a", "api_base": "http://s/v1"}]})
        self.assertEqual(merged[0]["status"], "DOWN")

    def test_foreign_endpoints_filtered_and_flagged(self):
        # 회귀: 체크 대상 어디에도 안 속하는 endpoint(=체크에서 제외한 backend
        # 가 ping 된 정황)는 버리고, 어떤 쿼리·어떤 base 인지 경고를 남긴다 —
        # 체크 제외 모델(Serverless)의 상태 오염을 막는다.
        results = [("a", True, {
            "healthy_endpoints": [
                {"model": "m/a", "api_base": "http://a/v1"},
                {"model": "m/other", "api_base": "http://serverless/v1"}],
            "unhealthy_endpoints": []}, None)]
        h = m.aggregate_selective_health(
            results, allowed_bases={"a": {"http://a"}})
        self.assertEqual(h["healthy_count"], 1)
        self.assertEqual(h["healthy_endpoints"][0]["api_base"], "http://a/v1")
        self.assertTrue(any("체크 대상 밖 endpoint" in e
                            and "health?model=a" in e
                            and "http://serverless" in e for e in h["errors"]))

    def test_cross_sibling_endpoint_accepted_via_union(self):
        # LiteLLM ?model= 매칭이 이름보다 넓어 sibling(다른 **체크** 모델)의
        # endpoint 가 섞여 올 수 있다 — 체크 대상 합집합 안이면 수용해야
        # 정보 손실·거짓 경고가 없다(merge 는 api_base 기준이라 정확한 행에 붙음).
        results = [("a", True, {
            "healthy_endpoints": [
                {"model": "m/a", "api_base": "http://a/v1"},
                {"model": "m/b", "api_base": "http://b/v1"}],   # 체크 모델 b 의 것
            "unhealthy_endpoints": []}, None)]
        h = m.aggregate_selective_health(
            results, allowed_bases={"a": {"http://a"}, "b": {"http://b"}})
        self.assertEqual(h["healthy_count"], 2)   # 둘 다 수용
        self.assertEqual(h["errors"], [])          # 거짓 경고 없음


class TestActiveHealthCheckMarker(unittest.TestCase):
    """model_info.active_health_check 파싱 — bool() 강제 변환 금지.

    회귀: YAML 에 "false"(따옴표 문자열)로 쓰는 흔한 실수가 bool("false")==True
    로 뒤집히면 운영자의 opt-out 이 opt-in 이 되어 제외하려던 모델을 ping 한다."""

    _SENTINEL = object()   # "키 자체가 없음" 표시

    def _collect(self, ahc_value):
        def fake(url, key=None, timeout=10):
            if "/model/info" in url:
                mi = {"id": "x"}
                if ahc_value is not self._SENTINEL:
                    mi["active_health_check"] = ahc_value
                return True, {"data": [{
                    "model_name": "mm",
                    "litellm_params": {"model": "m", "api_base": "http://a/v1"},
                    "model_info": mi}]}, None
            return False, None, "skip"
        orig = _ll.http_get_json
        _ll.http_get_json = fake
        try:
            r = m.collect_litellm("http://llm", "sk", 5, with_health=False)
        finally:
            _ll.http_get_json = orig
        return r["deployments"][0]

    def test_bool_passthrough(self):
        self.assertIs(self._collect(True).get("active_health_check"), True)
        self.assertIs(self._collect(False).get("active_health_check"), False)

    def test_string_false_is_not_true(self):
        self.assertIs(self._collect("false").get("active_health_check"), False)
        self.assertIs(self._collect("no").get("active_health_check"), False)
        self.assertIs(self._collect("True").get("active_health_check"), True)

    def test_unknown_values_ignored(self):
        # 인식 불가 값/타입은 마커 없음과 동일(fail-safe: opt-in 으로 안 둔갑)
        self.assertNotIn("active_health_check", self._collect("maybe"))
        self.assertNotIn("active_health_check", self._collect(1))
        self.assertNotIn("active_health_check", self._collect(self._SENTINEL))


class TestCollectLitellmModels(unittest.TestCase):
    """result['models'] 는 별도 /v1/models 호출 없이 deployments 에서 유도한다.

    회귀: 매 스냅샷 주기(기본 5s)마다 어떤 렌더러도 안 쓰는 /v1/models 를 다시
    호출해 LiteLLM 왕복을 낭비하면 안 된다(부하 절감). 모델명은 /model/info 의
    model_name(=/v1/models 의 id)에서 정렬·중복제거로 유도한다.
    """

    def test_models_derived_from_deployments_without_v1_models_call(self):
        calls = []

        def fake(url, key=None, timeout=10):
            calls.append(url)
            if "/model/info" in url:
                return True, {"data": [
                    {"model_name": "b-model",
                     "litellm_params": {"model": "m", "api_base": "http://b/v1"},
                     "model_info": {"id": "1"}},
                    {"model_name": "a-model",
                     "litellm_params": {"model": "m", "api_base": "http://a/v1"},
                     "model_info": {"id": "2"}},
                    {"model_name": "a-model",   # 중복 model_name → 1개로 축약
                     "litellm_params": {"model": "m", "api_base": "http://a2/v1"},
                     "model_info": {"id": "3"}},
                    {"model_name": "?",         # 미상 플레이스홀더 → 제외
                     "litellm_params": {"model": "m", "api_base": None},
                     "model_info": {"id": "4"}},
                ]}, None
            return False, None, "skip"

        orig = _ll.http_get_json
        _ll.http_get_json = fake
        try:
            r = m.collect_litellm("http://llm", "sk", 5, with_health=False)
        finally:
            _ll.http_get_json = orig

        self.assertEqual(r["models"], ["a-model", "b-model"])
        self.assertFalse(any(u.endswith("/v1/models") for u in calls))
        self.assertFalse(any("v1/models" in e for e in r["errors"]))


class TestDiscoverBackends(unittest.TestCase):
    """probe 자동발견 안전 필터 — 직접 probe 는 LiteLLM 을 안 거치고 백엔드에 바로
    닿아 scale-to-zero 를 깨우므로, 선택적 health check 와 같은 안전 판정으로
    위험 백엔드를 대상에서 제외해야 한다."""

    def test_unsafe_and_shared_bases_excluded(self):
        ll = {"deployments": [
            {"model_name": "safe", "api_base": "http://plain.ns.svc:8080/v1"},
            # 이름 규약(-predictor)인데 Raw 양성 확인 없음 → 보수적 제외
            {"model_name": "kserve-unconfirmed",
             "api_base": "http://foo-predictor.ns.svc/v1"},
            # 양성 위험(scale-to-zero) → 제외
            {"model_name": "s2z", "api_base": "http://bar.ns.svc/v1",
             "scale_to_zero": True},
            # 자체는 안전해 보여도 위험 deployment 와 같은 base 공유 → base 제외
            {"model_name": "sharing-safe", "api_base": "http://bar.ns.svc/v1"},
        ]}
        out = m.discover_backends(ll)
        self.assertEqual([b["url"] for b in out], ["http://plain.ns.svc:8080"])
        # 제외는 조용히 삼키지 않는다 — litellm.errors 에 1줄 요약. 개수는
        # deployment 가 아니라 base 단위(foo-predictor, bar 2개 — bar 공유 2행은 1개).
        self.assertTrue(any("probe" in e and "2개" in e for e in ll["errors"]))

    def test_raw_confirmed_kserve_included(self):
        ll = {"deployments": [
            {"model_name": "raw", "api_base": "http://r-predictor.ns.svc/v1",
             "mode": "RawDeployment"}]}
        out = m.discover_backends(ll)
        self.assertEqual([b["url"] for b in out], ["http://r-predictor.ns.svc"])
        self.assertFalse(ll.get("errors"))   # 제외 없음 → 경고 없음

    def test_health_only_endpoints_filtered_by_name_rule(self):
        # /health 에만 있는 주소는 k8s 판정이 없다 — KServe 이름 규약이면 Raw 확인이
        # 불가능하므로 제외, 일반 이름만 대상에 남긴다.
        ll = {"deployments": [],
              "health": {"healthy_endpoints": [
                  {"model": "m1", "api_base": "http://x-predictor.ns.svc/v1"},
                  {"model": "m2", "api_base": "http://plain2.ns.svc/v1"}]}}
        out = m.discover_backends(ll)
        self.assertEqual([b["url"] for b in out], ["http://plain2.ns.svc"])


class TestBlockedMarker(unittest.TestCase):
    """model_info.blocked 파싱 (LiteLLM v1.90.0+ 관리자 일시중지).

    active_health_check 와 같은 엄격 파싱을 쓴다 — bool() 강제 변환을 하면
    "false" 문자열이 True 가 되어 멀쩡히 서빙 중인 모델을 PAUSED 로 오표시한다.
    키가 아예 없는 경우(구버전 LiteLLM / config.yaml 전용 모델)는 '모름' 이라
    키를 만들지 않는다 — 없는 것을 '활성' 으로 단정하지 않기 위해서."""

    _SENTINEL = object()   # "키 자체가 없음" 표시

    def _collect(self, blocked_value):
        def fake(url, key=None, timeout=10):
            if "/model/info" in url:
                mi = {"id": "x"}
                if blocked_value is not self._SENTINEL:
                    mi["blocked"] = blocked_value
                return True, {"data": [{
                    "model_name": "mm",
                    "litellm_params": {"model": "m", "api_base": "http://a/v1"},
                    "model_info": mi}]}, None
            return False, None, "skip"
        orig = _ll.http_get_json
        _ll.http_get_json = fake
        try:
            r = m.collect_litellm("http://llm", "sk", 5, with_health=False)
        finally:
            _ll.http_get_json = orig
        return r["deployments"][0]

    def test_bool_passthrough(self):
        self.assertIs(self._collect(True).get("blocked"), True)
        self.assertIs(self._collect(False).get("blocked"), False)

    def test_string_forms_parsed_strictly(self):
        self.assertIs(self._collect("false").get("blocked"), False)
        self.assertIs(self._collect("True").get("blocked"), True)

    def test_absent_key_stays_absent(self):
        # 구버전 LiteLLM / config 전용 모델 -> '모름'. False 로 단정하면 안 된다.
        self.assertNotIn("blocked", self._collect(self._SENTINEL))
        self.assertNotIn("blocked", self._collect("maybe"))
        self.assertNotIn("blocked", self._collect(1))


class TestBlockedStatus(unittest.TestCase):
    """blocked -> PAUSED 승격. LiteLLM /health 는 blocked 를 걸러주지 않아서
    (v1.90.0 확인) 일시중지된 백엔드도 healthy 로 보고된다. 그대로 두면
    '트래픽을 못 받는데 UP' 인 거짓 정상이 대시보드에 남는다."""

    def _ll(self):
        return {
            "health": {
                "healthy_endpoints": [{"api_base": "http://a/v1"},
                                      {"api_base": "http://p/v1"}],
                "unhealthy_endpoints": [{"api_base": "http://b/v1"}],
            },
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1"},
                {"model_name": "B", "api_base": "http://b/v1"},
                # health 는 healthy 라고 하지만 관리자가 꺼둔 모델
                {"model_name": "P", "api_base": "http://p/v1", "blocked": True},
                {"model_name": "N", "api_base": "http://n/v1", "blocked": False},
            ],
        }

    def test_blocked_overrides_healthy_status(self):
        merged = {d["model_name"]: d
                  for d in m.merge_deployments_with_health(self._ll())}
        self.assertEqual(merged["P"]["status"], "PAUSED")
        self.assertEqual(merged["P"]["status_source"], "blocked")
        # 원래 health 판정은 보존 — 다시 켰을 때 뜰 백엔드인지 알아야 한다.
        self.assertEqual(merged["P"]["health_status"], "UP")
        # 그 판정의 근거도 보존. status_source 는 "blocked" 로 덮이므로 이게
        # 없으면 괄호 안 UP 이 실측인지 추정인지 화면에서 구분할 수 없다.
        self.assertEqual(merged["P"]["health_status_source"], "health")

    def test_paused_records_k8s_provenance_when_health_absent(self):
        # 회귀: MONITOR_HEALTH 기본값이 off 라 PAUSED 의 괄호 값은 대개 k8s
        # readiness 추정이다. 이를 "health" 로 뭉개거나 아예 안 남기면 화면이
        # 추정을 실측과 같은 확신으로 보여준다(판정근거 가시화의 취지 위반).
        ll = {"health": None,
              "deployments": [{"model_name": "P", "api_base": "http://p/v1",
                               "backends_ready": 2, "backends_desired": 2,
                               "blocked": True}]}
        row = m.merge_deployments_with_health(ll)[0]
        self.assertEqual(row["status"], "PAUSED")
        self.assertEqual(row["status_source"], "blocked")
        self.assertEqual(row["health_status"], "UP")
        self.assertEqual(row["health_status_source"], "k8s")   # 실측이 아니다

    def test_health_status_source_cleared_when_unpaused(self):
        # merge 는 한 스냅샷에서 두 번 돈다 — health_status 와 마찬가지로
        # 근거 필드도 재병합 때 지워져야 옛 값이 유령처럼 남지 않는다.
        ll = {"health": None,
              "deployments": [{"model_name": "P", "api_base": "http://p/v1",
                               "backends_ready": 2, "backends_desired": 2,
                               "blocked": True}]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        for d in ll["deployments"]:
            d["blocked"] = False
        row = m.merge_deployments_with_health(ll)[0]
        self.assertNotIn("health_status", row)
        self.assertNotIn("health_status_source", row)

    def test_blocked_false_and_absent_are_untouched(self):
        merged = {d["model_name"]: d
                  for d in m.merge_deployments_with_health(self._ll())}
        self.assertEqual(merged["A"]["status"], "UP")     # 키 없음
        self.assertEqual(merged["B"]["status"], "DOWN")   # 키 없음
        self.assertEqual(merged["N"]["status"], "?")      # blocked=False
        self.assertNotIn("health_status", merged["A"])
        self.assertNotIn("health_status", merged["N"])

    def test_summary_excludes_paused_from_both_cards(self):
        ll = self._ll()
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"litellm": ll, "backends": []}
        s = m.summarize(snap)
        statuses = [d["status"] for d in ll["deployments"]]
        # 카드 == 표 항등식 유지 (PAUSED 는 양쪽 어디에도 안 들어간다)
        self.assertEqual(s["deployments_healthy"], statuses.count("UP"))
        self.assertEqual(s["deployments_unhealthy"], statuses.count("DOWN"))
        self.assertEqual(s["deployments_healthy"], 1)     # A 만
        self.assertEqual(s["deployments_unhealthy"], 1)   # B 만
        self.assertEqual(s["deployments_blocked"], 1)     # P
        self.assertEqual(s["deployments_total"], 4)       # 전체는 그대로
        self.assertTrue(s["blocked_known"])

    def test_merge_is_idempotent_for_health_status(self):
        # 이 함수는 한 스냅샷에서 두 번 돈다(build_snapshot -> state.Refresher 가
        # /health 주입 후 재실행). 회귀: 이전 회차의 health_status 가 남으면
        # blocked 가 풀린 행에 옛 판정이 유령처럼 붙는다.
        ll = self._ll()
        ll["deployments"] = m.merge_deployments_with_health(ll)
        # 운영자가 P 를 다시 켰다고 가정(다음 /model/info 가 blocked=False 를 준다)
        for d in ll["deployments"]:
            if d["model_name"] == "P":
                d["blocked"] = False
        again = {d["model_name"]: d
                 for d in m.merge_deployments_with_health(ll)}
        self.assertEqual(again["P"]["status"], "UP")       # health 로 복귀
        self.assertNotIn("health_status", again["P"])      # 옛 값이 남지 않는다

    def test_blocked_known_false_when_litellm_never_reports_it(self):
        # 구버전 LiteLLM: 키가 하나도 없으면 '판별 불가' 로 남아야 한다.
        ll = {"health": None,
              "deployments": [{"model_name": "A", "api_base": "http://a/v1"}]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertFalse(s["blocked_known"])
        self.assertEqual(s["deployments_blocked"], 0)


class TestSummarize(unittest.TestCase):
    def test_cards_match_table_when_health_times_out(self):
        # 회귀 테스트: /health 타임아웃(health=None)이라도 카드 healthy 수가
        # 표(merged status)와 일치해야 한다.
        ll = {
            "groups": [{"model_group": "g"}],
            "health": None,
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "deployment"},
                {"model_name": "B", "api_base": "http://b/v1",
                 "backends_ready": 0, "backends_desired": 2,
                 "backend_source": "deployment"},
            ],
        }
        ll["deployments"] = m.merge_deployments_with_health(ll)
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertEqual(s["deployments_healthy"], 1)     # A UP
        self.assertEqual(s["deployments_unhealthy"], 1)   # B DOWN
        self.assertEqual(s["deployments_registered"], 2)
        self.assertEqual(s["backend_pods_ready"], 3)
        self.assertEqual(s["backend_pods_desired"], 5)
        self.assertTrue(s["backend_pods_known"])

    def test_backend_pods_dedup_shared_service(self):
        # 회귀 테스트: 여러 model_name 이 같은 (ns,svc) 백엔드를 공유해도
        # 물리 Pod 합계는 Service 당 한 번만 집계되어야 한다(이중 집계 금지).
        ll = {
            "groups": [], "health": None,
            "deployments": [
                {"model_name": "A", "api_base": "http://a1/v1",
                 "namespace": "kserve", "service": "a1",
                 "backends_ready": 2, "backends_desired": 2,
                 "backend_source": "deployment"},
                {"model_name": "A", "api_base": "http://a2/v1",
                 "namespace": "kserve", "service": "a2",
                 "backends_ready": 2, "backends_desired": 2,
                 "backend_source": "deployment"},
                {"model_name": "B", "api_base": "http://a1/v1",   # A-1 공유
                 "namespace": "kserve", "service": "a1",
                 "backends_ready": 2, "backends_desired": 2,
                 "backend_source": "deployment"},
            ],
        }
        ll["deployments"] = m.merge_deployments_with_health(ll)
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertEqual(s["backend_pods_ready"], 4)      # a1 + a2 (a1 한 번만)
        self.assertEqual(s["backend_pods_desired"], 4)
        self.assertEqual(s["deployments_registered"], 3)  # 행 수는 그대로
        self.assertEqual(s["deployments_healthy"], 3)     # 상태는 deployment 단위

    def test_fallback_to_health_counts_when_no_deployments(self):
        ll = {
            "groups": [],
            "health": {"healthy_count": 2, "unhealthy_count": 1},
            "deployments": [],
        }
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertEqual(s["deployments_healthy"], 2)
        self.assertEqual(s["deployments_unhealthy"], 1)

    def test_collect_error_counts(self):
        # 수집 실패 총계(k8s_errors/gpu_errors)를 summary 에 집계해 배너/메트릭이
        # 공유하게 한다(셀별 툴팁만으론 규모가 안 보임).
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "A", "api_base": "http://a/v1",
             "k8s_error": "pods: HTTP 403"},
            {"model_name": "B", "api_base": "http://b/v1",
             "gpu_error": "service: HTTP 404 Not Found"},
            {"model_name": "C", "api_base": "http://c/v1",
             "k8s_error": "x", "gpu_error": "y"},
            {"model_name": "D", "api_base": "http://d/v1"},
        ]}
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertEqual(s["k8s_errors"], 2)   # A, C
        self.assertEqual(s["gpu_errors"], 2)   # B, C


class TestResolveBackendCount(unittest.TestCase):
    def test_kserve_rawdeployment_label_sum(self):
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 2},
                                "spec": {"replicas": 3}}]}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertEqual(out["backends_ready"], 2)
        self.assertEqual(out["backends_desired"], 3)
        self.assertEqual(out["backend_source"], "deployment")
        self.assertEqual(out["namespace"], "kserve")
        self.assertEqual(out["service"], "qwen36-35b-predictor")

    def test_plain_service_endpointslice(self):
        client = FakeClient([
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.2"]},
                 {"conditions": {"ready": False}, "addresses": ["1.1.1.3"]},
             ]}]}, None)),
        ], default_namespace="kind")
        dep = {"api_base": "http://embeddinggemma-300m.kind:18080/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertEqual(out["backends_ready"], 2)   # ready=False 1개 제외
        self.assertEqual(out["backend_source"], "endpointslice")

    def test_serverless_flag_exported_via_revision(self):
        # 회귀: deploymentMode 가 없어도(모드 "Unknown") revision 이 있으면
        # Knative-backed — serverless=True 를 명시 필드로 내보내야
        # 능동 health check 가 마커(true)로도 이 백엔드를 ping 하지 못한다.
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"components": {"predictor": {
                 "latestReadyRevision": "qwen36-35b-predictor-00001"}}}}, None)),
            ("labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertTrue(out["serverless"])
        self.assertEqual(out["mode"], "Unknown")
        self.assertEqual(out["backends_ready"], 1)   # 떠 있어도(0 아님) 위험
        self.assertFalse(m._deployment_health_safe(
            dict(out, model_name="x", active_health_check=True)))

    def test_activator_only_exported(self):
        # 회귀: EndpointSlice 가 activator 뿐(=scale-to-zero 된 Knative Service)
        # 이면 activator_only=True 를 명시 필드로 내보내야 — ISVC 404 로
        # network_type 이 "service" 여도 능동 health check 가 절대 ping 안 한다.
        client = FakeClient([
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"],
                  "targetRef": {"namespace": "knative-serving",
                                "name": "activator-abc"}},
             ]}]}, None)),
        ], default_namespace="serving")
        dep = {"api_base": "http://pure-knative.serving.svc:80/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertTrue(out["activator_only"])
        self.assertEqual(out["network_type"], "service")   # ISVC 404
        self.assertIsNone(out["backends_ready"])
        self.assertFalse(m._deployment_health_safe(
            dict(out, model_name="x", active_health_check=True)))

    def test_statefulset_fills_desired_via_selector(self):
        # StatefulSet 으로 뜬 Service: EndpointSlice 는 ready 만 알고, 같은 이름
        # Deployment 는 없다(404). Service↔STS 네이밍 규칙이 없으므로 selector 로
        # Pod 을 찾아 ownerReferences 의 StatefulSet(이름이 svc 와 달라도 됨)에서
        # spec.replicas 를 읽어 desired 를 채운다 -> 집계 100% 초과가 안 생긴다.
        client = FakeClient([
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
             ]}]}, None)),
            # deployments/<svc> 라우팅 없음 -> 기본 404 (Deployment 아님)
            ("/services/",
             (True, {"spec": {"selector": {"app": "emb"}}}, None)),
            ("pods?labelSelector",
             (True, {"items": [{"metadata": {"ownerReferences": [
                 {"kind": "StatefulSet", "name": "emb-vllm-0svc"}]}}]}, None)),
            ("statefulsets/emb-vllm-0svc",
             (True, {"spec": {"replicas": 3}}, None)),
        ], default_namespace="kind")
        dep = {"api_base": "http://qwen3-embedding-8b.kind:8080/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        # ready 는 EndpointSlice(실제 서빙 엔드포인트) 유지, desired 는 STS 에서 보강
        self.assertEqual(out["backends_ready"], 1)
        self.assertEqual(out["backends_desired"], 3)   # degraded 은폐 안 됨(1/3)
        self.assertEqual(out["backend_source"], "endpointslice")

    def test_bare_pod_leaves_desired_none(self):
        # 소유 컨트롤러가 없는 bare Pod: selector 로 Pod 은 찾아도 ownerReferences 에
        # StatefulSet 이 없으면 desired 를 지어내지 않고 None 으로 남긴다.
        client = FakeClient([
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
             ]}]}, None)),
            ("/services/",
             (True, {"spec": {"selector": {"app": "lonely"}}}, None)),
            ("pods?labelSelector",
             (True, {"items": [{"metadata": {}}]}, None)),  # ownerReferences 없음
        ], default_namespace="kind")
        dep = {"api_base": "http://lonely-pod.kind:8080/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertEqual(out["backends_ready"], 1)
        self.assertIsNone(out["backends_desired"])
        self.assertEqual(out["backend_source"], "endpointslice")

    def test_external_api_base_short_circuits(self):
        client = FakeClient([])
        dep = {"api_base": "http://50.50.65.54:8000/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertEqual(out["backend_source"], "external")
        self.assertEqual(out["network_type"], "external")
        self.assertEqual(client.calls, [])           # k8s 호출 안 함

    def test_network_type_from_isvc_lookup(self):
        # 네트워크 타입은 문자열 추측이 아니라 ISVC 조회 결과로 판정한다.
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 2},
                                "spec": {"replicas": 2}}]}, None)),
        ], default_namespace="kserve")
        out = m.resolve_backend_count(
            {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"},
            client, SETTINGS)
        self.assertEqual(out["network_type"], "kserve")   # ISVC GET 성공
        # ISVC 404(FakeClient 기본 응답) = KServe 아님 → 단순 Service
        client2 = FakeClient([
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
             ]}]}, None)),
        ], default_namespace="kind")
        out2 = m.resolve_backend_count(
            {"api_base": "http://embeddinggemma-300m.kind:18080/v1"},
            client2, SETTINGS)
        self.assertEqual(out2["network_type"], "service")

    def test_network_type_unknown_on_isvc_lookup_error(self):
        # 404 가 아닌 실패(RBAC/CRD/타임아웃)는 kserve/service 를 단정하지 않는다.
        client = FakeClient([
            ("inferenceservices", (False, None, "HTTP 403 Forbidden")),
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
             ]}]}, None)),
        ], default_namespace="kind")
        out = m.resolve_backend_count(
            {"api_base": "http://svc-a.kind:8000/v1"}, client, SETTINGS)
        self.assertEqual(out["network_type"], "-")
        self.assertEqual(out["backends_ready"], 1)   # 개수 수집은 정상 진행
        # 개수 수집이 성공해 k8s_error 가 비어도 '-' 의 원인은 전용 필드에 남는다
        self.assertEqual(out["network_type_error"], "isvc: HTTP 403 Forbidden")

    def test_network_type_404_must_be_prefix_not_substring(self):
        # 'char 404' 같은 우연 일치가 'ISVC 없음(service)' 으로 오판되면 안 된다.
        client = FakeClient([
            ("inferenceservices",
             (False, None,
              "JSONDecodeError: Expecting value: line 1 column 405 (char 404)")),
            ("endpointslices",
             (True, {"items": [{"endpoints": [
                 {"conditions": {"ready": True}, "addresses": ["1.1.1.1"]},
             ]}]}, None)),
        ], default_namespace="kind")
        out = m.resolve_backend_count(
            {"api_base": "http://svc-a.kind:8000/v1"}, client, SETTINGS)
        self.assertEqual(out["network_type"], "-")   # 단정 금지
        self.assertIn("JSONDecodeError", out["network_type_error"])

    def test_cache_avoids_duplicate_k8s_calls(self):
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
        ], default_namespace="kserve")
        cache = {}
        # 같은 Service 를 가리키는 두 model_name (api_base 문자열은 달라도 됨)
        d1 = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        d2 = {"api_base": "http://qwen36-35b-predictor.kserve.svc.cluster.local:8080/v1"}
        m.resolve_backend_count(d1, client, SETTINGS, cache)
        n_after_first = len(client.calls)
        out2 = m.resolve_backend_count(d2, client, SETTINGS, cache)
        self.assertEqual(len(client.calls), n_after_first)   # 추가 호출 0
        self.assertEqual(out2["backends_ready"], 1)


def _pod(node, gpu, ready=True):
    return {"spec": {"nodeName": node,
                     "containers": [{"resources": {"limits": {"nvidia.com/gpu": str(gpu)}}}]},
            "status": {"phase": "Running" if ready else "Pending",
                       "conditions": [{"type": "Ready",
                                       "status": "True" if ready else "False"}]}}


class TestGpu(unittest.TestCase):
    GPU_SETTINGS = dict(SETTINGS, gpu_info=True)

    def test_short_gpu_product(self):
        self.assertEqual(m._short_gpu_product("NVIDIA-H100-80GB-HBM3"), "H100")
        self.assertEqual(m._short_gpu_product("NVIDIA-B200"), "B200")
        self.assertEqual(m._short_gpu_product("NVIDIA-A100-SXM4-80GB"), "A100")
        self.assertIsNone(m._short_gpu_product(None))

    def test_pod_gpu_and_ready(self):
        self.assertEqual(m._pod_gpu(_pod("n", 4)), 4)
        self.assertTrue(m._pod_ready(_pod("n", 1, ready=True)))
        self.assertFalse(m._pod_ready(_pod("n", 1, ready=False)))

    def test_kserve_gpu_sum_and_device(self):
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 2},
                                "spec": {"replicas": 2}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [_pod("gpu-a", 2), _pod("gpu-a", 2)]}, None)),
            ("/nodes/gpu-a",
             (True, {"metadata": {"labels":
                     {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}}}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, self.GPU_SETTINGS)
        self.assertEqual(out["gpu_ready"], 4)            # 2 pod × 2 GPU
        self.assertEqual(out["gpu_products"], {"H100": 4})
        self.assertIsNone(out["gpu_error"])

    def test_node_cache_persists_across_clients(self):
        # 회귀: 노드 GPU 라벨은 노드 수명 동안 불변 → 사이클 간(=K8sClient 재생성)
        # node_cache 를 넘기면 두 번째 조회에서 /nodes/... 를 다시 부르지 않는다
        # (정적 라벨을 위해 Node 오브젝트를 5초마다 반복 조회하던 부하 제거).
        routes = [
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [_pod("gpu-a", 2)]}, None)),
            ("/nodes/gpu-a",
             (True, {"metadata": {"labels":
                     {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}}}, None)),
        ]
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        node_cache = {}

        c1 = FakeClient(routes, default_namespace="kserve")
        out1 = m.resolve_backend_count(dep, c1, self.GPU_SETTINGS,
                                       node_cache=node_cache)
        self.assertEqual(out1["gpu_products"], {"H100": 2})
        self.assertTrue(any("/nodes/gpu-a" in p for p in c1.calls))

        # 다음 사이클: 새 클라이언트지만 같은 node_cache → 노드 재조회 없이 캐시 히트
        c2 = FakeClient(routes, default_namespace="kserve")
        out2 = m.resolve_backend_count(dep, c2, self.GPU_SETTINGS,
                                       node_cache=node_cache)
        self.assertEqual(out2["gpu_products"], {"H100": 2})
        self.assertFalse(any("/nodes/gpu-a" in p for p in c2.calls))

    def test_node_cache_skips_failed_lookups(self):
        # 회귀: 캐시가 프로세스 수명이 되면서, 노드 GET 일시 실패를 캐시하면 그
        # 노드 장치명이 재기동 전까지 'GPU'(미상)로 영구히 굳는다 — 실패는 캐시
        # 밖에 두고 다음 사이클에 자가 치유되어야 한다.
        base_routes = [
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [_pod("gpu-a", 2)]}, None)),
        ]
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        node_cache = {}

        # 1사이클: /nodes/gpu-a 라우트 없음(404) → 미상 'GPU' 버킷, 캐시 미기록
        c1 = FakeClient(list(base_routes), default_namespace="kserve")
        out1 = m.resolve_backend_count(dep, c1, self.GPU_SETTINGS,
                                       node_cache=node_cache)
        self.assertEqual(out1["gpu_products"], {"GPU": 2})
        self.assertNotIn("gpu-a", node_cache)   # 실패는 캐시하지 않는다

        # 2사이클: 노드 조회 복구 → 같은 캐시로 자가 치유
        c2 = FakeClient(base_routes + [
            ("/nodes/gpu-a",
             (True, {"metadata": {"labels":
                     {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}}}, None)),
        ], default_namespace="kserve")
        out2 = m.resolve_backend_count(dep, c2, self.GPU_SETTINGS,
                                       node_cache=node_cache)
        self.assertEqual(out2["gpu_products"], {"H100": 2})
        self.assertEqual(node_cache.get("gpu-a"), "NVIDIA-H100-80GB-HBM3")

    def test_gpu_zero_when_no_ready_pods(self):
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 0},
                                "spec": {"replicas": 2}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [_pod("gpu-a", 2, ready=False)]}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, self.GPU_SETTINGS)
        self.assertEqual(out["gpu_ready"], 0)            # ready pod 없음 -> 0 (장애 아님)
        self.assertEqual(out["gpu_products"], {})

    @staticmethod
    def _pod_img(image, gpu=1, extra=None, command=None, args=None):
        ctr = {"image": image}
        if gpu:
            ctr["resources"] = {"limits": {"nvidia.com/gpu": str(gpu)}}
        if command:
            ctr["command"] = command
        if args:
            ctr["args"] = args
        return {"spec": {"nodeName": "n1", "containers": [ctr] + (extra or [])},
                "status": {"phase": "Running",
                           "conditions": [{"type": "Ready", "status": "True"}]}}

    def test_pod_engine_from_image_prefers_gpu_container(self):
        # GPU 를 점유한 컨테이너(서빙)를 우선 검사 — queue-proxy 사이드카 배제
        pod = self._pod_img("registry.local/vllm/vllm-openai:v0.8", gpu=1,
                            extra=[{"image": "knative/queue-proxy:1.0"}])
        self.assertEqual(m._pod_engine(pod), "vllm")

    def test_engine_mixed_when_two_engines_coexist(self):
        # 엔진 교체 롤아웃 중 vllm/sglang Pod 공존 → Pod 순서 무관 'mixed' 고정
        # (첫 Pod 하나로 정하면 목록 순서에 따라 폴링마다 값이 플랩한다).
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 2},
                                "spec": {"replicas": 2}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [self._pod_img("vllm/vllm-openai:v0.8", gpu=1),
                               self._pod_img("sglang/sglang:v0.4", gpu=1)]}, None)),
            ("/nodes/n1", (True, {"metadata": {"labels": {}}}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, self.GPU_SETTINGS)
        self.assertEqual(out["backend_type"], "mixed")
        self.assertEqual(out["backend_type_source"], "pod")

    def test_pod_engine_command_fallback_and_unknown(self):
        # 리네임된 사설 레지스트리 이미지 → command/args 로 폴백 판별
        pod = self._pod_img("registry.local/llm-server:1.0", gpu=1,
                            command=["python", "-m", "sglang.launch_server"])
        self.assertEqual(m._pod_engine(pod), "sglang")
        self.assertIsNone(m._pod_engine(
            self._pod_img("registry.local/other:1.0", gpu=1)))

    def test_backend_type_overridden_by_pod_image(self):
        # 이름 휴리스틱이 틀려도 Pod 이미지 판정이 이기고 source=pod 가 된다.
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
            ("/pods?labelSelector",
             (True, {"items": [self._pod_img("sglang/sglang:v0.4", gpu=2)]}, None)),
            ("/nodes/n1", (True, {"metadata": {"labels": {}}}, None)),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1",
               "backend_type": "vllm", "backend_type_source": "name"}
        out = m.resolve_backend_count(dep, client, self.GPU_SETTINGS)
        self.assertEqual(out["backend_type"], "sglang")
        self.assertEqual(out["backend_type_source"], "pod")
        # GPU 수집이 꺼져 있으면 out 은 backend_type 을 건드리지 않는다(휴리스틱 유지).
        client2 = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
        ], default_namespace="kserve")
        out2 = m.resolve_backend_count(dict(dep), client2, SETTINGS)
        self.assertNotIn("backend_type", out2)

    def test_gpu_unknown_when_pods_forbidden(self):
        client = FakeClient([
            ("inferenceservices/qwen36-35b",
             (True, {"status": {"deploymentMode": "RawDeployment",
                                "components": {"predictor": {}}}}, None)),
            ("/deployments?labelSelector",
             (True, {"items": [{"status": {"readyReplicas": 1},
                                "spec": {"replicas": 1}}]}, None)),
            ("/pods?labelSelector", (False, None, "HTTP 403 Forbidden")),
        ], default_namespace="kserve")
        dep = {"api_base": "http://qwen36-35b-predictor.kserve.svc:8080/v1"}
        out = m.resolve_backend_count(dep, client, self.GPU_SETTINGS)
        self.assertIsNone(out["gpu_ready"])              # 권한 없음 -> ? 폴백
        self.assertIn("403", out["gpu_error"])
        self.assertEqual(out["backends_ready"], 1)       # Pod 개수는 영향 없음

    def test_summarize_gpu_dedup_shared_service(self):
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "A", "namespace": "kserve", "service": "a1",
             "api_base": "http://a1/v1", "backends_ready": 2,
             "gpu_ready": 4, "gpu_products": {"H100": 4}},
            {"model_name": "B", "namespace": "kserve", "service": "a1",  # 공유
             "api_base": "http://a1/v1", "backends_ready": 2,
             "gpu_ready": 4, "gpu_products": {"H100": 4}},
            {"model_name": "C", "namespace": "kserve", "service": "a2",
             "api_base": "http://a2/v1", "backends_ready": 1,
             "gpu_ready": 2, "gpu_products": {"B200": 2}},
        ]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertEqual(s["gpu_total"], 6)              # a1(4) 한 번 + a2(2)
        self.assertEqual(s["gpu_products"], {"H100": 4, "B200": 2})
        self.assertTrue(s["gpu_known"])


class TestSorting(unittest.TestCase):
    def test_merge_sorts_deployments_by_name_case_insensitive(self):
        ll = {"health": None, "deployments": [
            {"model_name": "zoo", "api_base": "http://z/v1"},
            {"model_name": "Apple", "api_base": "http://a/v1"},
            {"model_name": "mango", "api_base": "http://m/v1"},
        ]}
        out = m.merge_deployments_with_health(ll)
        self.assertEqual([d["model_name"] for d in out], ["Apple", "mango", "zoo"])

    def test_order_is_deterministic_for_case_and_dup_ties(self):
        # 회귀: 대소문자만 다른 이름('vllm-X'↔'vLLM-X')과 같은 이름의 deployment 가
        # 여러 개면 lower 단일 키로는 동률이라 입력 순서를 따라가 폴링마다 뒤바뀐다.
        # 입력 순서를 뒤집어도 출력 순서가 동일해야(결정적) 한다.
        a = {"model_name": "vLLM-X", "api_base": "http://a/v1", "id": "1"}
        b = {"model_name": "vllm-X", "api_base": "http://b/v1", "id": "2"}
        c = {"model_name": "Qwen", "api_base": "http://c/v1", "id": "9"}
        d = {"model_name": "Qwen", "api_base": "http://c/v1", "id": "8"}  # 이름·base 같고 id만 다름
        order1 = [o["id"] for o in m.merge_deployments_with_health(
            {"health": None, "deployments": [a, b, c, d]})]
        order2 = [o["id"] for o in m.merge_deployments_with_health(
            {"health": None, "deployments": [d, c, b, a]})]
        self.assertEqual(order1, order2)                 # 입력 순서와 무관하게 동일
        # 'Qwen'(이름·base 동률) 은 id 로, 'vLLM/vllm-X'(lower 동률) 는 원문으로 갈린다
        self.assertEqual(order1, ["8", "9", "1", "2"])

    def test_demo_groups_and_deployments_sorted(self):
        snap = m.demo_snapshot()
        groups = [g["model_group"] for g in snap["litellm"]["groups"]]
        deps = [d["model_name"] for d in snap["litellm"]["deployments"]]
        self.assertEqual(groups, sorted(groups, key=str.lower))
        self.assertEqual(deps, sorted(deps, key=str.lower))


class TestDemoSnapshot(unittest.TestCase):
    def test_demo_snapshot_consistent(self):
        snap = m.demo_snapshot()
        s = snap["summary"]
        statuses = [d["status"] for d in snap["litellm"]["deployments"]]
        # 카드 healthy 수 == 표의 UP 행 수
        self.assertEqual(s["deployments_healthy"], statuses.count("UP"))
        self.assertEqual(s["deployments_unhealthy"], statuses.count("DOWN"))

    def test_demo_has_epoch_and_error_surfacing(self):
        # 데모도 build_snapshot 과 동일하게 ts_epoch 을 싣고, DOWN 사유·GPU 수집
        # 오류를 노출해 새 UI(툴팁/배너)를 미리보기 할 수 있어야 한다.
        snap = m.demo_snapshot()
        self.assertIsInstance(snap.get("ts_epoch"), float)
        self.assertGreaterEqual(snap["summary"]["gpu_errors"], 1)
        downs = [d for d in snap["litellm"]["deployments"]
                 if d.get("status") == "DOWN" and d.get("down_reason")]
        self.assertTrue(downs, "데모에 사유가 붙은 DOWN 이 하나는 있어야 한다")


class TestCollectUserAccess(unittest.TestCase):
    """per-user 키 접근 수집 — http_get_json 을 가짜로 갈아끼워 분기만 고정."""

    def _patch(self, fake):
        # collect_user_access 는 app.services.user_access 모듈 전역 http_get_json 을
        # 부르므로, 그 모듈 속성을 교체해야 패치가 먹는다.
        self._orig = _ua.http_get_json
        _ua.http_get_json = fake

    def tearDown(self):
        if getattr(self, "_orig", None):
            _ua.http_get_json = self._orig

    def test_fail_closed_on_v1_models_error(self):
        # 키 무효/만료 -> ok=False, accessible 빈 집합 (global 폴백 금지의 근거)
        self._patch(lambda url, key=None, timeout=10:
                    (False, None, "HTTP 401 Unauthorized"))
        out = m.collect_user_access("http://litellm:4000", "sk-bad", 5)
        self.assertFalse(out["ok"])
        self.assertEqual(out["accessible"], [])
        self.assertIsNotNone(out["error"])

    def test_accessible_from_v1_models_sorted_and_meta(self):
        def fake(url, key=None, timeout=10):
            if url.endswith("/v1/models"):
                return (True, {"data": [{"id": "b"}, {"id": "a"}, {"id": None}]},
                        None)
            if url.endswith("/key/info"):
                return (True, {"info": {"spend": 2.0, "max_budget": 10,
                                        "tpm_limit": 100, "rpm_limit": 5,
                                        "key_alias": "team-x"}}, None)
            return (False, None, "404")
        self._patch(fake)
        out = m.collect_user_access("http://litellm:4000/", "sk-good", 5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["accessible"], ["a", "b"])     # 정렬·None 제외
        self.assertEqual(out["key_info"]["spend"], 2.0)
        self.assertEqual(out["key_info"]["tpm_limit"], 100)
        self.assertEqual(out["key_info"]["key_alias"], "team-x")

    def test_key_info_failure_is_nonfatal(self):
        # /key/info 못 읽어도(비-admin 버전) 모델 목록(접근권)은 그대로 살아야 함
        def fake(url, key=None, timeout=10):
            if url.endswith("/v1/models"):
                return (True, {"data": [{"id": "a"}]}, None)
            return (False, None, "HTTP 403")
        self._patch(fake)
        out = m.collect_user_access("http://litellm:4000", "sk", 5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["accessible"], ["a"])
        self.assertIsNone(out["key_info"])


class TestAccessCache(unittest.TestCase):
    """키별 접근 캐시 — 폴링 중복 호출 제거, 성공/실패 각각 TTL 만료."""

    def test_get_peek_without_collect(self):
        # get() 은 수집 없이 살아있는 항목만 돌려준다(요청 경로 세마포어 선회피).
        cache = m.AccessCache(ttl=30.0)
        self.assertIsNone(cache.get("sk-x", now=100.0))      # 미스
        cache.get_or_collect(
            "sk-x", lambda: {"ok": True, "accessible": ["a"]}, now=100.0)
        hit = cache.get("sk-x", now=110.0)                   # TTL 내
        self.assertEqual(hit["accessible"], ["a"])
        self.assertIsNone(cache.get("sk-x", now=200.0))      # 만료

    def test_caches_success_and_skips_recollect(self):
        cache = m.AccessCache(ttl=30.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": True, "accessible": ["a"]}
        a1 = cache.get_or_collect("sk-x", collect, now=100.0)
        a2 = cache.get_or_collect("sk-x", collect, now=110.0)   # TTL 내
        self.assertEqual(calls["n"], 1)                          # 재호출 없음
        self.assertIs(a1, a2)

    def test_recollect_after_ttl(self):
        cache = m.AccessCache(ttl=30.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": True}
        cache.get_or_collect("sk-x", collect, now=100.0)
        cache.get_or_collect("sk-x", collect, now=131.0)        # TTL 경과
        self.assertEqual(calls["n"], 2)

    def test_failure_cached_briefly_then_revalidated(self):
        # 실패(무효/만료 키)도 짧은 fail_ttl 동안 캐시 — 5초 폴링이 매번 blocking
        # LiteLLM 왕복을 새로 일으켜 스레드·CPU 를 잡아먹고 502 나는 걸 막는다.
        cache = m.AccessCache(ttl=30.0, fail_ttl=3.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": False, "error": "401"}
        cache.get_or_collect("sk-bad", collect, now=100.0)
        cache.get_or_collect("sk-bad", collect, now=101.0)      # fail_ttl 내 → 캐시 재사용
        self.assertEqual(calls["n"], 1)
        cache.get_or_collect("sk-bad", collect, now=104.0)      # fail_ttl 경과 → 재검증
        self.assertEqual(calls["n"], 2)

    def test_failure_ttl_zero_disables_negative_cache(self):
        # fail_ttl=0 이면 옛 동작(실패는 절대 캐시 안 함)으로 되돌아간다.
        cache = m.AccessCache(ttl=30.0, fail_ttl=0.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": False}
        cache.get_or_collect("sk-bad", collect, now=100.0)
        cache.get_or_collect("sk-bad", collect, now=100.5)      # 매번 재검증
        self.assertEqual(calls["n"], 2)

    def test_distinct_keys_distinct_entries(self):
        cache = m.AccessCache(ttl=30.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": True}
        cache.get_or_collect("sk-a", collect, now=100.0)
        cache.get_or_collect("sk-b", collect, now=100.0)
        self.assertEqual(calls["n"], 2)

    def test_raw_key_never_stored(self):
        cache = m.AccessCache(ttl=30.0)
        cache.get_or_collect("sk-secret", lambda: {"ok": True}, now=100.0)
        self.assertNotIn("sk-secret", cache._d)                 # 해시만 보관
        self.assertEqual(len(next(iter(cache._d))), 64)         # sha256 hex


class TestFilterSnapshotForUser(unittest.TestCase):
    def _global(self):
        return {
            "version": "x", "backend_count_enabled": True,
            "collect_error": "boom http://internal:8080",
            "litellm": {
                "url": "http://litellm:4000",
                "errors": ["model/info: HTTP 500 http://internal-a:8080"],
                "health": {"healthy_count": 1},
                "groups": [{"model_group": "gpt-x"}, {"model_group": "secret-y"}],
                "deployments": [
                    {"model_name": "gpt-x", "api_base": "http://internal-a/v1",
                     "underlying": "vllm/x", "type": "vllm", "status": "UP",
                     "network_type": "kserve", "backend_type": "vllm",
                     "namespace": "ns-a", "service": "svc-a",
                     "backends_ready": 2, "backends_desired": 2,
                     "backend_source": "deployment"},
                    # gpt-x 와 같은 백엔드(svc-a)를 공유 — backend_ref 토폴로지 검증용
                    {"model_name": "gpt-x-router", "api_base": "http://internal-a/v1",
                     "type": "vllm", "status": "UP",
                     "network_type": "kserve", "backend_type": "vllm",
                     "namespace": "ns-a", "service": "svc-a",
                     "backends_ready": 2, "backends_desired": 2,
                     "backend_source": "deployment"},
                    {"model_name": "secret-y", "api_base": "http://internal-b/v1",
                     "type": "vllm", "status": "DOWN",
                     "backends_ready": 0, "backends_desired": 1,
                     "backend_source": "deployment",
                     # DOWN 사유는 내부 주소를 담을 수 있어 비-admin 뷰에서 숨겨야 한다.
                     "down_reason": "connection",
                     "status_detail": "Connection error http://internal-b:8080"},
                ],
            },
            "backends": [], "summary": {},
        }

    def test_filters_and_recomputes_summary(self):
        g = self._global()
        access = {"accessible": ["gpt-x"], "key_info": {"spend": 1.5}}
        out = m.filter_snapshot_for_user(g, access)
        self.assertEqual([d["model_name"] for d in out["litellm"]["deployments"]],
                         ["gpt-x"])
        self.assertEqual([gp["model_group"] for gp in out["litellm"]["groups"]],
                         ["gpt-x"])
        self.assertTrue(out["user_view"])
        self.assertEqual(out["accessible_count"], 1)
        self.assertEqual(out["key_info"]["spend"], 1.5)
        self.assertEqual(out["litellm"]["models"], ["gpt-x"])
        # summary 재계산: 접근 가능한 gpt-x(UP) 1개만 집계
        self.assertEqual(out["summary"]["deployments_healthy"], 1)
        self.assertEqual(out["summary"]["deployments_registered"], 1)
        self.assertEqual(out["summary"]["backend_pods_ready"], 2)

    def test_hide_internal_strips_topology(self):
        out = m.filter_snapshot_for_user(
            self._global(), {"accessible": ["gpt-x"]}, hide_internal=True)
        d = out["litellm"]["deployments"][0]
        self.assertNotIn("api_base", d)
        self.assertNotIn("underlying", d)
        self.assertEqual(out["litellm"]["errors"], [])
        self.assertNotIn("health", out["litellm"])
        self.assertNotIn("url", out["litellm"])
        self.assertNotIn("collect_error", out)
        # 상태·Pod 수(키 무관, deployment 단위)는 그대로 유지
        self.assertEqual(d["status"], "UP")
        self.assertEqual(d["backends_ready"], 2)

    def test_hide_internal_strips_down_reason_detail(self):
        # DOWN 사유(status_detail)에 내부 주소가 섞일 수 있어 비-admin 뷰에선
        # allowlist 리댁션으로 자동 제거돼야 한다(admin 전체 뷰에만 노출).
        out = m.filter_snapshot_for_user(
            self._global(), {"accessible": ["secret-y"]}, hide_internal=True)
        d = out["litellm"]["deployments"][0]
        self.assertEqual(d["model_name"], "secret-y")
        self.assertEqual(d["status"], "DOWN")          # 상태는 유지
        self.assertNotIn("status_detail", d)           # 내부 주소 유출 방지
        self.assertNotIn("down_reason", d)

    def test_redaction_keeps_type_axes_and_anon_backend_ref(self):
        out = m.filter_snapshot_for_user(
            self._global(), {"accessible": ["gpt-x", "gpt-x-router"]},
            hide_internal=True)
        deps = out["litellm"]["deployments"]
        d = deps[0]
        # 2축 타입은 유지(내부 이름이 아니라 분류값이라 노출 무해)
        self.assertEqual(d["network_type"], "kserve")
        self.assertEqual(d["backend_type"], "vllm")
        # ns/svc 는 여전히 숨기고, 익명 backend_ref 로만 백엔드를 식별
        self.assertNotIn("namespace", d)
        self.assertNotIn("service", d)
        refs = {x["model_name"]: x["backend_ref"] for x in deps}
        self.assertEqual(refs["gpt-x"], refs["gpt-x-router"])   # 같은 svc-a 공유
        self.assertEqual(len(refs["gpt-x"]), 8)
        self.assertNotIn("svc-a", refs["gpt-x"])                # 원문 이름 미노출

    def test_per_user_summary_dedups_by_backend_ref(self):
        # 리댁션 뷰는 ns/svc/api_base 가 없어 summarize dedup 키가 붕괴했었다 —
        # backend_ref 로 dedup: 공유 백엔드(svc-a)는 1회만, 다른 백엔드는 합산.
        out = m.filter_snapshot_for_user(
            self._global(),
            {"accessible": ["gpt-x", "gpt-x-router", "secret-y"]},
            hide_internal=True)
        s = out["summary"]
        self.assertEqual(s["backend_pods_ready"], 2)    # svc-a(2) 1회 + secret-y(0)
        self.assertEqual(s["backend_pods_desired"], 3)  # svc-a(2) 1회 + secret-y(1)

    def test_backend_ref_seed_and_no_api_base_fallback(self):
        # seed 가 다르면(사용자마다) 같은 백엔드의 ref 가 달라진다 — 사용자 간
        # '내 뷰 JSON' 대조로 백엔드 공유 관계를 상관 분석하는 것 차단.
        d = {"namespace": "ns-a", "service": "svc-a"}
        self.assertEqual(m._backend_ref(d, "seed1"), m._backend_ref(d, "seed1"))
        self.assertNotEqual(m._backend_ref(d, "seed1"), m._backend_ref(d, "seed2"))
        # api_base 조차 없는 deployment 는 id/model_name 으로 서로 다른 ref —
        # 하나의 'external' 로 뭉치면 그래프에 거짓 공유(⇄)가 생긴다.
        a = m._backend_ref({"model_name": "openai-a", "id": "id-1"}, "s")
        b = m._backend_ref({"model_name": "openai-b", "id": "id-2"}, "s")
        self.assertIsNotNone(a)
        self.assertNotEqual(a, b)

    def test_redaction_keeps_blocked_state(self):
        # "내 모델이 왜 응답이 없나" 의 답 — 내부 토폴로지가 아니므로 비-admin
        # 뷰에도 남긴다. 여기서 빠지면 사용자는 장애와 일시중지를 구분 못 한다.
        g = self._global()
        g["litellm"]["deployments"][0].update(
            {"blocked": True, "status": "PAUSED", "health_status": "UP",
             "health_status_source": "k8s", "status_source": "blocked"})
        out = m.filter_snapshot_for_user(g, {"accessible": ["gpt-x"]})
        d = out["litellm"]["deployments"][0]
        self.assertIs(d["blocked"], True)
        self.assertEqual(d["status"], "PAUSED")
        self.assertEqual(d["health_status"], "UP")
        # 괄호 안 UP 이 실측인지 추정인지도 사용자 뷰에서 구분돼야 한다
        # (상태 문자열일 뿐 내부 토폴로지가 아니다).
        self.assertEqual(d["health_status_source"], "k8s")
        # 내부 주소는 여전히 가려져 있어야 한다.
        self.assertNotIn("api_base", d)
        self.assertEqual(out["summary"]["deployments_blocked"], 1)
        self.assertTrue(out["summary"]["blocked_known"])

    def test_redaction_does_not_invent_blocked_key(self):
        # 회귀: 리댁션이 blocked 키를 무조건 넣으면(값 None 이라도) summarize 의
        # blocked_known 이 항상 참이 되어, blocked 를 모르는 LiteLLM 에서도
        # "일시중지 판별 가능" 이라고 거짓 보고한다.
        out = m.filter_snapshot_for_user(self._global(), {"accessible": ["gpt-x"]})
        d = out["litellm"]["deployments"][0]
        self.assertNotIn("blocked", d)
        self.assertNotIn("health_status", d)
        self.assertNotIn("health_status_source", d)
        self.assertFalse(out["summary"]["blocked_known"])

    def test_view_is_isolated_from_global_snapshot(self):
        """뷰의 mutable 을 건드려도 global 스냅샷이 안 깨져야 한다.

        회귀: 예전 구현은 스냅샷을 통째로 deepcopy 해서 자동으로 격리됐다.
        비용 때문에 '필터 먼저, 필요한 것만 복사' 로 바꿨으므로(배포 1000개 중
        50개 접근 사용자 13.9ms -> 0.21ms) 남기는 값마다 복사를 빠뜨리면
        모든 사용자가 공유하는 global 스냅샷이 오염된다. 두 모드 다 검사한다.
        """
        for hide in (True, False):
            g = self._global()
            before = json.dumps(g, sort_keys=True, default=str)
            v = m.filter_snapshot_for_user(
                g, {"accessible": ["gpt-x", "gpt-y"]}, hide_internal=hide)
            v["litellm"]["deployments"].clear()
            v["litellm"]["groups"].append({"model_group": "침입"})
            v["litellm"]["models"].append("침입")
            v["summary"]["deployments_total"] = -1
            if isinstance(v.get("backends"), list):
                v["backends"].append({"url": "침입"})
            self.assertEqual(json.dumps(g, sort_keys=True, default=str), before,
                             "hide_internal=%s 뷰 변형이 global 로 새어나갔다" % hide)

    def test_redaction_emits_only_scalars(self):
        """리댁션 출력에 컨테이너가 없어야 한다 — filter 가 이 불변식에 기댄다.

        회귀: hide_internal=True(기본 per-user 모드)는 리댁션된 행을 deepcopy
        하지 않는다. `_redact_deployment_for_user` 가 스칼라만 낸다는 전제이기
        때문이다. allowlist 에 컨테이너 필드(예: gpu_products)를 하나 추가하면
        그 전제가 깨지고, 뷰는 공유 스냅샷과 **같은 객체**를 가리킨다 — 사용자
        한 명이 자기 뷰를 변형하면 모든 사용자가 읽는 global 스냅샷이 오염된다.
        예전 구현(전체 deepcopy)은 allowlist 내용과 무관하게 안전했으므로, 이
        불변식은 성능 최적화가 새로 들여온 것이다. 발생 지점에서 고정한다.

        컨테이너를 정말 노출해야 한다면 이 테스트를 고치는 게 아니라
        filter_snapshot_for_user 의 리댁션 분기에서 deepcopy 를 해야 한다.
        """
        src = {
            "model_name": "m", "namespace": "ns", "service": "svc",
            "api_base": "http://x/v1", "status": "UP", "status_source": "health",
            "type": "vllm", "network_type": "service", "backend_type": "vllm",
            "backends_ready": 1, "backends_desired": 1, "backend_source": "eps",
            "scale_to_zero": False, "mode": "RawDeployment",
            "blocked": True, "health_status": "UP", "health_status_source": "k8s",
            # 컨테이너 값들 — 리댁션이 이들을 통과시키면 안 된다.
            "gpu_products": {"H100": 2}, "tags": ["a", "b"],
            "nested": {"deep": {"x": 1}},
        }
        out = m._redact_deployment_for_user(src, "seed")
        containers = {k: type(v).__name__ for k, v in out.items()
                      if isinstance(v, (dict, list, set, tuple))}
        self.assertEqual(
            containers, {},
            "리댁션이 컨테이너를 노출한다: %s — hide_internal=True 경로는 이 값을 "
            "deepcopy 하지 않으므로 global 스냅샷과 객체를 공유하게 된다" % containers)

    def test_nested_containers_are_not_shared_in_redacted_view(self):
        # 위 불변식의 결과를 filter 수준에서도 확인한다: 리댁션 뷰가 global 의
        # 중첩 컨테이너를 참조로 물고 있지 않은지(현재는 아예 노출 안 하므로
        # 키 부재로 통과 — 노출로 바뀌면 위 테스트가 먼저 깨진다).
        g = self._global()
        for d in g["litellm"]["deployments"]:
            d["gpu_products"] = {"H100": 2}
        v = m.filter_snapshot_for_user(
            g, {"accessible": ["gpt-x", "gpt-y"]}, hide_internal=True)
        for vd, gd in zip(v["litellm"]["deployments"], g["litellm"]["deployments"]):
            if "gpu_products" in vd:
                self.assertIsNot(vd["gpu_products"], gd["gpu_products"])

    def test_nested_containers_are_not_shared_with_global(self):
        # gpu_products 처럼 행 안의 중첩 dict 까지 복사돼야 한다 — 얕은 복사면
        # 사용자가 받은 뷰와 global 이 같은 객체를 가리킨다(내부노출 모드).
        g = self._global()
        for d in g["litellm"]["deployments"]:
            d["gpu_products"] = {"H100": 2}
        v = m.filter_snapshot_for_user(
            g, {"accessible": ["gpt-x", "gpt-y"]}, hide_internal=False)
        for vd, gd in zip(v["litellm"]["deployments"], g["litellm"]["deployments"]):
            self.assertIsNot(vd["gpu_products"], gd["gpu_products"])

    def test_show_internal_keeps_api_base(self):
        out = m.filter_snapshot_for_user(
            self._global(), {"accessible": ["gpt-x"]}, hide_internal=False)
        self.assertEqual(out["litellm"]["deployments"][0]["api_base"],
                         "http://internal-a/v1")

    def test_does_not_mutate_global(self):
        # 공유 캐시 오염 방지: 원본 global 스냅샷은 절대 변형되면 안 된다.
        g = self._global()
        before = copy.deepcopy(g)
        m.filter_snapshot_for_user(g, {"accessible": ["gpt-x"]})
        self.assertEqual(g, before)

    def test_no_access_yields_empty_view(self):
        out = m.filter_snapshot_for_user(self._global(), {"accessible": []})
        self.assertEqual(out["litellm"]["deployments"], [])
        self.assertEqual(out["accessible_count"], 0)
        self.assertEqual(out["summary"]["deployments_registered"], 0)


class TestPrometheusMetrics(unittest.TestCase):
    """render_prometheus_metrics: exposition 포맷·인코딩·중복 series 처리."""

    def _parse(self, text):
        """exposition 텍스트에서 (metric_name{labels}) -> value 맵으로 파싱."""
        out = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            key, _, val = line.rpartition(" ")
            out.setdefault(key, []).append(val)
        return out

    def _snap(self):
        ll = {
            "groups": [{"model_group": "g"}],
            "health": None,
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1",
                 "namespace": "ns1", "service": "svc-a",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "deployment", "scale_to_zero": False},
                {"model_name": "B", "api_base": "http://b/v1",
                 "namespace": "ns1", "service": "svc-b",
                 "backends_ready": 0, "backends_desired": 2,
                 "backend_source": "knative-pa", "scale_to_zero": True},
            ],
        }
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "9.9.9", "litellm": ll, "backends": [],
                "backend_count_enabled": True}
        snap["summary"] = m.summarize(snap)
        return snap

    def test_well_formed_exposition(self):
        text = m.render_prometheus_metrics(self._snap())
        self.assertTrue(text.endswith("\n"))
        # HELP/TYPE 헤더가 각 메트릭마다 있어야 한다.
        self.assertIn("# TYPE model_monitor_model_up gauge", text)
        self.assertIn("# HELP model_monitor_up", text)
        # 모든 비주석 라인은 "name{...} value" 또는 "name value" 형태.
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[a-zA-Z_][a-zA-Z0-9_]*(\{.*\})? -?\d")

    def test_status_encoding(self):
        parsed = self._parse(m.render_prometheus_metrics(self._snap()))
        up = parsed['model_monitor_model_up{model="A",namespace="ns1",'
                    'service="svc-a",status_source="k8s"}']
        self.assertEqual(up, ["1"])   # A: ready>0 -> UP=1
        # B: ready=0 이지만 scale_to_zero -> 정상 idle "?" -> -1 (DOWN 아님)
        idle = parsed['model_monitor_model_up{model="B",namespace="ns1",'
                      'service="svc-b",status_source="k8s"}']
        self.assertEqual(idle, ["-1"])
        self.assertEqual(parsed['model_monitor_model_scale_to_zero{model="B"}'],
                         ["1"])

    def test_unknown_status_is_minus_one(self):
        # status 가 ?(미상)면 -1 로 인코딩.
        ll = {"groups": [], "health": None,
              "deployments": [{"model_name": "U", "api_base": "1.2.3.4",
                               "backend_source": "external"}]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": [],
                "backend_count_enabled": False}
        snap["summary"] = m.summarize(snap)
        text = m.render_prometheus_metrics(snap)
        self.assertIn('model_monitor_model_up{model="U",status_source="unknown"} -1',
                      text)

    def test_summary_gauges_present(self):
        parsed = self._parse(m.render_prometheus_metrics(self._snap()))
        self.assertEqual(parsed["model_monitor_deployments_total"], ["2"])
        self.assertEqual(parsed["model_monitor_deployments_healthy"], ["1"])  # A UP
        # B 는 scale_to_zero -> "?" 이므로 DOWN(unhealthy) 아님
        self.assertEqual(parsed["model_monitor_deployments_unhealthy"], ["0"])
        # 공유 없는 두 Service -> ready 합 3, desired 합 5
        self.assertEqual(parsed["model_monitor_backend_pods_ready_total"], ["3"])
        self.assertEqual(parsed["model_monitor_backend_pods_desired_total"], ["5"])
        self.assertEqual(parsed['model_monitor_build_info{version="9.9.9"}'], ["1"])

    def test_gpu_metrics_and_shared_service_dedup(self):
        # X, Y 가 같은 (ns, svc1) 을 공유(로드밸런싱/라우팅) — 물리 GPU 는 동일하므로
        # 총합은 1회만 집계돼야 한다. Z 는 다른 Service(B200).
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "X", "api_base": "http://x/v1",
             "namespace": "ns", "service": "svc1",
             "backends_ready": 3, "backends_desired": 3,
             "backend_source": "deployment",
             "gpu_ready": 6, "gpu_products": {"H100": 6}},
            {"model_name": "Y", "api_base": "http://x/v1",  # 같은 (ns, svc1) 공유
             "namespace": "ns", "service": "svc1",
             "backends_ready": 3, "backends_desired": 3,
             "backend_source": "deployment",
             "gpu_ready": 6, "gpu_products": {"H100": 6}},
            {"model_name": "Z", "api_base": "http://z/v1",
             "namespace": "ns", "service": "svc2",
             "backends_ready": 1, "backends_desired": 1,
             "backend_source": "deployment",
             "gpu_ready": 2, "gpu_products": {"B200": 2}},
        ]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": [],
                "backend_count_enabled": True}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        # 총합: svc1(6) 은 1회만 + svc2(2) = 8, 이중 집계 아님.
        self.assertEqual(parsed["model_monitor_backend_gpus_ready_total"], ["8"])
        self.assertEqual(parsed["model_monitor_backend_gpus_known"], ["1"])
        # 장치별(이기종): H100=6(공유 dedup), B200=2.
        self.assertEqual(
            parsed['model_monitor_backend_gpus_ready_by_device{device="H100"}'],
            ["6"])
        self.assertEqual(
            parsed['model_monitor_backend_gpus_ready_by_device{device="B200"}'],
            ["2"])
        # 모델별: 공유 X/Y 는 각각 6 으로 노출(합산 금지 — total 이 정답).
        self.assertEqual(
            parsed['model_monitor_model_backend_gpus_ready'
                   '{model="X",namespace="ns",service="svc1"}'], ["6"])
        self.assertEqual(
            parsed['model_monitor_model_backend_gpus_ready'
                   '{model="Y",namespace="ns",service="svc1"}'], ["6"])

    def test_gpu_unknown_emits_zero_flag(self):
        # GPU 정보가 전혀 없으면 known=0, total=0, 장치별 series 도 없어야 한다.
        parsed = self._parse(m.render_prometheus_metrics(self._snap()))
        self.assertEqual(parsed["model_monitor_backend_gpus_known"], ["0"])
        self.assertEqual(parsed["model_monitor_backend_gpus_ready_total"], ["0"])
        self.assertFalse(
            any(k.startswith("model_monitor_backend_gpus_ready_by_device")
                for k in parsed))

    def test_duplicate_series_collapsed(self):
        # 회귀: LiteLLM 은 한 model_name 에 여러 deployment 를 둘 수 있다(로드밸런싱).
        # 같은 라벨 series 가 중복되면 Prometheus 스크레이프가 깨지므로 1개로 합쳐야 한다.
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "LB", "api_base": "http://x/v1",
             "namespace": "ns", "service": "svc",
             "backends_ready": 2, "backends_desired": 2,
             "backend_source": "deployment"},
            {"model_name": "LB", "api_base": "http://x/v1",  # 동일 (model,ns,svc)
             "namespace": "ns", "service": "svc",
             "backends_ready": 2, "backends_desired": 2,
             "backend_source": "deployment"},
        ]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": [],
                "backend_count_enabled": True}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        # 동일 라벨이 정확히 1개 series 로만 나와야 한다.
        for key, vals in parsed.items():
            self.assertEqual(len(vals), 1,
                             "중복 series 발생: %s -> %s" % (key, vals))

    def test_down_wins_on_status_collision(self):
        # 같은 라벨(model,ns,svc,status_source)에서 UP/DOWN 충돌 시 DOWN(0) 우선.
        # 같은 (ns,svc) 인데 backend api_base 만 달라 한쪽은 healthy, 한쪽은 unhealthy.
        ll = {"groups": [], "health": {
            "healthy_endpoints": [{"api_base": "http://up/v1"}],
            "unhealthy_endpoints": [{"api_base": "http://down/v1"}]},
            "deployments": [
                {"model_name": "C", "api_base": "http://up/v1",
                 "namespace": "ns", "service": "svc"},
                {"model_name": "C", "api_base": "http://down/v1",
                 "namespace": "ns", "service": "svc"},
            ]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": []}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        key = [k for k in parsed if k.startswith("model_monitor_model_up{")][0]
        self.assertEqual(parsed[key], ["0"])  # 충돌 -> DOWN

    def test_label_value_escaping(self):
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": 'we"ird\\name', "api_base": "http://x/v1",
             "namespace": "ns", "service": "svc",
             "backends_ready": 1, "backend_source": "deployment"}]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": []}
        snap["summary"] = m.summarize(snap)
        text = m.render_prometheus_metrics(snap)
        # 따옴표·역슬래시가 이스케이프되어야 한다.
        self.assertIn(r'model="we\"ird\\name"', text)

    def test_blocked_metrics(self):
        ll = {
            "groups": [], "health": None,
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1",
                 "namespace": "ns1", "service": "svc-a", "backends_ready": 2},
                {"model_name": "P", "api_base": "http://p/v1",
                 "namespace": "ns1", "service": "svc-p",
                 "backends_ready": 2, "blocked": True},
            ],
        }
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "9.9.9", "litellm": ll, "backends": []}
        snap["summary"] = m.summarize(snap)
        g = self._parse(m.render_prometheus_metrics(snap))
        self.assertEqual(
            g['model_monitor_model_blocked{model="P",namespace="ns1",service="svc-p"}'],
            ["1"])
        self.assertEqual(
            g['model_monitor_model_blocked{model="A",namespace="ns1",service="svc-a"}'],
            ["0"])
        self.assertEqual(g["model_monitor_deployments_blocked"], ["1"])
        self.assertEqual(g["model_monitor_blocked_known"], ["1"])
        # PAUSED 는 model_up 에서 -1(미상) — 기존 DOWN 알림(==0)을 건드리지 않는다.
        self.assertEqual(
            g['model_monitor_model_up{model="P",namespace="ns1",'
              'service="svc-p",status_source="blocked"}'], ["-1"])
        # 일시중지는 healthy 카드에도 안 잡힌다.
        self.assertEqual(g["model_monitor_deployments_healthy"], ["1"])

    def test_blocked_series_collapse_requires_all_blocked(self):
        # 같은 (model,ns,svc) 라벨의 deployment 가 둘인데 하나만 꺼진 경우,
        # 그 조합은 여전히 라우팅된다 -> 0 이어야 한다(min). max 면 거짓 양성.
        ll = {
            "groups": [], "health": None,
            "deployments": [
                {"model_name": "M", "api_base": "http://m1/v1",
                 "namespace": "ns", "service": "svc",
                 "backends_ready": 2, "blocked": True},
                {"model_name": "M", "api_base": "http://m2/v1",
                 "namespace": "ns", "service": "svc",
                 "backends_ready": 2, "blocked": False},
            ],
        }
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "9.9.9", "litellm": ll, "backends": []}
        snap["summary"] = m.summarize(snap)
        g = self._parse(m.render_prometheus_metrics(snap))
        self.assertEqual(
            g['model_monitor_model_blocked{model="M",namespace="ns",service="svc"}'],
            ["0"])
        # 행 단위 집계는 그대로 1건이 PAUSED (그래야 카드가 표와 일치한다)
        self.assertEqual(g["model_monitor_deployments_blocked"], ["1"])

    def test_blocked_known_zero_on_old_litellm(self):
        g = self._parse(m.render_prometheus_metrics(self._snap()))
        self.assertEqual(g["model_monitor_blocked_known"], ["0"])
        self.assertEqual(g["model_monitor_deployments_blocked"], ["0"])

    def test_no_api_base_label_leak(self):
        # 내부 URL(api_base)은 메트릭 라벨에 노출되면 안 된다(카디널리티/보안).
        text = m.render_prometheus_metrics(self._snap())
        self.assertNotIn("api_base", text)
        self.assertNotIn("http://a/v1", text)

    def test_loading_snapshot_reports_down(self):
        text = m.render_prometheus_metrics({"loading": True, "version": "x"})
        self.assertIn("model_monitor_up 0", text)

    def test_reliability_gauges_present(self):
        # 수집 신뢰도 게이지: reachable / litellm_errors / collect_failing /
        # k8s·gpu 수집 에러 총계가 모두 노출돼야 한다.
        ll = {"groups": [], "reachable": True,
              "errors": ["health: timeout", "선택적 경고"],
              "health": None, "deployments": [
            {"model_name": "A", "api_base": "http://a/v1",
             "backends_ready": 1, "backend_source": "deployment",
             "k8s_error": "pods: 403"},
            {"model_name": "B", "api_base": "http://b/v1",
             "backends_ready": 2, "backend_source": "deployment",
             "gpu_error": "service: 404"},
        ]}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        snap = {"version": "x", "litellm": ll, "backends": [],
                "backend_count_enabled": True}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        self.assertEqual(parsed["model_monitor_litellm_reachable"], ["1"])
        self.assertEqual(parsed["model_monitor_litellm_errors"], ["2"])
        self.assertEqual(parsed["model_monitor_collect_errors"], ["1"])
        self.assertEqual(parsed["model_monitor_gpu_collect_errors"], ["1"])
        self.assertEqual(parsed["model_monitor_collect_failing"], ["0"])

    def test_litellm_unreachable_and_collect_failing(self):
        snap = {"version": "x", "backend_count_enabled": True,
                "collect_error": "RuntimeError: boom",
                "litellm": {"reachable": False, "errors": [],
                            "deployments": []}}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        self.assertEqual(parsed["model_monitor_litellm_reachable"], ["0"])
        self.assertEqual(parsed["model_monitor_collect_failing"], ["1"])

    def test_snapshot_age_and_timestamp(self):
        # ts_epoch 가 있으면 timestamp 를 그대로, age 는 음수 없이 노출.
        ll = {"groups": [], "reachable": True, "health": None,
              "deployments": []}
        snap = {"version": "x", "litellm": ll, "backends": [],
                "ts_epoch": 1_000_000.0}
        snap["summary"] = m.summarize(snap)
        parsed = self._parse(m.render_prometheus_metrics(snap))
        self.assertEqual(
            parsed["model_monitor_snapshot_timestamp_seconds"], ["1000000"])
        age = float(parsed["model_monitor_snapshot_age_seconds"][0])
        self.assertGreaterEqual(age, 0.0)

    def test_snapshot_age_absent_without_epoch(self):
        # ts_epoch 가 없는(구버전) 스냅샷은 age/timestamp 시리즈를 안 만든다.
        text = m.render_prometheus_metrics({"loading": True, "version": "x"})
        self.assertNotIn("model_monitor_snapshot_timestamp_seconds", text)
        self.assertNotIn("model_monitor_snapshot_age_seconds", text)


# ----- 웹/배선 계층(FastAPI 전환으로 새로 추가된 코드) 단위 테스트 -----
# admin 게이트/설정 병합/백그라운드 스토어는 보안·동작 핵심인데 순수 함수라
# FastAPI 없이도 검증 가능하다(라우트 자체는 통합 영역이라 제외).

class _FakeRequest:
    """auth 헬퍼 검증용 최소 request 더미(헤더 + app.state 자격만)."""

    def __init__(self, headers=None, admin_key="", metrics_token=""):
        self.headers = headers or {}
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(
                admin_key=admin_key, metrics_token=metrics_token))


class TestAuth(unittest.TestCase):
    def test_admin_key_match(self):
        self.assertTrue(m.is_admin_key("sk-admin", "sk-admin"))

    def test_admin_key_mismatch(self):
        self.assertFalse(m.is_admin_key("sk-admin", "sk-other"))

    def test_empty_admin_never_matches(self):
        # admin_key 미설정(빈/None)이면 어떤 키도 통과 못 한다(fail-closed).
        self.assertFalse(m.is_admin_key("", "sk-x"))
        self.assertFalse(m.is_admin_key("", ""))
        self.assertFalse(m.is_admin_key(None, "sk-x"))

    def test_empty_request_key_never_matches(self):
        self.assertFalse(m.is_admin_key("sk-admin", ""))
        self.assertFalse(m.is_admin_key("sk-admin", None))

    def test_request_key_reads_and_strips_header(self):
        req = _FakeRequest(headers={"X-LiteLLM-Key": "  sk-x  "})
        self.assertEqual(m.request_key(req), "sk-x")

    def test_request_key_absent_is_empty(self):
        self.assertEqual(m.request_key(_FakeRequest()), "")

    def test_admin_ok_true_only_for_matching_admin_header(self):
        ok = _FakeRequest(headers={"X-LiteLLM-Key": "sk-admin"},
                          admin_key="sk-admin")
        self.assertTrue(m.admin_ok(ok))
        bad = _FakeRequest(headers={"X-LiteLLM-Key": "sk-nope"},
                           admin_key="sk-admin")
        self.assertFalse(m.admin_ok(bad))
        nokey = _FakeRequest(admin_key="sk-admin")
        self.assertFalse(m.admin_ok(nokey))

    def test_non_ascii_key_is_false_not_500(self):
        # latin-1 로 디코딩된 non-ASCII 헤더가 TypeError(→500)를 내면 안 된다.
        self.assertFalse(m.is_admin_key("sk-admin", "sk-café"))

    def test_bearer_token_parses_and_strips(self):
        req = _FakeRequest(headers={"Authorization": "Bearer  tok-123 "})
        self.assertEqual(m.bearer_token(req), "tok-123")
        # 대소문자 무관 스킴
        req2 = _FakeRequest(headers={"Authorization": "bearer tok-123"})
        self.assertEqual(m.bearer_token(req2), "tok-123")

    def test_bearer_token_absent_or_other_scheme_is_empty(self):
        self.assertEqual(m.bearer_token(_FakeRequest()), "")
        basic = _FakeRequest(headers={"Authorization": "Basic dXNlcjpwdw=="})
        self.assertEqual(m.bearer_token(basic), "")

    def test_metrics_ok_admin_header_still_works(self):
        req = _FakeRequest(headers={"X-LiteLLM-Key": "sk-admin"},
                           admin_key="sk-admin", metrics_token="")
        self.assertTrue(m.metrics_ok(req))

    def test_metrics_ok_bearer_token(self):
        ok = _FakeRequest(headers={"Authorization": "Bearer scrape-tok"},
                          admin_key="sk-admin", metrics_token="scrape-tok")
        self.assertTrue(m.metrics_ok(ok))
        wrong = _FakeRequest(headers={"Authorization": "Bearer nope"},
                             admin_key="sk-admin", metrics_token="scrape-tok")
        self.assertFalse(m.metrics_ok(wrong))

    def test_metrics_ok_fail_closed_without_token_config(self):
        # 토큰 미설정이면 Bearer 로는 절대 못 연다(admin 키만 유효).
        req = _FakeRequest(headers={"Authorization": "Bearer anything"},
                           admin_key="sk-admin", metrics_token="")
        self.assertFalse(m.metrics_ok(req))
        # 토큰이 admin 키를 대체하지도 않는다(스냅샷/export 용 admin_ok 는 불변).
        tok = _FakeRequest(headers={"X-LiteLLM-Key": "scrape-tok"},
                           admin_key="sk-admin", metrics_token="scrape-tok")
        self.assertFalse(m.admin_ok(tok))
        self.assertFalse(m.metrics_ok(tok))  # 토큰은 Bearer 로만 인정


class TestSnapshotStore(unittest.TestCase):
    def test_loading_placeholder_when_empty(self):
        store = m.SnapshotStore()
        snap = asyncio.run(store.get())
        self.assertTrue(snap.get("loading"))
        self.assertIsNone(snap.get("litellm"))

    def test_set_get_roundtrip_no_error_flag(self):
        store = m.SnapshotStore()

        async def go():
            await store.set({"version": "x", "summary": {}}, None)
            return await store.get()

        snap = asyncio.run(go())
        self.assertEqual(snap["version"], "x")
        self.assertNotIn("collect_error", snap)

    def test_collect_error_attached_as_copy(self):
        store = m.SnapshotStore()
        base = {"version": "x", "summary": {}}

        async def go():
            await store.set(base, None)
            await store.set_error("boom")
            return await store.get()

        out = asyncio.run(go())
        self.assertEqual(out.get("collect_error"), "boom")
        # 반환은 사본(dict(snap, ...)) 이라 캐시 원본은 오염되지 않아야 한다.
        self.assertNotIn("collect_error", base)


class TestRefresherBackoff(unittest.TestCase):
    """연속 수집 예외 시 리프레시 지연이 지수 백오프(상한 60s)로 늘어난다 —
    예상 밖 결함이 5초 타이트 재시도로 CPU(200m 캡)와 로그를 태우지 않게."""

    def test_next_delay_exponential_with_cap(self):
        r = m.Refresher({}, m.SnapshotStore(), interval=5.0)
        self.assertEqual(r._next_delay(0), 5.0)     # 정상 시 원래 주기
        self.assertEqual(r._next_delay(1), 10.0)
        self.assertEqual(r._next_delay(2), 20.0)
        self.assertEqual(r._next_delay(3), 40.0)
        self.assertEqual(r._next_delay(4), 60.0)    # 상한 도달
        self.assertEqual(r._next_delay(50), 60.0)   # 계속 실패해도 상한 고정

    def test_next_delay_never_below_interval(self):
        # interval 이 상한(60s)보다 크면 백오프가 주기를 단축하지 않는다.
        r = m.Refresher({}, m.SnapshotStore(), interval=120.0)
        self.assertEqual(r._next_delay(0), 120.0)
        self.assertEqual(r._next_delay(3), 120.0)


class TestRefresherDemo(unittest.TestCase):
    def test_collect_once_demo_populates_store(self):
        store = m.SnapshotStore()
        r = m.Refresher({}, store, interval=5.0, demo=True)
        snap = asyncio.run(r.collect_once())
        self.assertTrue(snap.get("demo"))
        self.assertIn("summary", snap)
        cached = asyncio.run(store.get())
        self.assertTrue(cached.get("demo"))

    def test_interval_has_floor(self):
        r = m.Refresher({}, m.SnapshotStore(), interval=0.0, demo=True)
        self.assertGreaterEqual(r.interval, 1.0)


def _settings_ns(**over):
    """Settings(pydantic) 의 기본값을 흉내낸 더미 — build_collector_settings 는
    속성 접근만 하므로 SimpleNamespace 로 충분하다(pydantic 인스턴스 불필요).
    env 우선순위는 os.environ 으로 별도 제어한다."""
    base = dict(
        host="0.0.0.0", port=8088, interval=5.0, demo=False,
        litellm_url=None, api_key=None, timeout=10.0, health=False,
        health_timeout=90.0, selective_health=True, probe_backends=False,
        backend_count=True, gpu_info=True,
        k8s_api_server=None, k8s_token_file="/t/token", k8s_ca_file="/t/ca",
        k8s_insecure=False, k8s_timeout=5.0,
        user_view=False, user_view_show_internal=False,
        user_view_cache_ttl=30.0, metrics=True, metrics_token=None,
        config_file=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


@unittest.skipUnless(_HAS_PYDANTIC, "app.config import 에 pydantic-settings 필요")
class TestBuildCollectorSettings(unittest.TestCase):
    def test_defaults_no_env_no_file(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            c = m.build_collector_settings(_settings_ns())
        self.assertTrue(c["backend_count"])
        self.assertTrue(c["gpu_info"])
        self.assertFalse(c["user_view"])
        self.assertTrue(c["metrics"])
        self.assertTrue(c["user_view_hide_internal"])  # show_internal False -> hide
        self.assertIsNone(c["litellm_url"])

    def test_gpu_requires_backend_count(self):
        # backend_count 가 꺼지면 gpu_info 를 켜도 의미 없으니 함께 꺼진다.
        with mock.patch.dict(os.environ,
                             {"MONITOR_BACKEND_COUNT": "false",
                              "MONITOR_GPU_INFO": "true"}, clear=True):
            c = m.build_collector_settings(
                _settings_ns(backend_count=False, gpu_info=True))
        self.assertFalse(c["backend_count"])
        self.assertFalse(c["gpu_info"])

    def test_file_used_when_env_absent(self):
        cfg = {"litellm": {"url": "http://file-llm:4000"},
               "backend_count": {"enabled": False},
               "user_view": {"enabled": True, "show_internal": True}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                c = m.build_collector_settings(_settings_ns(config_file=path))
        finally:
            os.unlink(path)
        self.assertEqual(c["litellm_url"], "http://file-llm:4000")
        self.assertFalse(c["backend_count"])           # 파일값 반영
        self.assertTrue(c["user_view"])                # 파일값 반영
        self.assertFalse(c["user_view_hide_internal"])  # show_internal True

    def test_env_beats_file(self):
        cfg = {"litellm": {"url": "http://file-llm:4000"},
               "backend_count": {"enabled": True}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        try:
            # Settings(env) 가 이미 env 를 반영한 상태 + os.environ 에 플래그 존재.
            with mock.patch.dict(os.environ,
                                 {"LITELLM_BASE_URL": "http://env-llm:4000",
                                  "MONITOR_BACKEND_COUNT": "false"}, clear=True):
                c = m.build_collector_settings(_settings_ns(
                    litellm_url="http://env-llm:4000", backend_count=False,
                    config_file=path))
        finally:
            os.unlink(path)
        self.assertEqual(c["litellm_url"], "http://env-llm:4000")  # env 우선
        self.assertFalse(c["backend_count"])                       # env 우선

    def test_selective_health_default_env_file(self):
        # 기본 true(전량 /health 기본 off 를 보완) / env 우선 / 파일 반영
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                m.build_collector_settings(_settings_ns())["selective_health"])
        with mock.patch.dict(os.environ,
                             {"MONITOR_SELECTIVE_HEALTH": "true"}, clear=True):
            c = m.build_collector_settings(_settings_ns(selective_health=True))
        self.assertTrue(c["selective_health"])
        cfg = {"litellm": {"selective_health": True}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                c = m.build_collector_settings(_settings_ns(config_file=path))
        finally:
            os.unlink(path)
        self.assertTrue(c["selective_health"])   # 파일값 반영

    def test_health_default_off_env_overrides(self):
        # 전량 /health 는 모든 백엔드 실 ping(scale-to-zero 각성) — opt-in 으로만.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(m.build_collector_settings(_settings_ns())["health"])
        with mock.patch.dict(os.environ, {"MONITOR_HEALTH": "true"}, clear=True):
            self.assertTrue(m.build_collector_settings(
                _settings_ns(health=True))["health"])
        cfg = {"litellm": {"health": True}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                c = m.build_collector_settings(_settings_ns(config_file=path))
        finally:
            os.unlink(path)
        self.assertTrue(c["health"])   # 파일값 반영

    def test_metrics_token_env_beats_file(self):
        cfg = {"metrics": {"enabled": True, "token": "file-tok"}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            path = f.name
        try:
            with mock.patch.dict(os.environ, {}, clear=True):
                c_file = m.build_collector_settings(_settings_ns(config_file=path))
            with mock.patch.dict(os.environ,
                                 {"MONITOR_METRICS_TOKEN": "env-tok"}, clear=True):
                c_env = m.build_collector_settings(_settings_ns(
                    metrics_token="env-tok", config_file=path))
        finally:
            os.unlink(path)
        self.assertEqual(c_file["metrics_token"], "file-tok")  # 파일값 반영
        self.assertEqual(c_env["metrics_token"], "env-tok")    # env 우선
        # 기본값은 None(토큰 비활성 — Bearer 경로 fail-closed).
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(m.build_collector_settings(
                _settings_ns())["metrics_token"])


class TestK8sMetaCache(unittest.TestCase):
    """사이클 간 메타 캐시(ISVC 부재 · Service selector).

    실측(배포 35 / 고유 Service 12 / 5초 주기): Service 마다 사이클당 k8s 5회 =
    하루 약 103만 회인데, 그중 ISVC 부재 확인과 spec.selector 는 매번 같은 답을
    준다. 이 둘만 TTL 캐시해 사이클당 60 -> 36회(40% 감소, 하루 약 41만 회)로
    줄인다. 캐시하면 **안 되는** 것들이 회귀 포인트라 함께 고정한다.
    """

    class _Client:
        default_namespace = "default"
        enabled = True

        def __init__(self, isvc_exists=False, isvc_err="HTTP 404 not found",
                     pods=1, selector="app=x"):
            self.calls = collections.Counter()
            self.isvc_exists = isvc_exists
            self.isvc_err = isvc_err
            self.pods = pods
            self.selector = selector

        def get(self, path, *a, **kw):
            if "inferenceservices" in path:
                self.calls["isvc"] += 1
                if not self.isvc_exists:
                    return False, None, self.isvc_err
                # revision 은 롤아웃마다 바뀌는 동적 값 — 호출마다 다르게 준다.
                return True, {"status": {
                    "deploymentMode": "RawDeployment",
                    "components": {"predictor": {
                        "latestReadyRevision": "rev-%d" % self.calls["isvc"]}}}}, None
            if "/services/" in path:
                self.calls["svc"] += 1
                k, v = self.selector.split("=")
                return True, {"spec": {"selector": {k: v}}}, None
            if "/pods" in path:
                self.calls["pods"] += 1
                items = [{"metadata": {"name": "pod-1"},
                          "spec": {"nodeName": "node-1", "containers": [
                              {"image": "vllm/x", "resources": {
                                  "limits": {"nvidia.com/gpu": "2"}}}]},
                          "status": {"conditions": [
                              {"type": "Ready", "status": "True"}]}}]
                return True, {"items": items[:self.pods]}, None
            if "/nodes/" in path:
                self.calls["node"] += 1
                return True, {"metadata": {"labels": {
                    "nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}}}, None
            self.calls["other"] += 1
            return False, None, "n/a"

    def test_absent_isvc_is_cached_after_first_cycle(self):
        meta = {}
        counts = []
        for _ in range(3):
            c = self._Client()
            m.detect_mode_and_revision(c, "ns", "svc", meta)
            counts.append(c.calls["isvc"])
        self.assertEqual(counts, [1, 0, 0], "ISVC 404 가 캐시되지 않았다")

    def test_present_isvc_is_never_cached(self):
        # 회귀: 성공을 캐시하면 revision(latestReadyRevision)이 굳고, 그걸 쓰는
        # Knative PodAutoscaler 조회가 사라진 revision 을 가리킨다.
        meta = {}
        revs = []
        # 클라이언트를 사이클 간 공유해야 revision 이 실제로 바뀐다(가짜 응답이
        # 호출 횟수로 revision 을 만든다) — 캐시되면 조회가 안 일어나 값이 굳는다.
        c = self._Client(isvc_exists=True)
        for i in range(3):
            info, _ = m.detect_mode_and_revision(c, "ns", "svc", meta)
            self.assertEqual(c.calls["isvc"], i + 1,
                             "성공을 캐시해 조회가 사라졌다")
            revs.append(info["revision"])
        self.assertEqual(revs, ["rev-1", "rev-2", "rev-3"],
                         "revision 이 갱신되지 않았다(캐시에 굳었다)")
        self.assertEqual(meta, {}, "found=True 를 캐시했다")

    def test_transient_isvc_failure_is_not_cached(self):
        # 404 가 아닌 실패(RBAC/타임아웃)를 캐시하면 network_type 이 TTL 동안
        # '-'(판정 불가)로 굳는다 — 노드 라벨 캐시와 같은 원칙.
        meta = {}
        for _ in range(3):
            c = self._Client(isvc_err="HTTP 403 forbidden")
            m.detect_mode_and_revision(c, "ns", "svc", meta)
            self.assertEqual(c.calls["isvc"], 1)
        self.assertEqual(meta, {}, "일시적 실패가 캐시됐다")

    def test_selector_is_cached(self):
        meta = {}
        counts = []
        for _ in range(3):
            c = self._Client()
            m.service_pod_selector(c, "ns", "svc", meta)
            counts.append(c.calls["svc"])
        self.assertEqual(counts, [1, 0, 0])

    def test_selector_failure_is_not_cached(self):
        class NoSvc(TestK8sMetaCache._Client):
            def get(self, path, *a, **kw):
                if "/services/" in path:
                    self.calls["svc"] += 1
                    return False, None, "HTTP 403 forbidden"
                return super().get(path, *a, **kw)
        meta = {}
        for _ in range(2):
            c = NoSvc()
            sel, err = m.service_pod_selector(c, "ns", "svc", meta)
            self.assertIsNone(sel)
            self.assertEqual(c.calls["svc"], 1)
        self.assertEqual(meta, {})

    def test_selector_dropped_when_pod_query_comes_back_empty(self):
        # 자기치유: 라벨을 바꾼 재배포면 캐시한 selector 가 Pod 을 못 찾는다.
        # TTL 을 기다리지 않고 버려 다음 사이클에 다시 읽어야 한다.
        meta, nc = {}, {}
        seen = []
        for pods in (1, 1, 0, 1, 1):
            c = self._Client(pods=pods)
            m.collect_gpu_for_service(c, "ns", "svc", "svc", False, nc, meta)
            seen.append(c.calls["svc"])
        self.assertEqual(seen, [1, 0, 0, 1, 0],
                         "Pod 0건 뒤에 selector 를 다시 읽지 않았다")

    def test_cache_disabled_when_none(self):
        # CLI/직접 호출/기존 테스트 경로: meta_cache 를 안 주면 종전 그대로.
        for _ in range(3):
            c = self._Client()
            m.detect_mode_and_revision(c, "ns", "svc", None)
            self.assertEqual(c.calls["isvc"], 1)
            c2 = self._Client()
            m.service_pod_selector(c2, "ns", "svc", None)
            self.assertEqual(c2.calls["svc"], 1)

    def test_ttl_expiry_refetches(self):
        meta = {}
        m_gpu = importlib.import_module("app.services.gpu")
        m_gpu.meta_put(meta, ("sel", "ns", "s"), "app=y", ttl=0.05)
        self.assertTrue(m_gpu.meta_get(meta, ("sel", "ns", "s"))[0])
        time.sleep(0.1)
        self.assertFalse(m_gpu.meta_get(meta, ("sel", "ns", "s"))[0])

    def test_cache_is_bounded(self):
        # node_cache 와 달리 상한을 둔다 — (ns,svc) 라 유계지만 안전망.
        m_gpu = importlib.import_module("app.services.gpu")
        cache = {}
        for i in range(m_gpu._META_MAX + 100):
            m_gpu.meta_put(cache, ("sel", "ns", "s%d" % i), "app=x")
        self.assertLessEqual(len(cache), m_gpu._META_MAX)

    def test_steady_state_call_reduction(self):
        """정상상태에서 Service 당 5회 -> 3회 (동적 조회만 남는다)."""
        def cycle(meta):
            c = self._Client()
            d = {"model_name": "m", "api_base": "http://svc.ns.svc:8000/v1"}
            m.resolve_backend_count(d, c, {"gpu": True}, {}, {}, meta)
            return sum(c.calls.values())
        meta = {}
        first, second, third = cycle(meta), cycle(meta), cycle(meta)
        self.assertEqual(second, third, "정상상태가 안정적이지 않다")
        self.assertLess(second, first)
        # 남는 것은 EndpointSlice / Deployment status / Pod 목록 — 전부 동적.
        self.assertEqual(first - second, 2,
                         "절감이 ISVC+selector 2회가 아니다")


class TestDashboardTemplateScopes(unittest.TestCase):
    """대시보드 템플릿의 JS 스코프 정적 검사.

    render() 는 표를 그리는 if 블록 안에서 필터 전 전체 목록(`all`)을 잡고,
    그래프는 그 블록 **밖에서** 그린다. `all` 을 블록 지역(const)으로 두면
    pausedMap(all) 이 ReferenceError 로 죽는데, render 호출이 폴링 try/catch 로
    감싸여 있어 예외가 "수집 실패" 문구로 위장되고 **그래프만 조용히 사라진다**
    — 실제로 그렇게 회귀했고, HTTP 200 · 표 렌더 정상이라 스모크 테스트로는
    잡히지 않았다. 브라우저 없이도 재발을 막기 위해 중괄호 깊이로 검사한다.

    파이썬 테스트가 JS 를 실행할 수는 없으므로(stdlib-only·에어갭) 완전한
    스코프 분석이 아니다. `X(all)` 호출 지점까지 가는 길에 선언 시점보다 깊이가
    얕아지는 구간이 있으면(= 선언 블록을 벗어났으면) 실패시킨다.
    """

    ROOT = os.path.dirname(os.path.abspath(__file__))
    TPL = ("app", "web", "templates", "dashboard.html")

    def test_all_is_in_scope_at_every_call_site(self):
        with open(os.path.join(self.ROOT, *self.TPL), encoding="utf-8") as f:
            lines = f.read().splitlines()

        decl = [i for i, l in enumerate(lines)
                if re.match(r"\s*const all\s*=", l)]
        self.assertEqual(len(decl), 1,
                         "render() 의 `const all` 선언을 정확히 1개 찾지 못함 "
                         "(이름이 바뀌었으면 이 테스트도 함께 고칠 것)")
        decl = decl[0]

        depth = 0
        for l in lines[:decl + 1]:
            depth += l.count("{") - l.count("}")

        calls, cur, lowest = [], depth, depth
        for i in range(decl + 1, len(lines)):
            cur += lines[i].count("{") - lines[i].count("}")
            if re.search(r"\b\w*Map\(all\)", lines[i]):
                calls.append((i + 1, lines[i].strip(), lowest))
            lowest = min(lowest, cur)

        self.assertTrue(calls, "sharedMap(all)/pausedMap(all) 호출을 찾지 못함 "
                               "— 검사가 무력화됐는지 확인할 것")
        for lineno, src, low in calls:
            self.assertGreaterEqual(
                low, depth,
                "%d행 `%s` 이 `const all` 선언 블록 밖이다 — 그래프가 "
                "ReferenceError 로 죽고 폴링 try/catch 가 '수집 실패' 로 "
                "위장한다. `all` 을 함수 스코프로 올릴 것." % (lineno, src))


class TestVersionConsistency(unittest.TestCase):
    """버전 문자열이 여러 파일에 흩어져 있고(단일 출처 __version__ 에 자동 연동되지
    않는 수동 복제본), 릴리스 때 한 곳을 빠뜨리기 쉽다 — 실제로 deploy/k8s.yaml 의
    버전 라벨을 빼먹은 적이 있다. app/__init__.py 의 __version__ 를 기준으로 README
    헤더와 deploy/k8s.yaml 라벨이 모두 일치하는지 강제해 재발을 막는다.

    새 버전 표기 위치를 추가하면(예: 또 다른 매니페스트) 여기 검사도 함께 늘릴 것.
    """

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_readme_header_matches_version(self):
        mt = re.search(r"\*\*버전:\s*v([0-9]+\.[0-9]+\.[0-9]+)\*\*",
                       self._read("README.md"))
        self.assertIsNotNone(mt, "README 헤더에서 '**버전: vX.Y.Z**' 패턴을 찾지 못함")
        self.assertEqual(mt.group(1), app.__version__,
                         "README 헤더 버전이 __version__ 과 불일치")

    def test_k8s_manifest_version_labels_match(self):
        labels = re.findall(r'app\.kubernetes\.io/version:\s*"([^"]+)"',
                            self._read("deploy", "k8s.yaml"))
        self.assertTrue(labels,
                        "deploy/k8s.yaml 에 app.kubernetes.io/version 라벨이 없음")
        for v in labels:
            self.assertEqual(v, app.__version__,
                             "deploy/k8s.yaml 의 버전 라벨이 __version__ 과 불일치")


if __name__ == "__main__":
    unittest.main(verbosity=2)

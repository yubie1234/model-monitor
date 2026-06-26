#!/usr/bin/env python3
"""model_monitor 단위 테스트 — 외부 패키지 0개(표준 라이브러리 unittest)만 사용.

실행:  python3 -m unittest -v        (또는)  python3 test_model_monitor.py

가장 까다롭고 KServe/Knative 버전에 민감한 파싱·병합·집계 로직을 고정한다.
"""

import copy
import unittest

import model_monitor as m


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

    def test_external_api_base_short_circuits(self):
        client = FakeClient([])
        dep = {"api_base": "http://50.50.65.54:8000/v1"}
        out = m.resolve_backend_count(dep, client, SETTINGS)
        self.assertEqual(out["backend_source"], "external")
        self.assertEqual(client.calls, [])           # k8s 호출 안 함

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


class TestCollectUserAccess(unittest.TestCase):
    """per-user 키 접근 수집 — http_get_json 을 가짜로 갈아끼워 분기만 고정."""

    def _patch(self, fake):
        self._orig = m.http_get_json
        m.http_get_json = fake

    def tearDown(self):
        if getattr(self, "_orig", None):
            m.http_get_json = self._orig

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
    """키별 접근 캐시 — 폴링 중복 호출 제거, 성공만 캐시, TTL 만료."""

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

    def test_failure_not_cached(self):
        cache = m.AccessCache(ttl=30.0)
        calls = {"n": 0}
        def collect():
            calls["n"] += 1
            return {"ok": False, "error": "401"}
        cache.get_or_collect("sk-bad", collect, now=100.0)
        cache.get_or_collect("sk-bad", collect, now=101.0)      # 실패는 매번 재검증
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
                     "backends_ready": 2, "backends_desired": 2,
                     "backend_source": "deployment"},
                    {"model_name": "secret-y", "api_base": "http://internal-b/v1",
                     "type": "vllm", "status": "DOWN",
                     "backends_ready": 0, "backends_desired": 1,
                     "backend_source": "deployment"},
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

    def test_no_api_base_label_leak(self):
        # 내부 URL(api_base)은 메트릭 라벨에 노출되면 안 된다(카디널리티/보안).
        text = m.render_prometheus_metrics(self._snap())
        self.assertNotIn("api_base", text)
        self.assertNotIn("http://a/v1", text)

    def test_loading_snapshot_reports_down(self):
        text = m.render_prometheus_metrics({"loading": True, "version": "x"})
        self.assertIn("model_monitor_up 0", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

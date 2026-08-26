#!/usr/bin/env python3
"""model_monitor 단위 테스트 — 외부 패키지 0개(표준 라이브러리 unittest)만 사용.

실행:  python3 -m unittest -v        (또는)  python3 test_model_monitor.py

가장 까다롭고 KServe/Knative 버전에 민감한 파싱·병합·집계 로직을 고정한다.
"""

import unittest
from datetime import datetime

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


class TestUsageParsers(unittest.TestCase):
    """LiteLLM 분석 엔드포인트 응답 -> {model: {requests, tokens, spend}} 정규화."""

    def test_activity_model_toplevel_totals(self):
        data = [{"model": "A", "total_requests": 10, "total_tokens": 100},
                {"model_group": "B", "api_requests": 5, "sum_total_tokens": 50}]
        out = m._usage_from_activity_model(data)
        self.assertEqual(out["A"]["requests"], 10)
        self.assertEqual(out["A"]["tokens"], 100)
        self.assertEqual(out["B"]["requests"], 5)
        self.assertEqual(out["B"]["tokens"], 50)

    def test_activity_model_sums_daily_data_when_no_totals(self):
        # 상단 합계가 없는 버전 -> daily_data 합산으로 폴백
        data = {"data": [{"model": "A", "daily_data": [
            {"date": "2026-08-25", "api_requests": 3, "total_tokens": 30},
            {"date": "2026-08-26", "api_requests": 4, "total_tokens": 40}]}]}
        out = m._usage_from_activity_model(data)
        self.assertEqual(out["A"]["requests"], 7)
        self.assertEqual(out["A"]["tokens"], 70)

    def test_daily_activity_breakdown(self):
        data = {"results": [
            {"date": "2026-08-25", "breakdown": {"models": {
                "A": {"metrics": {"api_requests": 2, "total_tokens": 20, "spend": 0.5}}}}},
            {"date": "2026-08-26", "breakdown": {"models": {
                "A": {"metrics": {"api_requests": 3, "total_tokens": 30, "spend": 0.25}},
                "B": {"metrics": {"api_requests": 1, "total_tokens": 10}}}}},
        ]}
        out = m._usage_from_daily_activity(data)
        self.assertEqual(out["A"]["requests"], 5)     # 날짜별 합산
        self.assertEqual(out["A"]["tokens"], 50)
        self.assertAlmostEqual(out["A"]["spend"], 0.75)
        self.assertEqual(out["B"]["requests"], 1)
        self.assertIsNone(out["B"]["spend"])          # 없는 값은 0 이 아니라 None

    def test_model_metrics_needs_request_count(self):
        data = {"data": [{"model": "A", "num_requests": 9},
                         {"model": "B", "avg_latency_seconds": 1.2}]}  # 요청 수 없음
        out = m._usage_from_model_metrics(data)
        self.assertEqual(out["A"]["requests"], 9)
        self.assertNotIn("B", out)

    def test_garbage_shapes_do_not_raise(self):
        for bad in (None, "nope", {"data": "x"}, [1, 2], {"results": {}}):
            self.assertEqual(m._usage_from_activity_model(bad), {})
            self.assertEqual(m._usage_from_daily_activity(bad), {})
            self.assertEqual(m._usage_from_model_metrics(bad), {})


class TestCollectUsage(unittest.TestCase):
    """후보 엔드포인트를 순서대로 시도하고 처음 성공한 응답만 쓴다."""

    def _fake_http(self, routes):
        calls = []

        def fake(url, api_key=None, timeout=10):
            calls.append(url)
            for substr, resp in routes:
                if substr in url:
                    return resp
            return (False, None, "HTTP 404 Not Found")
        return fake, calls

    def test_falls_through_to_next_endpoint(self):
        fake, calls = self._fake_http([
            # 1순위는 404, 2순위(신형 daily activity)에서 데이터가 나온다
            ("/global/daily/activity", (True, {"results": [{"breakdown": {"models": {
                "A": {"metrics": {"api_requests": 120, "total_tokens": 6000}}}}}]}, None)),
        ])
        orig = m.http_get_json
        m.http_get_json = fake
        try:
            u = m.collect_usage("http://litellm:4000", "sk-x", 5, window_hours=2.0,
                                now=datetime(2026, 8, 26, 12, 0, 0))
        finally:
            m.http_get_json = orig
        self.assertEqual(u["source"], "/global/daily/activity")
        self.assertEqual(u["models"]["A"]["requests"], 120)
        self.assertEqual(u["totals"]["requests"], 120)
        self.assertEqual(u["totals"]["models_used"], 1)
        self.assertTrue(any("/global/activity/model" in c for c in calls))  # 1순위 시도함
        self.assertFalse(any("/model/metrics" in c for c in calls))         # 성공 후 중단
        # 날짜 단위 소스는 그 날 00:00 부터 커버 -> 분모가 window(2h)보다 크다
        self.assertGreater(u["window_minutes"], 120)
        self.assertAlmostEqual(u["models"]["A"]["requests_per_min"],
                               round(120 / u["window_minutes"], 3))

    def test_all_endpoints_fail_leaves_usage_empty(self):
        fake, _ = self._fake_http([])
        orig = m.http_get_json
        m.http_get_json = fake
        try:
            u = m.collect_usage("http://litellm:4000", None, 5,
                                now=datetime(2026, 8, 26, 12, 0, 0))
        finally:
            m.http_get_json = orig
        self.assertIsNone(u["source"])          # 값을 지어내지 않는다
        self.assertEqual(u["models"], {})
        self.assertEqual(len(u["errors"]), 3)


class TestAttachUsage(unittest.TestCase):
    def test_join_by_model_name_and_underlying(self):
        ll = {
            "groups": [{"model_group": "A", "rpm": 60, "tpm": 6000}],
            "deployments": [
                {"model_name": "A", "api_base": "http://a/v1"},
                {"model_name": "B", "underlying": "hosted_vllm/b-raw",
                 "api_base": "http://b/v1"},
                {"model_name": "C", "api_base": "http://c/v1"},   # 사용량 없음
            ],
        }
        usage = {"models": {
            "A": {"requests": 600, "tokens": 60000, "requests_per_min": 30.0,
                  "tokens_per_min": 3000.0},
            "b-raw": {"requests": 10, "requests_per_min": 0.5},
        }}
        out = {d["model_name"]: d for d in m.attach_usage_to_deployments(ll, usage)}
        self.assertEqual(out["A"]["usage"]["requests"], 600)
        self.assertEqual(out["A"]["usage"]["rpm_limit"], 60)
        self.assertAlmostEqual(out["A"]["usage"]["rpm_util"], 0.5)     # 30/60
        self.assertAlmostEqual(out["A"]["usage"]["tpm_util"], 0.5)     # 3000/6000
        self.assertEqual(out["B"]["usage"]["requests"], 10)            # underlying 로 매칭
        self.assertNotIn("rpm_limit", out["B"]["usage"])               # 한도 없음
        self.assertNotIn("usage", out["C"])                            # 없으면 안 붙인다

    def test_no_usage_returns_deployments_untouched(self):
        ll = {"deployments": [{"model_name": "A"}]}
        self.assertEqual(m.attach_usage_to_deployments(ll, {"models": {}}),
                         ll["deployments"])

    def test_summary_does_not_double_count_replicas(self):
        # 같은 model_name 의 deployment 가 2개여도 요청 수 합계는 1번만 센다
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "A", "api_base": "http://a1/v1", "backends_ready": 1,
             "backend_source": "deployment"},
            {"model_name": "A", "api_base": "http://a2/v1", "backends_ready": 1,
             "backend_source": "deployment"},
        ]}
        usage = {"source": "/global/activity/model",
                 "window_hours": 24.0,
                 "models": {"A": {"requests": 100, "tokens": 1000,
                                  "requests_per_min": 1.0}},
                 "totals": {"requests": 100, "tokens": 1000, "spend": 0.0,
                            "requests_per_min": 1.0, "models_used": 1}}
        ll["deployments"] = m.merge_deployments_with_health(ll)
        ll["deployments"] = m.attach_usage_to_deployments(ll, usage)
        s = m.summarize({"litellm": ll, "backends": [], "usage": usage})
        self.assertTrue(s["usage_known"])
        self.assertEqual(s["usage_requests"], 100)    # 200 이 되면 안 된다
        self.assertEqual(s["usage_window_hours"], 24.0)


class TestLiveMetrics(unittest.TestCase):
    VLLM = """# HELP vllm:num_requests_running Number of requests running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="qwen"} 5.0
vllm:num_requests_waiting{model_name="qwen"} 2.0
vllm:gpu_cache_usage_perc{model_name="qwen"} 0.734
vllm:request_success_total{model_name="qwen"} 1234
"""
    SGLANG = """sglang:num_running_reqs{model_name="q"} 3.0
sglang:num_queue_reqs{model_name="q"} 0.0
sglang:token_usage{model_name="q"} 0.51
"""

    def test_parse_prom_metrics_labels_and_bare(self):
        out = m.parse_prom_metrics('a{x="1"} 2.5\nb 7\n# comment\n\nbad_line\n')
        self.assertEqual(out["a"], [2.5])
        self.assertEqual(out["b"], [7.0])
        self.assertNotIn("bad_line", out)

    def test_live_from_prom_vllm(self):
        live = m.live_from_prom(self.VLLM)
        self.assertEqual(live["engine"], "vllm")
        self.assertEqual(live["running"], 5)
        self.assertEqual(live["waiting"], 2)
        self.assertEqual(live["kv_cache_pct"], 73.4)   # 0~1 비율 -> %

    def test_live_from_prom_sglang(self):
        live = m.live_from_prom(self.SGLANG)
        self.assertEqual(live["engine"], "sglang")
        self.assertEqual(live["running"], 3)
        self.assertEqual(live["waiting"], 0)
        self.assertEqual(live["kv_cache_pct"], 51.0)

    def test_live_from_prom_sums_multiple_label_sets(self):
        live = m.live_from_prom(
            'vllm:num_requests_running{model_name="a"} 2\n'
            'vllm:num_requests_running{model_name="b"} 3\n')
        self.assertEqual(live["running"], 5)

    def test_unknown_metrics_return_none(self):
        self.assertIsNone(m.live_from_prom("go_gc_duration_seconds 0.1\n"))
        self.assertIsNone(m.live_from_prom(""))

    def test_attach_live_joins_on_stripped_api_base(self):
        deps = [{"model_name": "A", "api_base": "http://a.ns.svc:8080/v1"},
                {"model_name": "B", "api_base": "http://b.ns.svc:8080/openai/v1"},
                {"model_name": "C", "api_base": None}]
        live = {"http://a.ns.svc:8080": {"engine": "vllm", "running": 1,
                                         "waiting": 0, "kv_cache_pct": 10.0}}
        out = {d["model_name"]: d for d in m.attach_live_to_deployments(deps, live)}
        self.assertEqual(out["A"]["live"]["running"], 1)
        self.assertNotIn("live", out["B"])
        self.assertNotIn("live", out["C"])

    def test_summary_counts_shared_lb_once(self):
        live = {"engine": "vllm", "running": 4, "waiting": 1,
                "kv_cache_pct": 20.0, "url": "http://a/metrics"}
        ll = {"groups": [], "health": None, "deployments": [
            {"model_name": "A", "api_base": "http://a/v1", "live": live},
            {"model_name": "B", "api_base": "http://a/v1", "live": live},
        ]}
        s = m.summarize({"litellm": ll, "backends": []})
        self.assertTrue(s["live_known"])
        self.assertEqual(s["live_running"], 4)   # 같은 LB -> 한 번만
        self.assertEqual(s["live_waiting"], 1)


class TestDemoUsage(unittest.TestCase):
    def test_demo_usage_totals_match_rows(self):
        snap = m.demo_snapshot()
        s = snap["summary"]
        self.assertTrue(s["usage_known"])
        self.assertEqual(s["usage_requests"],
                         sum(v["requests"] for v in snap["usage"]["models"].values()))
        self.assertTrue(s["live_known"])
        # 표 렌더가 죽지 않는지(포맷터 경로 전체) 확인
        out = m.render(snap, {"probe_backends": False})
        self.assertIn("REQ/24H", out)
        self.assertIn("IN-FLIGHT", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

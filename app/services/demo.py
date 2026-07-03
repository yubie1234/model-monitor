"""--demo 용 샘플 스냅샷 (라이브 엔드포인트 없이 대시보드 미리보기)."""

from datetime import datetime

from app import __version__
from app.services.snapshot import merge_deployments_with_health, summarize


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
                 "network_type": "kserve", "backend_type": "vllm",
                 "backend_type_source": "pod",
                 "backends_ready": 3, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "kserve",
                 "service": "qwen36-35b-predictor",
                 "gpu_ready": 6, "gpu_products": {"H100": 6}},
                {"model_name": "SGlang-Qwen3.6-27B-FP8",
                 "underlying": "hosted_vllm/Qwen3.6-27B-FP8",
                 "api_base": "http://qwen36-27b-sglang.serving.svc:30000/v1",
                 "id": "d4e5f6", "type": "sglang",
                 "network_type": "service", "backend_type": "sglang",
                 "backend_type_source": "pod",
                 "backends_ready": 1, "backends_desired": 3,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen36-27b-sglang",
                 "gpu_ready": 4, "gpu_products": {"H100": 4}},
                {"model_name": "vLLM-Stack-Qwen3-32B-AWQ",
                 "underlying": "hosted_vllm/Qwen3-32B-AWQ",
                 "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1",
                 "id": "g7h8i9", "type": "vllm",
                 "network_type": "service", "backend_type": "vllm",
                 "backend_type_source": "name",
                 "backends_ready": 0, "backends_desired": 2,
                 "backend_source": "deployment", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "qwen3-32b-vllm",
                 "gpu_ready": 0, "gpu_products": {}},
                {"model_name": "Qwen3-Embedding-8B",
                 "underlying": "openai/Qwen3-Embedding-8B",
                 "api_base": "http://qwen3-embd-predictor.kserve.svc:8080/v1",
                 "id": "j1k2l3", "type": "kserve",
                 "network_type": "kserve", "backend_type": "-",
                 "backend_type_source": "name",
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
                 "network_type": "kserve", "backend_type": "vllm",
                 "backend_type_source": "pod",
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
                 "network_type": "kserve", "backend_type": "vllm",
                 "backend_type_source": "pod",
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
        key=lambda g: (str(g.get("model_group") or "").lower(),
                       str(g.get("model_group") or "")))
    snap["litellm"]["deployments"] = merge_deployments_with_health(snap["litellm"])
    snap["summary"] = summarize(snap)
    snap["demo"] = True
    return snap

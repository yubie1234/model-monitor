"""--demo 용 샘플 스냅샷 (라이브 엔드포인트 없이 대시보드 미리보기)."""

import time
from datetime import datetime

from app import __version__
from app.services.snapshot import merge_deployments_with_health, summarize


def demo_snapshot():
    snap = {
        "version": __version__,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ts_epoch": time.time(),   # build_snapshot 과 동일 — 신선도 판정용
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
                {"model_group": "vLLM-Llama3.3-70B-Instruct",
                 "providers": ["openai"], "mode": "chat"},
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
                 "service": "qwen36-35b-predictor", "blocked": False,
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
                 # GPU 수집 실패 데모: summary.gpu_errors 집계 + 상단 배너로 표면화
                 "gpu_error": "pods: HTTP 403 Forbidden (nodes RBAC 없음)",
                 "gpu_ready": None, "gpu_products": {}},
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
                # 관리자가 LiteLLM 에서 일시중지(model_info.blocked=true)한 모델.
                # 아래 healthy_endpoints 에 이 api_base 가 **그대로 들어 있다** —
                # LiteLLM /health 는 blocked 를 걸러주지 않아 계속 healthy 로
                # 보고하기 때문. Pod 도 2/2 로 멀쩡하다. 그런데 라우팅 풀에는
                # 없어 트래픽은 0 이다. PAUSED 가 바로 이 거짓 정상을 잡는다.
                {"model_name": "vLLM-Llama3.3-70B-Instruct",
                 "underlying": "hosted_vllm/Llama3.3-70B-Instruct",
                 "api_base": "http://llama33-70b-vllm.serving.svc:8000/v1",
                 "id": "m9n8o7", "type": "vllm",
                 "network_type": "service", "backend_type": "vllm",
                 "backend_type_source": "pod",
                 "backends_ready": 2, "backends_desired": 2,
                 "backend_source": "endpointslice", "mode": "RawDeployment",
                 "scale_to_zero": False, "namespace": "serving",
                 "service": "llama33-70b-vllm", "blocked": True,
                 "gpu_ready": 4, "gpu_products": {"H100": 4}},
            ],
            "health": {
                "healthy_count": 5,
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
                    # 일시중지된 백엔드도 /health 는 healthy 로 준다(의도된 재현).
                    {"model": "hosted_vllm/Llama3.3-70B-Instruct",
                     "api_base": "http://llama33-70b-vllm.serving.svc:8000/v1"},
                ],
                "unhealthy_endpoints": [
                    {"model": "hosted_vllm/Qwen3-32B-AWQ",
                     "api_base": "http://qwen3-32b-vllm.serving.svc:8000/v1",
                     # DOWN 사유 표면화 데모: 첫 줄만 status_detail 로, 'connection'
                     # 카테고리로 정규화된다(뒤 스택트레이스는 잘려 나감).
                     "error": "litellm.InternalServerError: OpenAIException - "
                              "Connection error.\nstack trace: Traceback (most "
                              "recent call last): ...",
                     "exception_status": "500"},
                ],
            },
            # /v1/models 에 vLLM-Llama3.3-70B-Instruct 가 **없는 것이 정상**이다 —
            # LiteLLM 은 모든 deployment 가 blocked 인 이름을 /v1/models 에서
            # 숨긴다(get_fully_blocked_model_names). 그래서 일시중지 판별은
            # /model/info 로만 가능하다.
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

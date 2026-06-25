# model-monitor

LiteLLM → KServe → vLLM/SGLang 백엔드에서 **실제로 떠 있는 모델 현황**과 **각 api_base(LB) 뒤에 떠 있는 backend Pod 개수**를 보여주는 모니터. 터미널(TUI)과 웹 대시보드(`--serve`)를 모두 제공합니다.

외부 패키지 없이 **Python 3.6+ 표준 라이브러리만** 사용합니다. air-gapped 노드에서 `pip install` 없이 `model_monitor.py` 한 파일만 있으면 실행됩니다.

## 무엇을 보여주나

| 지표 | 출처 |
|------|------|
| model groups | LiteLLM `GET /model_group/info` |
| **model_name → api_base 매핑** | LiteLLM `GET /model/info` (api_base 평문, admin 키 권장) |
| running(healthy) / unhealthy | LiteLLM `GET /health` (api_base 기준으로 위 매핑과 join) |
| **LB 뒤 backend Pod 개수 (ready/desired)** | Kubernetes API (EndpointSlice / Knative PodAutoscaler / Deployment) |
| OpenAI 호환 모델 이름 목록 | LiteLLM `GET /v1/models` (이름만, **api_base 없음**) |
| backends up (옵션) | 각 백엔드 `GET /v1/models`, `/health` 직접 probe |

> 주의: `/v1/models` 는 OpenAI 호환 스펙이라 `id`(model_name)만 줍니다. **api_base 는 `/model/info` 에서** 나옵니다.

## LB 뒤 backend Pod 개수 (핵심)

`api_base` 는 k8s Service(=LB) 주소라서, 거기에 `/v1/models` 를 찔러봐야 "LB 살아있음"만 알 뿐
**그 뒤에 실제 backend Pod 가 몇 개 떠서 서빙 중인지**는 알 수 없습니다. 그 개수는 LB 가 아니라
**개별 Pod 를 볼 수 있는 컨트롤 플레인 소스**에서 나와야 하며, 다음 우선순위로 산출합니다:

1. **EndpointSlice** (`discovery.k8s.io/v1`, `kubernetes.io/service-name=<svc>`) — ready 엔드포인트 수 = LB 가 실제 라우팅하는 Pod 수. activator Pod 는 제외. *(가장 직접적·실시간)*
2. **Knative PodAutoscaler** `status.actualScale` — KServe Serverless 에서 EndpointSlice 가 비거나 activator 만 가리킬 때 보정 (scale-to-zero 면 0).
3. **Deployment** `status.readyReplicas` / `spec.replicas` — RawDeployment desired 보강.
4. 전부 실패하면 `?` (추측값을 넣지 않음).

표기: `ready/desired` (예: `2/2` 초록, `1/3` 노랑=미달, `0/2` 빨강, `0 (scaled-to-zero)` 노랑=정상 idle).
`/health` 가 UP 인데 backend `0` 이면 `via activator?` 로 cold-start 대기를 드러냅니다.

> in-cluster 에서 ServiceAccount 토큰이 있으면 **자동으로 켜집니다**(`--no-backend-count` 로 끔).
> 필요한 RBAC 는 [deploy/k8s.yaml](deploy/k8s.yaml) 의 ClusterRole 참고 — 최소한 `endpointslices` 읽기.

## 사용법

```bash
# 1회 스냅샷
python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234

# 실시간 watch (5초 주기)
python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234 --watch

# JSON (다른 도구로 파이프)
python3 model_monitor.py --litellm-url http://litellm:4000 --api-key sk-1234 --json

# 설정 파일 + 백엔드 직접 probe
python3 model_monitor.py --config config.yaml --probe-backends --watch

# 웹 대시보드 (브라우저로 조회, 5초 자동 갱신)
python3 model_monitor.py --config config.yaml --serve --port 8088
#   -> http://localhost:8088  (/api/snapshot 으로 JSON 도 제공)

# 라이브 엔드포인트 없이 출력 미리보기 (TUI / 웹 둘 다 가능)
python3 model_monitor.py --demo
python3 model_monitor.py --demo --watch
python3 model_monitor.py --demo --serve --port 8088
```

### 설정 우선순위
CLI 인자 > 환경변수(`LITELLM_BASE_URL`, `LITELLM_API_KEY`) > config 파일

설정 파일은 `.json`(표준 라이브러리로 항상 동작) 또는 `.yaml`(노드에 PyYAML 있을 때)을 지원합니다. 예시는 [config.example.json](config.example.json) / [config.example.yaml](config.example.yaml).

### 백엔드 주소는 어떻게 알아내나
백엔드(vLLM/SGLang) 주소를 **사람이 적을 필요가 없습니다.** 주소의 원천은 LiteLLM 설정의
`model_list[].litellm_params.api_base` 이고, LiteLLM `GET /model/info` 응답이 각 deployment의
`api_base` 를 **평문 그대로** 돌려줍니다 (마스킹은 `api_key`·credentials 만 제거). 따라서:

- 모니터는 `/model/info` 로 `model_name → api_base` 매핑을 얻고, `/health` 로 살아있는지
  판정해 둘을 api_base 기준으로 합칩니다 → `[Deployments]` 테이블.
- `--probe-backends` 를 켜도 `config.backends` 를 비워두면 위 api_base 들에서 probe 대상을
  **자동 발견**합니다. `config.backends` 는 LiteLLM 을 거치지 않고 특정 주소만 따로
  찍어보고 싶을 때만 수동으로 적으면 됩니다.

> `/model/info` 의 api_base 는 admin 권한 키여야 보입니다. 권한이 없으면 `[Deployments]`
> 의 STATUS 는 `?` 로 표시되고, api_base 없이 이름만 나옵니다.

## 옵션

| 옵션 | 설명 |
|------|------|
| `--litellm-url` | LiteLLM 게이트웨이 URL |
| `--api-key` | LiteLLM API key (admin 권장 — `/health` 조회) |
| `--config` | 설정 파일 경로 |
| `--probe-backends` | config 의 backends 를 직접 probe |
| `--watch` / `--interval N` | 실시간 갱신 / 주기(초, 기본 5) |
| `--serve` / `--host` / `--port` | 웹 대시보드 (기본 0.0.0.0:8088) |
| `--json` | JSON 출력 |
| `--timeout N` | HTTP 타임아웃(초, 기본 10) |
| `--demo` | 샘플 데이터로 미리보기 |
| `--no-backend-count` | LB 뒤 backend Pod 개수 수집 끄기 |
| `--k8s-api-server` / `--k8s-token-file` / `--k8s-ca-file` | k8s 접근 오버라이드 |
| `--k8s-insecure` | k8s API TLS 검증 비활성 |

## 운영 배포

in-cluster 로 배포하면 backend Pod 개수 수집이 자동으로 켜집니다(ServiceAccount 토큰 사용).

- **매니페스트**: [deploy/k8s.yaml](deploy/k8s.yaml) — Namespace / ServiceAccount / **ClusterRole(RBAC)** / ConfigMap / Deployment / Service. 이미지 없이 `model_monitor.py` 를 ConfigMap 으로 주입하는 방식이 기본:
  ```bash
  kubectl create namespace model-monitor
  kubectl -n model-monitor create configmap model-monitor-src --from-file=model_monitor.py
  kubectl apply -f deploy/k8s.yaml
  ```
- **컨테이너 이미지 방식**(이 레포의 다른 컴포넌트처럼 오프라인 반입):
  ```dockerfile
  FROM python:3.12-slim
  COPY model_monitor.py /app/model_monitor.py
  ENTRYPOINT ["python3", "/app/model_monitor.py"]
  ```
  빌드 후 `docker save ... -o model-monitor-image.tar` 로 묶어 다른 `*-container-images.tar` 와 동일하게 반입.

### scale-to-zero / Knative 참고
KServe Serverless 는 scale-to-zero 시 Service/EndpointSlice 가 activator 를 가리켜 실제 모델 Pod 수를 왜곡합니다.
이 경우 Knative PodAutoscaler `actualScale` 로 보정해 `0 (scaled-to-zero)` 로 명확히 표기합니다(장애 아님).
PodAutoscaler 는 internal API 라 RBAC 권한이 더 필요하며, 권한이 없으면 `?`/`via activator?` 로 솔직하게 표기합니다.
InferenceService CRD `status` 에는 replica 개수 필드가 없어 개수 산출에는 쓰지 않고 mode/revision 감지에만 사용합니다.

# model-monitor

**버전: v1.0.4** — KServe 판별을 svc 네이밍 규약(`-predictor`) 기반으로 전환 + sibling 교차 제외(GLM 등 external IP 백엔드 체크 편입)

LiteLLM → KServe → vLLM/SGLang 백엔드에서 **실제로 떠 있는 모델 현황**과 **각 api_base(LB) 뒤에 떠 있는 backend Pod 개수**를 보여주는 **FastAPI 서비스**. 웹 대시보드(`/`)와 JSON API(`/api/snapshot`), Prometheus 메트릭(`/metrics`)을 제공합니다.

수집 계층(LiteLLM·Kubernetes 조회)은 여전히 **Python 표준 라이브러리(`urllib`/`ssl`)만** 사용하고, 웹 계층만 FastAPI 스택(`fastapi`/`uvicorn`/`pydantic`)을 씁니다. 의존성은 [requirements.txt](requirements.txt) 참고 — air-gapped 노드에 이미지를 통째로 배포합니다.

> 버전 확인: `/api/snapshot` 의 `version` 필드, FastAPI `/docs`, 대시보드 헤더에 표시됩니다. 단일 진실 공급원은 [app/__init__.py](app/__init__.py) 의 `__version__`.

## 무엇을 보여주나

| 지표 | 출처 |
|------|------|
| model groups | LiteLLM `GET /model_group/info` |
| **model_name → api_base 매핑** | LiteLLM `GET /model/info` (api_base 평문, admin 키 권장) |
| running(healthy) / unhealthy | LiteLLM `GET /health` (api_base 기준으로 위 매핑과 join) |
| **DOWN 사유 (연결실패/타임아웃/5xx …)** | `/health` unhealthy endpoint 의 `error` 를 소수 카테고리로 정규화 — DOWN pill ⚠ 툴팁에 표시 |
| **LB 뒤 backend Pod 개수 (ready/desired)** | Kubernetes API (EndpointSlice / Knative PodAutoscaler / Deployment) |
| **GPU 개수 + 장치명 (H100/B200 …)** | Pod `resources.limits[nvidia.com/gpu]` + 노드 라벨 `nvidia.com/gpu.product` |
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

> **모델 기준 그룹 뷰 + Model↔Backend 그래프 (웹, v0.1.4)** — 한 model_name 이 여러 백엔드를
> 가지거나(로드밸런싱) 여러 model_name 이 한 백엔드(api_base=Service)를 공유할 수 있습니다.
> 웹 대시보드는 deployment 를 model_name 으로 묶어 모델별 합성 상태와 `Σ ready/desired` 를
> 보여주고(`group by model` 토글), 공유 백엔드는 `⇄ SHARED` 로 명시합니다. 상단 **Model ↔ Backend**
> 그래프(`show graph` 토글)는 라우팅을 이분 그래프로 그려 공유 백엔드로 간선이 모이는 모습을
> 한눈에 보여줍니다. 노드에 마우스를 올리면 **연결된 노드를 그 옆으로 끌어모으고 나머지는 숨겨**(v0.5.4)
> 멀리 떨어져 있어도 무엇과 이어졌는지 바로 보입니다(간선도 모인 위치로 다시 그림). **노드를 클릭하면 그 배치를 고정(pin)해 얼려두어**, 끌어모은 노드 위로 마우스를 올려 값(이름·replicas·GPU tooltip)을 읽어도 배치가 흐트러지지 않습니다(v0.5.6 — 예전엔 hover 가 고정을 가로채 풀리는 것처럼 보였음). 고정은 5초 자동 갱신·필터 변경·브라우저 새로고침에도 유지됩니다(v0.5.10 — 배경 클릭/재클릭으로만 해제, 탭 닫으면 소멸). 헤드라인 **Replicas** 합계는 `(namespace, service)` 기준으로 **dedup** 하므로
> 공유 백엔드가 모델 수만큼 이중 집계되지 않습니다. (UI 라벨은 `Replicas`/`REPLICAS` — k8s Pod=서빙 복제본.)

> **TYPE 2축 분리 (v0.5.9)** — 기존 단일 TYPE(vllm/sglang/kserve 혼합, 첫 매칭이 나머지를
> 가림)을 **네트워크 타입**과 **백엔드(엔진) 타입**으로 분리했습니다.
> - `network_type` = `kserve`(KServe 기반 배포) / `service`(단순 k8s Service·LB) /
>   `external`(클러스터 밖) / `-`(판정 불가) — 문자열 추측이 아니라 **ISVC 조회 성공 여부라는
>   k8s 사실**로 판정합니다(RBAC 등으로 조회가 불확실하면 단정하지 않고 `-`).
> - `backend_type` = `vllm` / `sglang` / `-` — **Pod 컨테이너의 image/command** 로 판별하고
>   (GPU 수집이 이미 받아온 Pod 재활용, 추가 API 호출 0; `backend_type_source: "pod"`),
>   Pod 를 못 보는 경우(GPU 수집 OFF/외부/scale-to-zero)는 이름 휴리스틱으로 폴백합니다
>   (`backend_type_source: "name"`).
> - 대시보드 TYPE 칸은 net/engine 칩 2개로, 필터도 `net`/`engine` 2개로 나뉩니다. 모델 그룹
>   행에서 자식들의 값이 서로 다르면 `mixed` 로 표기합니다. 기존 `type` 필드는 API 호환용으로
>   유지되지만 **deprecated** 입니다.

> **GPU 개수 + 장치명 (v0.4.0)** — backend Pod 가 점유한 GPU 수(`resources.limits[nvidia.com/gpu]`
> 합)와 장치 모델명을 함께 보여줍니다. 장치명은 Pod 가 뜬 **노드의 라벨 `nvidia.com/gpu.product`**
> (NVIDIA GPU Operator / GPU Feature Discovery 가 부착)에서 얻어 `H100`·`B200` 처럼 축약 표시합니다.
> 한 모델의 replica 가 **서로 다른 GPU 로 섞여**(예: H100 풀 + B200 풀) 구성될 수 있고, 이 경우
> 장치별 색 **칩**으로 `H100×4` `B200×2` 처럼 나눠 보여줍니다(단일 장치면 칩 하나). 헤드라인 GPU
> 카드는 장치 비율 **세그먼트 바 + 범례**로 믹스를 드러냅니다. GPU 없음/idle 은 `-`, 조회 실패는 `?`.
> 헤드라인 GPU 총합도 `(namespace, service)` 기준 dedup. 기본 ON(`MONITOR_GPU_INFO=false` 로 끔). 멀티노드 GPU 미지원.

> in-cluster 에서 ServiceAccount 토큰이 있으면 **자동으로 켜집니다**(`MONITOR_BACKEND_COUNT=false` 로 끔).
> 필요한 RBAC 는 [deploy/k8s.yaml](deploy/k8s.yaml) 의 ClusterRole 참고 — 최소한 `endpointslices` 읽기,
> GPU 정보까지 보려면 `pods`·`nodes` 읽기.

## 사용법

```bash
# 0) 의존성 설치
python3 -m pip install -r requirements.txt

# 1) 서비스 실행 (LiteLLM 주소/키는 환경변수로)
LITELLM_BASE_URL=http://litellm:4000 LITELLM_API_KEY=sk-1234 \
  uvicorn app.main:app --host 0.0.0.0 --port 8088
#   -> http://localhost:8088          웹 대시보드 (5초 자동 갱신)
#      http://localhost:8088/api/snapshot   라이브 JSON
#      http://localhost:8088/docs           OpenAPI 문서
#      http://localhost:8088/snapshot.json  raw JSON 파일 다운로드(클릭 한 번)
#      http://localhost:8088/snapshot.html  데이터 박제 self-contained 정지 페이지
#      http://localhost:8088/metrics        Prometheus 메트릭(기본 ON, MONITOR_METRICS=false 로 끔)

# 설정 파일로 실행 (backend_count/namespace_overrides/user_view/metrics 등 풍부한 설정)
MONITOR_CONFIG_FILE=config.yaml uvicorn app.main:app --port 8088

# 간편 실행 래퍼 (Settings 의 host/port 사용)
python3 -m app

# 키별(per-user) 뷰 — 켜면 "키 필수 모드": 키를 입력해야 목록이 보인다
#   기본 OFF. 켜기 전에 ① /v1/models 가 키별로 필터되는지 ② TLS 종단 을 확인할 것.
MONITOR_USER_VIEW=true LITELLM_BASE_URL=http://litellm:4000 LITELLM_API_KEY=sk-admin \
  uvicorn app.main:app --port 8088
#   -> 대시보드 상단 "🔑 키로 조회" 바에 키 입력 후 Enter/조회
#   -> 일반 키: 그 키로 볼 수 있는 모델만 (내부 api_base 숨김)
#   -> admin 키(= 구동 시 쓴 LITELLM_API_KEY): 전체 뷰 + 내부 주소 + export 버튼 노출
#   -> 키는 브라우저(sessionStorage)에만, 매 요청 X-LiteLLM-Key 헤더로만 전송(서버 저장·로그 없음)
#   -> 키 무효/만료면 fail-closed (전체 뷰로 폴백하지 않고 에러만 표시)

# 라이브 엔드포인트 없이 미리보기 (샘플 데이터; user-view 는 데모에서 비활성)
MONITOR_DEMO=true uvicorn app.main:app --port 8088
```

### 키별(per-user) 뷰 — `MONITOR_USER_VIEW=true` ("키 필수 모드")

LiteLLM 가상 키마다 접근 가능한 모델이 다릅니다. 이 모드를 켜면 **키를 입력해야만 목록이 보이며**,
입력한 키 기준으로 필터된 뷰를 보여줍니다.

- **데이터 흐름**: 무거운 데이터(상태·Pod 수·`model/info`·`/health`)는 **admin 키로 백그라운드에서
  한 번만 수집해 공유 캐시**에 둡니다(키와 무관). 요청이 오면 이 캐시를 키별로 **필터**만 하므로,
  서로 다른 키로 조회해도 무거운 호출이 중복되지 않습니다.
- **입력 키에 따라**:
  - 일반 키 → 그 키의 `GET /v1/models`(접근 모델 집합)로 필터한 뷰 + "내 키" 카드(spend/budget/limit).
    내부 `api_base`/namespace 는 **숨김**.
  - admin 키(= 모니터 구동 시 쓴 키) → 내부 주소 포함 **전체 뷰** + export(JSON/정지 페이지) 버튼.
- **라우팅 그래프 (v0.5.9)**: per-user 뷰에서도 Model↔Backend 그래프를 **항상** 그립니다.
  Service 이름은 숨긴 채 **익명 식별자 `backend_ref`**(솔트 해시 8자)로 노드/공유(⇄) 토폴로지만
  유지하므로 내부 구조가 노출되지 않습니다(목록이 단순해도 그래프 표시).
- **내 뷰 JSON (v0.5.9, 디버그용)**: 일반 키로 조회 중일 때 상단의 **`🐞 내 뷰 JSON`** 버튼으로
  지금 렌더 중인 per-user 스냅샷(서버 응답 그대로)을 파일로 저장할 수 있습니다 — 문의/디버그에
  첨부. 브라우저 밖에서는 `curl -X POST -H "X-LiteLLM-Key: <내 키>" <base>/api/snapshot/user`
  로 같은 JSON 을 받을 수 있습니다. (admin 전체 export 는 기존처럼 admin 키 전용.)
- **접근 캐시**: 같은 키의 `/v1/models` 결과를 **짧은 TTL(기본 30s)** 캐시해 폴링 중복 호출을 제거
  (해시만 보관, 원문 키 비저장). 성공만 캐시 → 무효 키는 매번 재검증. 취소/만료 키는 최대 TTL 동안 stale.
  config `user_view.cache_ttl` 로 조절.
- **보안**: 키는 헤더(`X-LiteLLM-Key`) 전용(쿼리 금지), 서버 비저장·비로그, 브라우저 sessionStorage
  에만 보관(탭 닫으면 소멸). 무인증 `GET /api/snapshot`·export 는 **잠김**(export 는 admin 키 헤더로만).
  키 검증 실패 시 전체 뷰로 폴백하지 않습니다(fail-closed).
- **⚠️ 전제**: ① LiteLLM 버전/설정에 따라 `/v1/models` 가 키별로 필터되지 않을 수 있으니 **먼저
  확인**(안 되면 켜지 말 것 — 전체 모델 유출) ② 키를 평문 반복 전송하므로 **TLS 뒤에서만** 노출.
  이 때문에 기능은 **기본 OFF** 입니다. (키 필수 모드가 OFF면 기존 열린 global 대시보드 그대로)

### Prometheus 메트릭 — `GET /metrics`

서비스는 **기본으로** `/metrics` 를 노출합니다(text exposition 0.0.4, 끄려면 `MONITOR_METRICS=false`).
요청 경로에서 수집하지 않고 **백그라운드 캐시 스냅샷을 포맷만** 하므로(다른 엔드포인트와 동일) 스크레이프가
수집을 막지 않습니다. 시점 대시보드를 **시계열·알림**으로 확장하는 용도입니다(기존 Prometheus/Grafana 연동).

- **요약 게이지**: `model_monitor_deployments_{total,healthy,unhealthy}`,
  `model_monitor_backend_pods_{ready,desired}_total`(공유 Service 는 1회만 집계),
  `model_monitor_model_groups`, `model_monitor_backend_pods_known`.
- **모델(deployment) 단위**: `model_monitor_model_up`(라벨 `model`/`namespace`/`service`/`status_source`,
  값 **UP=1 · DOWN=0 · 미상/idle=-1**), `model_monitor_model_backend_pods_{ready,desired}`,
  `model_monitor_model_scale_to_zero`(0 Pod 가 정상 idle 인지 장애인지 구분).
- **스크레이프 신뢰도**: `model_monitor_up`, `model_monitor_build_info{version=…}`,
  `model_monitor_backend_count_enabled`, `model_monitor_collect_errors`(>0 이면 일부 Pod 수 부정확),
  `model_monitor_gpu_collect_errors`(>0 이면 일부 GPU 수 부정확),
  `model_monitor_litellm_reachable`(0=최상류 게이트웨이 미도달), `model_monitor_litellm_errors`(수집 경고 수),
  `model_monitor_collect_failing`(1=마지막 수집 실패, 직전 스냅샷 서빙 중),
  `model_monitor_snapshot_timestamp_seconds`/`model_monitor_snapshot_age_seconds`(스냅샷 나이 — 커지면 수집 멈춤).
- 활용 예: `model_monitor_model_up == 0 and model_monitor_model_scale_to_zero == 0` 으로 **"진짜 죽음"만**
  알림(정상 idle 오탐 제거), `model_up == 1 and model_backend_pods_ready == 0` 으로 **LB 는 200인데 뒤에
  Pod 0** 인 함정 탐지, `avg_over_time(model_monitor_model_up[30d])` 로 모델별 가동률 산출.
- **주의**: 한 `model_name` 에 여러 deployment(로드밸런싱)가 있으면 동일 라벨 series 가 중복될 수 있어
  내부적으로 합칩니다(상태는 DOWN 우선). 내부 `api_base` 는 라벨로 노출하지 않습니다(카디널리티·보안).
- **키 필수 모드(`MONITOR_USER_VIEW=true`)** 에선 `/metrics` 도 인증이 필요합니다. 두 가지 방법:
  **① metrics 전용 Bearer 토큰(권장)** — `MONITOR_METRICS_TOKEN`(또는 설정 파일 `metrics.token`)을
  설정하고 Prometheus 는 표준 `authorization`(Bearer) 인증으로 스크레이프합니다. PodMonitor 의
  `authorization.credentials`(Secret 참조)로도 그대로 동작하며, 이 토큰으로는 metrics 만 열리고
  스냅샷/export 는 열리지 않아 admin 키를 Prometheus 에 배포할 필요가 없습니다.
  **② admin 키 헤더(`X-LiteLLM-Key`)** — 기존 방식(임의 헤더를 지원하는 스크레이퍼만 가능).
  활성 PodMonitor + 토큰 Secret 은 [deploy/k8s.yaml](deploy/k8s.yaml), scrape_config
  예시는 [deploy/prometheus-alerts.yaml](deploy/prometheus-alerts.yaml) 참고.

#### Grafana / Prometheus 연동

바로 쓸 수 있는 구성 파일을 함께 제공합니다:

- [deploy/grafana-dashboard.json](deploy/grafana-dashboard.json) — Grafana 대시보드(Import → JSON 붙여넣기
  → Prometheus 데이터소스 선택). 개요 stat, **이상 징후**(진짜 DOWN / LB UP인데 Pod 0 / 수집 신뢰도),
  추세 그래프, 모델별 상태 타임라인, 상세 테이블로 구성. `namespace`/`model` 변수로 필터.
- [deploy/prometheus-alerts.yaml](deploy/prometheus-alerts.yaml) — 스크레이프 설정 예시 + 알림 룰
  (`PrometheusRule`): `ModelDown`(idle 제외), `BackendPodsZeroWhileUp`, `BackendCapacityDegraded`,
  `ModelMonitorDown`, `ModelMonitorCollectErrors`, `ModelMonitorGpuCollectErrors`, `LiteLLMUnreachable`,
  `ModelMonitorSnapshotStale`, `ModelMonitorCollectFailing`.

### 설정 우선순위
환경변수(`LITELLM_BASE_URL`, `LITELLM_API_KEY`, `MONITOR_*`) > config 파일(`MONITOR_CONFIG_FILE`) > 기본값

설정 파일은 `.json`(표준 라이브러리로 항상 동작) 또는 `.yaml`(노드에 PyYAML 있을 때)을 지원하며, `litellm.*`·`backend_count.*`·`backends`·`namespace_overrides`·`user_view.*`·`metrics.*` 같은 중첩 값을 담습니다. 예시는 [config.example.json](config.example.json) / [config.example.yaml](config.example.yaml).

### 백엔드 주소는 어떻게 알아내나
백엔드(vLLM/SGLang) 주소를 **사람이 적을 필요가 없습니다.** 주소의 원천은 LiteLLM 설정의
`model_list[].litellm_params.api_base` 이고, LiteLLM `GET /model/info` 응답이 각 deployment의
`api_base` 를 **평문 그대로** 돌려줍니다 (마스킹은 `api_key`·credentials 만 제거). 따라서:

- 모니터는 `/model/info` 로 `model_name → api_base` 매핑을 얻고, `/health` 로 살아있는지
  판정해 둘을 api_base 기준으로 합칩니다 → `[Deployments]` 테이블.
- `MONITOR_PROBE_BACKENDS=true` 를 켜도 `config.backends` 를 비워두면 위 api_base 들에서 probe 대상을
  **자동 발견**합니다. `config.backends` 는 LiteLLM 을 거치지 않고 특정 주소만 따로
  찍어보고 싶을 때만 수동으로 적으면 됩니다.

> `/model/info` 의 api_base 는 admin 권한 키여야 보입니다. 권한이 없으면 `[Deployments]`
> 의 STATUS 는 `?` 로 표시되고, api_base 없이 이름만 나옵니다.

## 설정 (환경변수 / 설정 파일)

서버 자체(`--host`/`--port`)는 uvicorn 인자로, 나머지 동작 설정은 환경변수/설정 파일로 줍니다.

| 환경변수 | 설명 (기본값) |
|----------|---------------|
| `LITELLM_BASE_URL` | LiteLLM 게이트웨이 URL |
| `LITELLM_API_KEY` | LiteLLM API key (admin 권장; 키 필수 모드의 admin 키) |
| `MONITOR_CONFIG_FILE` | 설정 파일 경로(.json/.yaml) — 중첩 설정 출처 |
| `MONITOR_HOST` / `MONITOR_PORT` | `python -m app` bind (0.0.0.0 / 8088) |
| `MONITOR_INTERVAL` | 스냅샷 갱신 주기 초 (5) |
| `MONITOR_DEMO` | 샘플 데이터 모드 (false) |
| `MONITOR_TIMEOUT` | HTTP 타임아웃 초 (10) |
| `MONITOR_HEALTH` / `MONITOR_HEALTH_TIMEOUT` | 전량 `/health` 사용 / 타임아웃 초 (**false** / 90) — 전량 `/health` 는 LiteLLM 이 모든 백엔드를 실제 ping 해 scale-to-zero 를 깨우므로 기본 off, 필요 시 명시적으로 켠다 |
| `MONITOR_SELECTIVE_HEALTH` | 선택적 health check (**true**) — `MONITOR_HEALTH=false`일 때 안전한 모델만 `/health?model=` 개별 조회(ping 은 LiteLLM 이 대신). **KServe 판별 = svc 네이밍 규약(`-predictor`)**: KServe 는 k8s 가 RawDeployment 로 양성 확인한 경우만 체크, Serverless/scale-to-zero 는 절대 깨우지 않음. 그 외(일반 Service·**external IP 포함**)는 전부 체크 → UP/DOWN. 위험 sibling(같은 underlying 모델/api_base 공유)이 있는 이름은 함께 제외(LiteLLM 의 `?model=` 매칭이 이름보다 넓을 수 있어서). LiteLLM `model_info.active_health_check: true/false` 로 모델별 수동 override(단 Knative 양성 확인은 true 도 무시. bool 또는 "true"/"false" 문자열만 인정) |
| `MONITOR_PROBE_BACKENDS` | 백엔드 직접 probe (false) |
| `MONITOR_BACKEND_COUNT` | LB 뒤 backend Pod 개수 수집 (true) |
| `MONITOR_GPU_INFO` | GPU 개수/장치명 수집 (true; Pod·Node 읽기 권한 필요) |
| `MONITOR_METRICS` | Prometheus `/metrics` (true) |
| `MONITOR_METRICS_TOKEN` | 키 필수 모드에서 `/metrics` 스크레이프용 Bearer 토큰 (미설정=admin 키만) |
| `MONITOR_USER_VIEW` | 키 필수(per-user) 모드 — 키 입력해야 조회, admin 키는 전체 뷰 (false) |
| `MONITOR_USER_VIEW_SHOW_INTERNAL` | per-user 뷰에서 내부 api_base/namespace 도 표시 (false=숨김) |
| `MONITOR_USER_VIEW_CACHE_TTL` | 키별 접근(/v1/models) 캐시 TTL 초 (30) |
| `MONITOR_K8S_API_SERVER` / `MONITOR_K8S_TOKEN_FILE` / `MONITOR_K8S_CA_FILE` | k8s 접근 오버라이드 |
| `MONITOR_K8S_INSECURE` / `MONITOR_K8S_TIMEOUT` | k8s API TLS 검증 비활성 / 타임아웃 초 (false / 5) |

> 중첩 설정(`backends`, `namespace_overrides`, `user_view.*`, `metrics.*` 등)은 `MONITOR_CONFIG_FILE` 가 가리키는 설정 파일에서 받습니다.

## 운영 배포

빌드한 컨테이너 이미지로 배포합니다. in-cluster 로 뜨면 backend Pod 개수 수집이 자동으로 켜집니다(ServiceAccount 토큰 사용).

1. **이미지 빌드** ([ci.sh](ci.sh) → [Dockerfile](Dockerfile)) — 로컬 `ai-tool/model-monitor:<버전>` + `:latest`:
   ```bash
   ./ci.sh                  # product: <버전> + :latest
   BRANCH=develop ./ci.sh   # <버전>-develop (:latest 안 붙임)
   ```
   `BRANCH` 로 브랜치별 이미지 태그를 구분합니다: **미지정/`product` → `<버전>` (+ `:latest`)**, **그 외 값(develop 등) → `<버전>-<BRANCH>` (`:latest` 제외)**. (`TAG=...` 로 직접 지정하면 BRANCH 로직은 무시.)
2. **레지스트리에 push** ([push.sh](push.sh)) — `10.92.20.77:5002` 로 retag 후 push (빌드와 **같은 `BRANCH`** 를 넘길 것):
   ```bash
   ./push.sh                                       # <버전> + latest
   BRANCH=develop ./push.sh                        # <버전>-develop
   ```
3. **배포** ([deploy/k8s.yaml](deploy/k8s.yaml) — Namespace / ServiceAccount / **ClusterRole(RBAC)** / ConfigMap / Deployment / Service):
   ```bash
   # 먼저 deploy/k8s.yaml 의 ConfigMap 에서 LiteLLM url·api_key·namespace 를 실제 값으로 교체
   kubectl apply -f deploy/k8s.yaml
   kubectl -n dashboard port-forward svc/model-monitor 8088:80   # 브라우저로 확인
   ```

### Path prefix 뒤로 노출 (`example.com/service/model-monitor`)

서비스를 루트가 아닌 prefix 경로 뒤에 둘 수 있습니다. [deploy/k8s.yaml](deploy/k8s.yaml) 에 nginx Ingress 가 포함돼 있습니다.

- **Ingress**: prefix(`/service/model-monitor`)를 `rewrite-target` 으로 떼고 앱(루트)에 전달.
- **앱**: `MONITOR_ROOT_PATH=/service/model-monitor` 를 주면 ① FastAPI `root_path`(/docs·OpenAPI 링크가 prefix 포함) ② 대시보드의 자기 호출(fetch `/api/snapshot`, export 링크)에 prefix 를 붙여 — 브라우저가 `example.com/service/model-monitor/...` 로 요청 → Ingress 가 다시 떼어 앱에 전달.
- **두 값(Ingress path prefix·`MONITOR_ROOT_PATH`)은 반드시 동일**해야 합니다. 루트(/)로 쓰려면 `MONITOR_ROOT_PATH` 를 비우고 Ingress 의 prefix/`rewrite-target` 을 제거하세요.
- `deploy/k8s.yaml` 의 `host`(example.com)·`ingressClassName`(nginx) 은 실제 환경 값으로 교체. nginx 외 컨트롤러면 rewrite 문법을 맞게 바꾸세요. probe(`/healthz`·`/readyz`)는 Pod 로 직접 가므로 prefix 영향 없음.

> Deployment 는 `image: 10.92.20.77:5002/ai-tool/model-monitor:latest` + `imagePullPolicy: Always` 라 latest 최신본을 매번 레지스트리에서 받습니다.
> HTTP(비TLS) 레지스트리면 빌드 노드의 docker(`/etc/docker/daemon.json` 의 `insecure-registries`)와 클러스터 노드의 containerd 에 `10.92.20.77:5002` 를 insecure 레지스트리로 등록해야 push/pull 이 됩니다.

### backend 개수 산출 방식 (KServe)
- **KServe ISVC**(api_base 가 `<isvc>-predictor...`): Deployment 를 `serving.kserve.io/inferenceservice=<isvc>`
  라벨로 찾아 `readyReplicas`/`replicas` 를 합산합니다. RawDeployment·Serverless 공통으로 동작하고
  Knative 네이밍/activator 를 몰라도 됩니다. 라벨 매칭이 안 되면 Knative PodAutoscaler `actualScale` 로 보강.
- **일반 Service**(비 KServe): EndpointSlice 의 ready 주소 수.
- scale-to-zero 면 `0 (scaled-to-zero)` 로 표기(장애 아님). 산출 실패 시 `?` 에 마우스를 올리면 원인을 보여줍니다.

### Troubleshooting
- **STATUS 가 전부 `?` + `health: timed out`**: LiteLLM `/health` 는 모든 백엔드를 실제 ping 해서
  모델이 많으면 수십 초 걸립니다(실측 60s 사례 있음). 서비스는 `/health` 를 **별도 asyncio 태스크로
  비동기 수집**하므로 화면이 멈추지 않고, status 는 우선 **k8s backend readiness 로 즉시 판정**한 뒤
  `/health` 가 도착하면 보강합니다. 그래도 부족하면 `MONITOR_HEALTH_TIMEOUT`(기본 90s)을 늘리거나,
  k8s readiness 만으로 충분하면 `MONITOR_HEALTH=false` 로 끄세요. (KServe 모델은 `/health` 없이도 backend
  Pod 가 ready 면 UP 으로 표시됩니다. 단 외부 IP 백엔드는 `/health` 가 있어야 status 가 나옵니다.)
- **backend 가 `?`(원인 보기)**: 셀에 마우스를 올리면 `k8s_error`(예: `deployments(label): no match`,
  `knative: HTTP 403`)가 뜹니다. RBAC([deploy/k8s.yaml](deploy/k8s.yaml) ClusterRole)나 라벨/네임스페이스를 점검하세요.
- 수집은 백그라운드 asyncio 태스크에서 주기적으로 돌고 HTTP 는 마지막 스냅샷을 즉시 반환합니다
  (요청 블로킹 없음).
- **간헐적 502 (nginx ingress 뒤)**: 여러 원인을 함께 막았습니다 — ① per-user(`/api/snapshot/user`)
  조회가 동기 LiteLLM 호출을 이벤트 루프에서 직접 돌려 단일 워커가 멈추던 문제는 `asyncio.to_thread`
  로 해소(v0.5.4), ② uvicorn keep-alive 기본 5s 가 폴링 주기(5s)·nginx upstream keepalive 와
  겹쳐 연결 재사용 시 reset 나던 문제는 `--timeout-keep-alive 75`(>nginx 60s)로 해소(v0.5.5),
  ③ 무효/만료 키가 폴링마다 blocking LiteLLM 왕복을 새로 일으키던 문제는 접근 캐시의
  **네거티브 캐시**(실패도 짧게 캐시)로 해소, ④ `to_thread` 기본 스레드풀이 노드 CPU 기준으로
  과다 생성돼(파드는 수백 m 로 throttle) CPU 를 경합하던 문제는 **executor 상한 고정**(max_workers=8)으로,
  ⑤ 대시보드 `setInterval` 폴링이 겹쳐 쌓이던 문제는 **in-flight 가드 + AbortController** 로 해소.
  여전히 502 면 ingress 의 `proxy-read-timeout`(느린 LiteLLM 대비)·워커 수·파드 CPU limit 을 점검하세요.

## 로드맵 / TODO

- ✅ **사용자(키)별 대시보드** — 1차 구현 완료(`MONITOR_USER_VIEW=true`, 위 "키별 뷰" 절 참고).
  남은 운영 과제(TLS 전제 확인, 라이브 Go/No-Go, per-IP throttle)는 [TODO.md](TODO.md) 참고.
- **(나중) admin 총괄 뷰(B안)** — admin 키로 키별/팀별 접근 모델을 한눈에. [TODO.md](TODO.md) 8.

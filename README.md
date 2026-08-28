# model-monitor

**버전: v1.3.0** — 대시보드를 **상태 / 부하 두 탭으로 분리**("떠 있나"와 "지금 바쁜가"를 갈라 헤드라인 카드 10장 → 6장) · 한 셀에 4조각을 우겨넣던 LOAD 컬럼을 부하 탭의 전용 표로 펼침(등급·처리 중·대기·KV·tok/s·표본) · 나쁜 등급 우선 정렬과 `바쁜 것만` 필터 · 탭을 열지 않아도 보이는 부하 요약 점

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
| **일시중지(PAUSED) 여부** | LiteLLM `GET /model/info` 의 `model_info.blocked` (v1.90.0+) |
| **LB 뒤 backend Pod 개수 (ready/desired)** | Kubernetes API (EndpointSlice / Knative PodAutoscaler / Deployment) |
| **GPU 개수 + 장치명 (H100/B200 …)** | Pod `resources.limits[nvidia.com/gpu]` + 노드 라벨 `nvidia.com/gpu.product` |
| OpenAI 호환 모델 이름 목록 | `/model/info` 의 model_name 에서 유도 — 별도 `/v1/models` 호출 없음(per-user 뷰의 키 검증은 예외) |
| backends up (옵션) | 각 백엔드 `GET /v1/models`, `/health` 직접 probe |

> 주의: `/v1/models` 는 OpenAI 호환 스펙이라 `id`(model_name)만 줍니다. **api_base 는 `/model/info` 에서** 나옵니다.

### 대시보드 구조 — `상태` / `부하` 두 탭 (v1.3.0)

화면은 답하는 질문이 다른 두 뷰로 갈라져 있습니다. **데이터가 갈라진 것이 아니라 뷰만
갈라진 것**이라 스냅샷도 폴링도 하나이고, 정지 페이지(`/snapshot.html`)도 그대로 두 탭을
가진 채 저장됩니다.

| 탭 | 답하는 질문 | 담는 것 | 출처 · 주기 |
|---|---|---|---|
| **상태** | *떠 있나* | Model Groups · Registered · Running(healthy) · Paused · Replicas · GPU 카드, Model↔Backend 그래프, Deployments 표, Model Groups | LiteLLM `/model/info`·`/health` + k8s · **5초** |
| **부하** | *지금 바쁜가* | 지금 바쁜 모델 · 처리 중 · 대기 · KV 최대 카드, 부하 표(등급·처리 중·대기·KV·tok/s·표본·근거), `⟳ 부하` | backend Pod `/metrics` · **60초** |

- v1.2.0 까지는 둘이 한 화면에 있어 헤드라인 카드가 **최대 10장**까지 늘었고, 장애(빨강)와
  혼잡(노랑)이 시선에서 구분되지 않았습니다. 지금은 상태 탭 **6장** / 부하 탭 **4장** 입니다.
- 부하 표는 **나쁜 등급 먼저** 정렬합니다(그 다음 대기 많은 순 → 이름순). 상태 표가 이름순
  고정인 것과 의도적으로 다릅니다 — 상태 표는 목록이고, 부하 표는 트리아지입니다.
  `등급` 필터의 **`바쁜 것만`** 은 `BUSY`·`FULL` 만 남깁니다(관측 실패인 `?` 는 섞지 않습니다).
- 탭을 열지 않아도 **부하 탭 이름 옆 점**이 지금 최악 등급을 색으로 알려줍니다(빨강=FULL,
  노랑=BUSY, 초록=ok, 회색=모름). 뷰를 갈랐다고 다른 탭의 이상 신호가 안 보이면 손해입니다.
- 각 탭은 **자기 검색창을 따로** 씁니다(같은 표를 좁히는 게 아니라 다른 질문을 보고 있으므로).
  `/` 는 보고 있는 탭의 검색창을 잡고, `Esc` 는 그 검색어를 지웁니다. 선택한 탭·검색어는
  `sessionStorage` 에 남아 F5 후에도 복원됩니다(탭 닫으면 소멸).
- `MONITOR_LOAD=false` 이거나 per-user 뷰에서 부하가 `off` 면 **부하 탭 자체가 없습니다**
  (빈 탭을 남기지 않습니다).

### 모델 비활성화(일시중지) 판별 — `PAUSED`

LiteLLM v1.90.0 부터 관리자가 모델 deployment 를 **삭제하지 않고 껐다 켤 수** 있습니다
(`POST /model/block` · `POST /model/unblock` · `PATCH /model/{id}/update {"blocked":true}`,
Admin UI 의 토글 스위치). 꺼진 deployment 는 라우팅 풀에서 빠져 **트래픽을 전혀 받지 않습니다**.

모니터는 이 상태를 `/model/info` 의 `model_info.blocked` 로 읽어 `PAUSED` 상태로 표시합니다.

**왜 필요한가** — LiteLLM `/health` 는 `blocked` 를 걸러주지 않습니다(v1.90.0 소스 확인).
일시중지된 백엔드도 계속 ping 되어 **healthy 로 보고**되므로, 이 판별이 없으면
"트래픽을 못 받는데 대시보드에는 UP" 인 거짓 정상이 그대로 남습니다.

- 표에는 `PAUSED(UP)` 처럼 **원래 health 판정을 괄호로** 함께 보여줍니다(`health_status` 필드) —
  다시 켰을 때 실제로 뜰 백엔드인지 판단할 수 있게.
- `PAUSED` 는 healthy 카드에도 unhealthy 카드에도 **들어가지 않고** 별도 `Paused` 카드로 셉니다
  (장애와 의도된 정지를 섞지 않기 위해). status 필터의 "이상만(장애후보)" 에서도 제외됩니다.
- 모든 deployment 가 꺼진 이름은 **능동 health check 대상에서 제외**합니다(꺼둔 백엔드를 두드리지 않음).
  일부만 꺼졌으면 살아있는 sibling 상태를 봐야 하므로 계속 체크합니다.
- **3상태**입니다: `true`(비활성) / `false`(활성) / **키 없음**(구버전 LiteLLM 이거나
  `config.yaml` 전용 모델 — `blocked` 는 DB 등록 모델에만 붙습니다). 키가 없으면 '알 수 없음'
  으로 두고 기존과 완전히 동일하게 동작합니다(`model_monitor_blocked_known` 으로 확인 가능).
- **한계**: 한 이름의 deployment 가 **전부** 꺼지면 LiteLLM 이 그 이름을 `/v1/models` 에서 숨깁니다.
  키별(per-user) 뷰의 접근 목록은 사용자 키의 `/v1/models` 에서 나오므로, 그런 모델은
  사용자 뷰에서 `PAUSED` 로 보이는 게 아니라 **목록에서 사라집니다**(전체/admin 뷰에는 보입니다).

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

### k8s API 호출량과 캐시

한 스냅샷 주기에 **고유 Service 당** 다음 5회를 조회합니다. 같은 Service 를 공유하는 deployment 가
여러 개여도 사이클 내 캐시로 1회로 접힙니다(배포 100개가 Service 12개를 공유하면 60회, 배포당 0.6회).

| 조회 | 성질 | 사이클 간 캐시 |
|------|------|----------------|
| `inferenceservices/<isvc>` | ISVC 존재 여부 + deploymentMode + revision | **부재(404)만** TTL 60초 |
| `services/<svc>` (`spec.selector`) | 라벨 셀렉터 | TTL 60초 + 자기치유 |
| `endpointslices?…` | ready endpoint | 없음 (동적) |
| `deployments/<name>` | readyReplicas / replicas | 없음 (동적) |
| `pods?labelSelector=…` | GPU 수 · 엔진 | 없음 (동적) |
| `nodes/<name>` | GPU 장치명 라벨 | 프로세스 수명 (노드 수명 동안 불변) |

**v1.1.0 에서 위 두 항목을 캐시**해 정상 상태 호출을 Service 당 5회 → 3회로 줄였습니다
(Service 12개·5초 주기 실측: 하루 약 103만 → 66만 회, **38만 회 절감**. TTL 주기마다 콜드 1회는
전체 5회를 조회하므로 평균 3.17회입니다).

캐시하지 **않는** 것이 안전장치입니다:

- **ISVC 조회 성공은 캐시하지 않습니다.** 반환하는 revision 이 `latestReadyRevision` 에서 오는
  동적 값이라, 캐시하면 롤아웃 후에도 옛 revision 이 굳고 그걸 쓰는 Knative PodAutoscaler 조회가
  사라진 revision 을 가리킵니다. 절감은 "ISVC 가 없는 일반 Service" 에서만 나옵니다.
- **404 가 아닌 실패(RBAC/타임아웃/프록시)도 캐시하지 않습니다.** 일시적 실패를 굳히면
  `network_type` 이 TTL 동안 `-`(판정 불가)로 고정됩니다 — 노드 라벨 캐시와 같은 원칙.
- **selector 는 자기치유합니다.** 캐시한 selector 로 Pod 조회가 0건이면 TTL 을 기다리지 않고
  버려 다음 사이클에 다시 읽습니다(라벨을 바꾼 재배포 대응). 실제로 0 replica 인 Service 가
  매 사이클 selector 를 다시 받는 것이 대가인데, 그쪽은 어차피 호출 예산이 남습니다.

TTL 은 [app/services/gpu.py](app/services/gpu.py) 의 `META_TTL`(기본 **60초**).

> ⚠️ **TTL 을 늘리기 전에 읽어주세요 — 호출량이 아니라 각성 안전 문제입니다.** ISVC 부재를
> 캐시하면 그동안 `network_type` 이 `service` 로 남습니다. 그런데 KServe 판별은 "이름 규약
> (`-predictor`) **또는** `network_type==kserve`" 이라, 이름 규약을 따르지 않는 Service 에는
> ISVC 조회가 **유일한 KServe 신호**입니다. 그런 Service 에 Serverless ISVC 가 새로 생기면
> TTL 동안 health check 대상에 들어가 LiteLLM 이 그 백엔드를 ping 해 **깨웁니다**. 이름 규약을
> 지키면 캐시와 무관하게 막히지만, 그 보장 하나에 각성 방지를 걸지 않으려고 60초로 뒀습니다 —
> 위험 창이 캐시 없을 때의 5초에서 12배로만 늘고 절감은 41만 → 38만 회로 8% 만 줍니다.
> 300초로 올리면 창이 60배가 되고 절감은 41만 회가 됩니다(+8%). 이 절충을 보고 정하세요.

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
- **뷰 생성 비용 (v1.1.0)**: 공유 스냅샷을 통째로 복사한 뒤 걸러내던 것을 **필터 먼저, 접근 가능한
  것만 복사**로 바꿨습니다. 예전엔 비용이 사용자 키와 무관하게 *전체* 배포 수에 비례했습니다
  (폴링 주기 × 사용자 수만큼 반복). 실측: 배포 1000개 중 50개 접근 사용자 **13.7ms → 0.24ms**,
  전체 접근이어도 **5.5배**. 남기는 값은 컨테이너면 deepcopy 해 공유 스냅샷과 객체를 공유하지
  않습니다(뷰를 변형해도 다른 사용자 뷰가 깨지지 않음 — 회귀 테스트로 고정).
- **지금 부하 노출 단계 (v1.2.0)**: `MONITOR_USER_VIEW_LOAD`(= config `user_view.load`) 로
  **off / summary / detail** 중 고릅니다(기본 `summary`).

  | 값 | 사용자에게 보이는 것 | 쓰임 |
  | --- | --- | --- |
  | `off` | 없음 — **부하 탭 자체가 사라집니다** | 사용자에게 부하를 아예 알리지 않을 때 |
  | `summary` | 등급만 (`idle`/`ok`/`BUSY`/`FULL`/`?`) + `?` 의 사유 코드 | "지금 쓸 수 있나"에는 답하되 운영 수치는 감출 때 (**기본**) |
  | `detail` | 등급 + 처리 중/대기/KV·표본 수 | 사용자도 수치를 봐야 할 때(사내 팀 등) |

  - 이건 **서버에서 값을 빼는 것**이지 화면에서 가리는 것이 아닙니다 — `POST /api/snapshot/user`
    응답을 직접 열어도 없는 값은 없습니다.
  - `summary` 라도 일부 Pod 을 못 읽고 낸 등급이면 `(일부 Pod)` 로 표시합니다(표본 수는 숨긴 채) —
    불완전한 값을 완전한 척 보여주지 않습니다.
  - 어느 모드에서도 **Pod 주소(`per_pod`)는 나가지 않습니다**. 오타 등 모르는 값은 `detail` 이
    아니라 `summary` 로 떨어집니다(과다 노출 쪽으로 실패하지 않게).
  - `MONITOR_LOAD=false`(수집 자체 off)면 이 값도 자동으로 `off` 입니다.
  - **적용**: env(`MONITOR_USER_VIEW_LOAD`) 또는 설정 파일 `user_view.load`. k8s 는
    ConfigMap 의 `config.json` 을 고친 뒤 `kubectl -n dashboard rollout restart
    deployment/model-monitor` — 예시는 [deploy/k8s.yaml](deploy/k8s.yaml) ConfigMap 위 주석에
    있습니다. 확인은 사용자 키로 직접:
    `curl -s -X POST -H "X-LiteLLM-Key: <사용자 키>" <base>/api/snapshot/user | grep load`
    (`off`=키 없음 · `summary`=`load_state` 만 · `detail`=`load_running`/`load_kv_pct` 까지)
  - `⟳ 부하`(즉시 갱신)는 백엔드 팬아웃을 유발하므로 **admin 키에만** 보입니다.
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

- **요약 게이지**: `model_monitor_deployments_{total,healthy,unhealthy,blocked}`,
  `model_monitor_backend_pods_{ready,desired}_total`(공유 Service 는 1회만 집계),
  `model_monitor_model_groups`, `model_monitor_backend_pods_known`,
  `model_monitor_blocked_known`(0 이면 이 LiteLLM 이 일시중지 상태를 안 알려줌).
- **모델(deployment) 단위**: `model_monitor_model_up`(라벨 `model`/`namespace`/`service`/`status_source`,
  값 **UP=1 · DOWN=0 · 미상/idle/일시중지=-1**), `model_monitor_model_backend_pods_{ready,desired}`,
  `model_monitor_model_scale_to_zero`(0 Pod 가 정상 idle 인지 장애인지 구분),
  `model_monitor_model_blocked`(관리자 일시중지 1/0 — 장애와 구분).
- **GPU**: `model_monitor_backend_gpus_ready_total`(모든 LB 뒤 ready Pod 가 점유한 GPU 합계 — Pod 와
  같은 이유로 공유 Service 는 1회만 집계), `model_monitor_backend_gpus_ready_by_device{device=…}`
  (H100/B200 등 장치 모델별 — 이기종 클러스터에서 어느 장치가 부족한지),
  `model_monitor_model_backend_gpus_ready`(모델 단위, 라벨 `model`/`namespace`/`service`),
  `model_monitor_backend_gpus_known`(0 이면 노드 라벨/RBAC 문제로 GPU 를 하나도 못 알아낸 것 —
  총량 0 이 '장애' 가 아니라 '미상' 임을 구분).
- **스크레이프 신뢰도**: `model_monitor_up`, `model_monitor_build_info{version=…}`,
  `model_monitor_backend_count_enabled`, `model_monitor_collect_errors`(>0 이면 일부 Pod 수 부정확),
  `model_monitor_gpu_collect_errors`(>0 이면 일부 GPU 수 부정확),
  `model_monitor_litellm_reachable`(0=최상류 게이트웨이 미도달), `model_monitor_litellm_errors`(수집 경고 수),
  `model_monitor_collect_failing`(1=마지막 수집 실패, 직전 스냅샷 서빙 중),
  `model_monitor_snapshot_timestamp_seconds`/`model_monitor_snapshot_age_seconds`(스냅샷 나이 — 커지면 수집 멈춤).
- 활용 예: `model_monitor_model_up == 0 and model_monitor_model_scale_to_zero == 0` 으로 **"진짜 죽음"만**
  알림(정상 idle 오탐 제거), `model_up == 1 and model_backend_pods_ready == 0` 으로 **LB 는 200인데 뒤에
  Pod 0** 인 함정 탐지, `avg_over_time(model_monitor_model_up[30d])` 로 모델별 가동률 산출,
  `model_monitor_model_blocked == 1` 을 `for: 24h` 로 걸어 **꺼둔 채 잊힌 모델**(Pod·GPU 는 물고
  트래픽은 0) 탐지. 일시중지는 `model_up` 이 0 이 아니라 -1 이라 기존 DOWN 알림에 **자동으로 안 걸립니다**.
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
| `MONITOR_LOAD` | **지금 부하** 수집 (true; `MONITOR_BACKEND_COUNT` 필요) — 각 backend Pod 의 `/metrics`(vLLM/SGLang 게이지)를 읽어 처리 중/대기 요청·KV 캐시 사용률·tok/s 를 표시. Pod 주소는 GPU 집계가 이미 받아오는 Pod 목록에서 나오므로 **k8s 호출이 늘지 않는다**. Pod 주소를 못 얻는 백엔드(scale-to-zero·external)는 **조회하지 않고** 이유와 함께 `?` — LB 로 찌르면 activator 를 거쳐 모델을 깨우기 때문 |
| `MONITOR_LOAD_INTERVAL` | 부하 조회 주기 초 (**60**) — Pod 마다 `/metrics` 를 읽는 팬아웃이라 스냅샷 갱신(5초)과 분리했다. 화면에는 "N초 전"으로 신선도를 함께 표시하고(부하 탭 탭바 우측), 급하면 같은 자리의 **`⟳ 부하`** 버튼으로 즉시 당겨 읽는다(`POST /api/load/refresh`, 서버가 최소 10초 간격·진행 중 락으로 제한) |
| `MONITOR_LOAD_TIMEOUT` | Pod `/metrics` 조회 타임아웃 초 (3) — 죽은 Pod 가 사이클을 잡아먹지 않게 짧게 |
| `MONITOR_LOAD_ROUTING` | 한 `model_name` 에 backend 가 여러 개일 때 모델 등급 기준 (`least-busy` \| `shuffle`). **LiteLLM 의 `routing_strategy` 에 맞춘다** — least-busy 면 다음 요청이 갈 가장 한가한 backend 가 답이고, simple-shuffle 이면 포화된 backend 도 트래픽을 받으므로 가장 나쁜 쪽이 정직하다. 어느 쪽이든 화면에는 등급 분포(`FULL 1 · ok 1`)를 함께 표시 |
| `MONITOR_PROMETHEUS_URL` | Pod 직접 조회가 막혔을 때(NetworkPolicy·mTLS) 같은 게이지를 대신 읽을 외부 Prometheus URL. 출처가 같아 정확도는 동일하고 스크레이프 주기만큼 늦다 |
| `MONITOR_PROMETHEUS_FIRST` / `MONITOR_PROMETHEUS_LOOKBACK` | Pod 조회를 건너뛰고 Prometheus 만 사용 (false) / 조회 구간 (`2m` — 이보다 오래된 샘플은 '모름'으로 둔다) |
| `MONITOR_METRICS` | Prometheus `/metrics` (true) |
| `MONITOR_METRICS_TOKEN` | 키 필수 모드에서 `/metrics` 스크레이프용 Bearer 토큰 (미설정=admin 키만) |
| `MONITOR_USER_VIEW` | 키 필수(per-user) 모드 — 키 입력해야 조회, admin 키는 전체 뷰 (false) |
| `MONITOR_USER_VIEW_SHOW_INTERNAL` | per-user 뷰에서 내부 api_base/namespace 도 표시 (false=숨김) |
| `MONITOR_USER_VIEW_LOAD` | per-user 뷰에 **지금 부하**를 어디까지 보여줄지 — `off` \| `summary` \| `detail` (**기본 `summary`**). `off`=아예 안 보냄(부하 탭도 사라짐), `summary`=등급(idle/ok/BUSY/FULL/?)만, `detail`=처리중/대기/KV 수치까지. 어느 모드든 **Pod 주소는 나가지 않는다**. `MONITOR_LOAD=false` 면 자동으로 `off` |
| `MONITOR_USER_VIEW_CACHE_TTL` | 키별 접근(/v1/models) 캐시 TTL 초 (30) |
| `MONITOR_K8S_API_SERVER` / `MONITOR_K8S_TOKEN_FILE` / `MONITOR_K8S_CA_FILE` | k8s 접근 오버라이드 |
| `MONITOR_K8S_INSECURE` / `MONITOR_K8S_TIMEOUT` | k8s API TLS 검증 비활성 / 타임아웃 초 (false / 5) |

> `load.thresholds`(등급 판정 기준: `queue_busy`/`queue_saturated`/`kv_busy`/`kv_saturated`)와
> `prometheus.labels`(스크레이프 라벨 이름 교정)는 설정 파일에서 받습니다.

> 중첩 설정(`backends`, `namespace_overrides`, `user_view.*`, `metrics.*` 등)은 `MONITOR_CONFIG_FILE` 가 가리키는 설정 파일에서 받습니다.

### 부하 조회 주기와 수동 새로고침

- **백그라운드 루프 하나**가 60초마다 조회한다. **화면을 보는 세션 수와 무관**하다 — 대시보드
  폴링(`GET /api/snapshot`)은 캐시된 스냅샷을 그대로 돌려줄 뿐 수집하지 않는다(이 프로젝트의
  기본 규칙: 요청 경로에서 수집하지 않는다). 10명이 보고 있어도 백엔드로 나가는 조회는 그대로다.
- 값의 나이는 화면에 `N초 전` 으로 표시하고, 주기의 2배를 넘으면 노란색으로 바뀐다.
- 즉시 보고 싶으면 **부하 탭**의 `⟳ 부하` 버튼 → `POST /api/load/refresh`. **이것만이 요청 경로에서 실제로
  수집하는 예외**라, 서버가 최소 간격 10초 + 진행 중 락으로 직렬화한다(여러 명이 눌러도 팬아웃이
  겹치지 않는다). 키 필수 모드에서는 admin 키가 있어야 한다.

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

# model-monitor

**버전: v0.3.0**

LiteLLM → KServe → vLLM/SGLang 백엔드에서 **실제로 떠 있는 모델 현황**과 **각 api_base(LB) 뒤에 떠 있는 backend Pod 개수**를 보여주는 모니터. 터미널(TUI)과 웹 대시보드(`--serve`)를 모두 제공합니다.

외부 패키지 없이 **Python 3.6+ 표준 라이브러리만** 사용합니다. air-gapped 노드에서 `pip install` 없이 `model_monitor.py` 한 파일만 있으면 실행됩니다.

> 버전 확인: `python3 model_monitor.py --version` · TUI/웹 헤더와 `/api/snapshot` 의 `version` 필드에도 표시됩니다.

## 무엇을 보여주나

| 지표 | 출처 |
|------|------|
| model groups | LiteLLM `GET /model_group/info` |
| **model_name → api_base 매핑** | LiteLLM `GET /model/info` (api_base 평문, admin 키 권장) |
| running(healthy) / unhealthy | LiteLLM `GET /health` (api_base 기준으로 위 매핑과 join) |
| **LB 뒤 backend Pod 개수 (ready/desired)** | Kubernetes API (EndpointSlice / Knative PodAutoscaler / Deployment) |
| OpenAI 호환 모델 이름 목록 | LiteLLM `GET /v1/models` (이름만, **api_base 없음**) |
| **지금 바쁜가 (부하 등급 · 실행/대기 요청 · KV 캐시 · tok/s)** | 백엔드 `GET /metrics` (vLLM/SGLang 게이지) — **Pod 마다 직접** 조회 |
| 누적 요청 수 / 토큰 (`--usage`) | LiteLLM `GET /global/activity/model` (→ `/gateway/daily/activity` → `/model/metrics` 폴백) |
| 분당 요청(rpm) · rpm/tpm 한도 대비 사용률 (`--usage`) | 위 누적값 ÷ 구간, 한도는 `GET /model_group/info` |
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

## 지금 바쁜가 (현재 부하) — 기본 켜짐

이 도구가 답해야 하는 질문은 **"지금 이 모델에 요청을 보내면 바로 처리되나, 기다리나"** 입니다.
그 답은 LiteLLM 누적 집계가 아니라 **vLLM/SGLang 엔진이 지금 이 순간 노출하는 게이지**에 있습니다.

| 열 | 뜻 | 왜 중요한가 |
|----|----|------------|
| `LOAD` | `idle` / `ok` / `BUSY` / `FULL` + 근거 | 한 단어로 보는 답 |
| `RUN` | 지금 처리 중인 요청 수 | 실제 동시 처리량 |
| `QUEUE` | 지금 **대기 중**인 요청 수 | **0보다 크면 이미 사용자가 기다리는 중** |
| `KV CACHE` | KV 캐시 사용률(%) — Pod 중 **최댓값** | GPU 포화도. 90%대면 곧 큐가 생긴다 |
| `TOK/S` | 지금 생성 속도(토큰/초) | 카운터 차분 — **두 번째 갱신부터** 표시 |
| `PODS` | 부하를 실제로 읽은 Pod 수 (`LB` = 단일 샘플) | 표본을 숨기지 않는다 |

**판정 기준** (`config` 의 `load.thresholds` 로 조정):

| 등급 | 조건 | 뜻 |
|------|------|-----|
| `FULL` | 대기 ≥ 5 또는 KV ≥ 95% | 지금 보내면 기다린다 |
| `BUSY` | 대기 ≥ 1 또는 KV ≥ 80% | 밀리기 시작함 |
| `ok` | 처리 중이지만 큐 없음 | 여유 |
| `idle` | 처리 중 0 | 놀고 있음 |
| `?` | 게이지 조회 실패 | **0 이 아니라 "모름"** — 추측하지 않는다 |

### Pod 마다 직접 읽습니다 (핵심)

`api_base` 로 `/metrics` 를 찌르면 LB 가 **뒤에 있는 Pod 중 하나**로만 보냅니다. Pod 3개 중 1개만
큐가 쌓여 있으면 2/3 확률로 못 봅니다. 그래서 모니터는 backend 개수를 셀 때 이미 조회하는
**EndpointSlice 에서 Pod 주소를 함께 얻어, Pod 마다 `/metrics` 를 직접** 읽습니다(스레드 병렬).

- 집계: `RUN`/`QUEUE` 는 **합**(그 모델이 지금 물고 있는 전체), `KV` 는 **최댓값**(한 Pod 만
  포화돼도 그 모델은 이미 아픔) — 평균은 툴팁에 함께 표시.
- Pod 주소를 모르면(외부 IP 백엔드·k8s 미사용·scale-to-zero) LB 로 1회만 샘플링하고 `PODS` 열에
  `LB` 로 **명시**합니다. 숨기지 않습니다.
- 일부 Pod 조회가 실패하면 `2/3` 처럼 표본 수를 드러내고 배너로 경고합니다(과소 집계 가능).
- Pod IP 로 직접 접속하므로 **모니터 Pod → 백엔드 Pod 네트워크가 열려 있어야** 합니다.
  (RBAC 은 기존 `endpointslices` 읽기 권한 그대로면 됩니다.)

### 이 조회가 백엔드에 주는 부하

측정값 (모델 10개 · Service 10개 · Pod 30개 · loopback 기준):

| 항목 | 갱신 1회 | 5초 주기 환산 |
|------|---------|--------------|
| Pod `/metrics` | Pod 당 1회 (≈15 KB) | **Pod 당 0.2 req/s**, 전체 ≈87 KB/s |
| k8s API | 30회 (Service 당 3: ISVC·EndpointSlice·Deployment) | 6 req/s |
| LiteLLM | 3회 (`/model_group/info`·`/model/info`·`/v1/models`) | 0.6 req/s |
| 모니터 CPU | 파싱 0.26 ms/scrape, fan-out 45 ms/라운드 | 1코어의 ≈0.2% |

`/metrics` 는 엔진이 메모리에 들고 있는 카운터를 텍스트로 뽑는 것이라 **GPU·추론 배치와는
무관**합니다. 다만 vLLM 의 API 서버와 같은 이벤트 루프에서 처리되므로, **모델이 바쁠 때는
scrape 응답도 같이 느려집니다**(그래서 타임아웃이 짧고, 실패는 0 이 아니라 `?`로 표시).

- Pod 가 많으면 `--interval` 을 10~15초로 올리는 게 가장 효과적입니다(모든 비용이 주기에 반비례).
- 응답 없는 Pod 가 섞이면 한 라운드 최악 시간 = `ceil(Pod수 / --load-threads) × --load-timeout`
  입니다. Pod 100개 규모면 `--load-threads 32` 정도로 올리세요.
- 이미 Prometheus 가 vLLM 을 스크레이프 중이라면 **같은 출처**입니다. 이 도구는 스크레이퍼가
  하나 더 붙는 셈이고, 대신 Prometheus 없이도 즉시 동작합니다.

터미널 표는 **바쁜 순으로 정렬**됩니다(`--sort name` 으로 이름순 고정). 웹은 헤더의 `load` 필터로
바쁜 모델만 골라볼 수 있고, `sort` 를 이름순으로 바꿀 수 있습니다.

```bash
# 지금 부하만 실시간으로 (기본값 — 별도 플래그 필요 없음)
python3 model_monitor.py --config config.yaml --watch
python3 model_monitor.py --config config.yaml --serve --port 8088

# 부하 수집 끄기 / Pod 조회 타임아웃 늘리기
python3 model_monitor.py --config config.yaml --no-load
python3 model_monitor.py --config config.yaml --load-timeout 5
```

## 누적 사용량 (요청 수 · rpm) — `--usage` 로 켜기

"지금 바쁜가"와는 **다른 축**입니다. 지난 N시간 동안 몇 번 불렸는지를 봅니다. 기본은 꺼져 있고
`--usage` 로 켜면 `REQ (24h)` / `RPM` 열이 붙습니다.

- **LiteLLM 에 DB 가 붙어 있어야 합니다.** 요청 수는 LiteLLM 이 모든 요청을 `LiteLLM_SpendLogs` 에
  적어둔 걸 `/global/activity/model` 이 `model_group`·일자로 GROUP BY 해서 돌려주는 값입니다.
  DB 가 없으면 `Database not connected` 를 그대로 사유로 표시하고 열을 생략합니다.
- **RPM 은 LiteLLM 이 주는 값이 아닙니다** — `요청 수 ÷ 구간` 으로 계산한 **구간 평균**입니다.
  순간 속도가 아니므로 "지금 바쁜가"에는 쓸 수 없습니다(그건 `QUEUE`/`KV` 를 보세요).
- **키 권한에 따라 범위가 달라집니다.** admin 키는 전체, internal user 키는 그 사용자 몫만.
  같은 이유로 `/user/daily/activity` 는 폴백 후보에서 제외했습니다(조용한 과소 집계 방지).
- 사용량은 `model_name`(그룹) 단위라 같은 이름의 deployment 가 여러 개면 각 행에 같은 값이 붙습니다.
  카드/합계는 행을 더하지 않고 집계 원본(`usage.totals`)을 씁니다.

```bash
python3 model_monitor.py --config config.yaml --usage --usage-window 6 --watch
```

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
#   현재 상태 내보내기(공유/디버깅용 — 헤더의 [💾 JSON]/[정지 페이지] 버튼과 동일):
#     http://localhost:8088/snapshot.json   # raw JSON 파일 다운로드(클릭 한 번)
#     http://localhost:8088/snapshot.html   # 데이터가 박제된 self-contained 정지 페이지(저장해 공유)

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
| `--no-load` | 현재 부하(엔진 게이지) 수집 끄기 |
| `--load-timeout N` | Pod `/metrics` 조회 타임아웃(초, 기본 3) |
| `--load-threads N` | Pod `/metrics` 동시 조회 수(기본 12) |
| `--sort load\|name` | 터미널 표 정렬 (기본 `load` = 바쁜 순) |
| `--usage` | 누적 요청 수/토큰 열 추가 (LiteLLM DB 필요) |
| `--usage-window N` | 누적 집계 구간(시간, 기본 24) |
| `--health-timeout N` | LiteLLM `/health` 타임아웃(초, 기본 90 — 모델 많으면 늘리기) |
| `--no-health` | `/health` 호출 안 함 (status 는 k8s backend readiness 로만 판정) |
| `--k8s-api-server` / `--k8s-token-file` / `--k8s-ca-file` | k8s 접근 오버라이드 |
| `--k8s-insecure` | k8s API TLS 검증 비활성 |

## 운영 배포

빌드한 컨테이너 이미지로 배포합니다. in-cluster 로 뜨면 backend Pod 개수 수집이 자동으로 켜집니다(ServiceAccount 토큰 사용).

1. **이미지 빌드** ([ci.sh](ci.sh) → [Dockerfile](Dockerfile)) — 로컬 `ai-tool/llm-monitor:<버전>` + `:latest`:
   ```bash
   ./ci.sh
   ```
2. **레지스트리에 push** ([push.sh](push.sh)) — `10.92.20.77:5002` 로 retag 후 push:
   ```bash
   ./push.sh
   #  -> 10.92.20.77:5002/ai-tool/llm-monitor:<버전> + :latest
   ```
3. **배포** ([deploy/k8s.yaml](deploy/k8s.yaml) — Namespace / ServiceAccount / **ClusterRole(RBAC)** / ConfigMap / Deployment / Service):
   ```bash
   # 먼저 deploy/k8s.yaml 의 ConfigMap 에서 LiteLLM url·api_key·namespace 를 실제 값으로 교체
   kubectl apply -f deploy/k8s.yaml
   kubectl -n model-monitor port-forward svc/model-monitor 8088:80   # 브라우저로 확인
   ```

> Deployment 는 `image: 10.92.20.77:5002/ai-tool/llm-monitor:latest` + `imagePullPolicy: Always` 라 latest 최신본을 매번 레지스트리에서 받습니다.
> HTTP(비TLS) 레지스트리면 빌드 노드의 docker(`/etc/docker/daemon.json` 의 `insecure-registries`)와 클러스터 노드의 containerd 에 `10.92.20.77:5002` 를 insecure 레지스트리로 등록해야 push/pull 이 됩니다.

### backend 개수 산출 방식 (KServe)
- **KServe ISVC**(api_base 가 `<isvc>-predictor...`): Deployment 를 `serving.kserve.io/inferenceservice=<isvc>`
  라벨로 찾아 `readyReplicas`/`replicas` 를 합산합니다. RawDeployment·Serverless 공통으로 동작하고
  Knative 네이밍/activator 를 몰라도 됩니다. 라벨 매칭이 안 되면 Knative PodAutoscaler `actualScale` 로 보강.
- **일반 Service**(비 KServe): EndpointSlice 의 ready 주소 수.
- scale-to-zero 면 `0 (scaled-to-zero)` 로 표기(장애 아님). 산출 실패 시 `?` 에 마우스를 올리면 원인을 보여줍니다.

### Troubleshooting
- **STATUS 가 전부 `?` + `health: timed out`**: LiteLLM `/health` 는 모든 백엔드를 실제 ping 해서
  모델이 많으면 수십 초 걸립니다(실측 60s 사례 있음). 웹(`--serve`)은 `/health` 를 **별도 스레드로
  비동기 수집**하므로 화면이 멈추지 않고, status 는 우선 **k8s backend readiness 로 즉시 판정**한 뒤
  `/health` 가 도착하면 보강합니다. 그래도 부족하면 `--health-timeout`(기본 90s)을 늘리거나,
  k8s readiness 만으로 충분하면 `--no-health` 로 끄세요. (KServe 모델은 `/health` 없이도 backend
  Pod 가 ready 면 UP 으로 표시됩니다. 단 외부 IP 백엔드는 `/health` 가 있어야 status 가 나옵니다.)
- **REQ/RPM 열이 안 보임**: LiteLLM 분석 엔드포인트가 전부 실패한 경우입니다. 가장 흔한 원인은
  **LiteLLM 에 DB 가 안 붙어 있는 것**(`Database not connected`)이고, 그 다음이 버전 차이로 경로가
  없는 경우입니다. 표 아래에 시도한 엔드포인트와 사유가 그대로 나옵니다.
  `--json` 의 `usage.errors` 에서도 확인할 수 있습니다.
- **LOAD 가 `?`**: 백엔드 `/metrics` 를 못 읽은 경우입니다 — 엔진이 메트릭을 끄고 떴거나, Pod IP 로
  가는 네트워크가 막혔거나, 포트가 다릅니다. vLLM 은 `vllm:num_requests_running` /
  `vllm:gpu_cache_usage_perc`, SGLang 은 `sglang:num_running_reqs` / `sglang:token_usage` 를 읽습니다.
  `PODS` 열(웹은 마우스 오버)에 Pod 별 실패 사유가 그대로 나옵니다. **`?` 는 0 이 아닙니다.**
- **`PODS` 에 `LB` 라고 나옴**: Pod 주소를 못 얻어 LB 로 1개만 샘플링했다는 뜻입니다(외부 IP 백엔드,
  k8s 조회 꺼짐, scale-to-zero). 이 경우 부하는 Pod 하나의 값이라 과소 집계일 수 있습니다.
- **`TOK/S` 가 `-`**: 카운터 차분이라 **두 번째 갱신부터** 나옵니다(1회 실행은 비교 대상이 없음).
  `--watch` / `--serve` 에서 보세요.
- **backend 가 `?`(원인 보기)**: 셀에 마우스를 올리면 `k8s_error`(예: `deployments(label): no match`,
  `knative: HTTP 403`)가 뜹니다. RBAC([deploy/k8s.yaml](deploy/k8s.yaml) ClusterRole)나 라벨/네임스페이스를 점검하세요.
- 웹 수집은 백그라운드 스레드에서 주기적으로 돌고 HTTP 는 마지막 스냅샷을 즉시 반환합니다
  (요청 블로킹/BrokenPipe 없음).

## 로드맵 / TODO

- **사용자(키)별 대시보드** — 사용자가 본인 키를 입력하면 그 키로 접근 가능한 모델만 필터링해
  보여주는 per-user 뷰. 계획은 [TODO.md](TODO.md) 참고.

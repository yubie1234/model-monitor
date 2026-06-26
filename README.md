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
#   현재 상태 내보내기(공유/디버깅용 — 헤더의 [💾 JSON]/[정지 페이지] 버튼과 동일):
#     http://localhost:8088/snapshot.json   # raw JSON 파일 다운로드(클릭 한 번)
#     http://localhost:8088/snapshot.html   # 데이터가 박제된 self-contained 정지 페이지(저장해 공유)

# 키별(per-user) 뷰 — 켜면 "키 필수 모드": 키를 입력해야 목록이 보인다
#   기본 OFF. 켜기 전에 ① /v1/models 가 키별로 필터되는지 ② TLS 종단 을 확인할 것.
python3 model_monitor.py --config config.yaml --serve --enable-user-view
#   -> 대시보드 상단 "🔑 키로 조회" 바에 키 입력 후 Enter/조회
#   -> 일반 키: 그 키로 볼 수 있는 모델만 (내부 api_base 숨김)
#   -> admin 키(= 구동 시 쓴 키): 전체 뷰 + 내부 주소 + export 버튼 노출
#   -> 키는 브라우저(sessionStorage)에만, 매 요청 X-LiteLLM-Key 헤더로만 전송(서버 저장·로그 없음)
#   -> 키 무효/만료면 fail-closed (전체 뷰로 폴백하지 않고 에러만 표시)

# 라이브 엔드포인트 없이 출력 미리보기 (TUI / 웹 둘 다 가능)
python3 model_monitor.py --demo
python3 model_monitor.py --demo --watch
python3 model_monitor.py --demo --serve --port 8088
```

### 키별(per-user) 뷰 — `--enable-user-view` ("키 필수 모드")

LiteLLM 가상 키마다 접근 가능한 모델이 다릅니다. 이 모드를 켜면 **키를 입력해야만 목록이 보이며**,
입력한 키 기준으로 필터된 뷰를 보여줍니다.

- **데이터 흐름**: 무거운 데이터(상태·Pod 수·`model/info`·`/health`)는 **admin 키로 백그라운드에서
  한 번만 수집해 공유 캐시**에 둡니다(키와 무관). 요청이 오면 이 캐시를 키별로 **필터**만 하므로,
  서로 다른 키로 조회해도 무거운 호출이 중복되지 않습니다.
- **입력 키에 따라**:
  - 일반 키 → 그 키의 `GET /v1/models`(접근 모델 집합)로 필터한 뷰 + "내 키" 카드(spend/budget/limit).
    내부 `api_base`/namespace 는 **숨김**.
  - admin 키(= 모니터 구동 시 쓴 키) → 내부 주소 포함 **전체 뷰** + export(JSON/정지 페이지) 버튼.
- **접근 캐시**: 같은 키의 `/v1/models` 결과를 **짧은 TTL(기본 30s)** 캐시해 폴링 중복 호출을 제거
  (해시만 보관, 원문 키 비저장). 성공만 캐시 → 무효 키는 매번 재검증. 취소/만료 키는 최대 TTL 동안 stale.
  config `user_view.cache_ttl` 로 조절.
- **보안**: 키는 헤더(`X-LiteLLM-Key`) 전용(쿼리 금지), 서버 비저장·비로그, 브라우저 sessionStorage
  에만 보관(탭 닫으면 소멸). 무인증 `GET /api/snapshot`·export 는 **잠김**(export 는 admin 키 헤더로만).
  키 검증 실패 시 전체 뷰로 폴백하지 않습니다(fail-closed).
- **⚠️ 전제**: ① LiteLLM 버전/설정에 따라 `/v1/models` 가 키별로 필터되지 않을 수 있으니 **먼저
  확인**(안 되면 켜지 말 것 — 전체 모델 유출) ② 키를 평문 반복 전송하므로 **TLS 뒤에서만** 노출.
  이 때문에 기능은 **기본 OFF** 입니다. (`--enable-user-view` OFF면 기존 열린 global 대시보드 그대로)

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
| `--enable-user-view` | 키 필수 모드 활성 — 키 입력해야 조회, admin 키는 전체 뷰 (기본 OFF) |
| `--user-view-show-internal` | per-user(일반 키) 뷰에서 내부 `api_base`/namespace 도 표시 (기본 숨김) |
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
- **backend 가 `?`(원인 보기)**: 셀에 마우스를 올리면 `k8s_error`(예: `deployments(label): no match`,
  `knative: HTTP 403`)가 뜹니다. RBAC([deploy/k8s.yaml](deploy/k8s.yaml) ClusterRole)나 라벨/네임스페이스를 점검하세요.
- 웹 수집은 백그라운드 스레드에서 주기적으로 돌고 HTTP 는 마지막 스냅샷을 즉시 반환합니다
  (요청 블로킹/BrokenPipe 없음).

## 로드맵 / TODO

- ✅ **사용자(키)별 대시보드** — 1차 구현 완료(`--enable-user-view`, 위 "키별 뷰" 절 참고).
  남은 운영 과제(TLS 전제 확인, 라이브 Go/No-Go, per-IP throttle)는 [TODO.md](TODO.md) 참고.
- **(나중) admin 총괄 뷰(B안)** — admin 키로 키별/팀별 접근 모델을 한눈에. [TODO.md](TODO.md) 8.

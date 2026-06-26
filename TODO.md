# TODO — 사용자(키)별 대시보드

> 현재: **전체(global) 모니터링** 동작. (LiteLLM 전체 모델 + 상태 + LB 뒤 backend Pod 수)
> 다음: **키마다 접근 가능한 모델이 다른** 점을 반영한 **per-user 뷰** 추가.
> 전제: 사용자가 **자기 키를 직접 들고 있고, 그 키를 입력으로 사용**한다.

---

## 0. 구현 전 필수 4원칙 (검수 반영)

1. **필터는 사본 위에서** — 공유 `state["snap"]` 을 deepcopy 후 필터(in-place 금지). 안 그러면 global 뷰 오염. → 5.백엔드
2. **키는 헤더 전용** — 쿼리스트링 금지(프록시 로그 유출). → 5.웹
3. **`/v1/models` 필터링은 Go/No-Go 게이트** — 키별 필터 안 되면 A안 보류. → 4
4. **fail-closed** — 키 무효 시 절대 unfiltered global 로 폴백 금지. → 5.웹/보안

---

## 1. 목표

LiteLLM은 가상 키(virtual key)마다 접근 가능한 모델이 다르다. 사용자가 **본인 키를 입력**하면,
그 키로 **볼 수 있는 모델만** 골라 상태(UP/DOWN)와 백엔드 정보를 보여주는 개인 대시보드를 제공한다.

## 2. 확정된 접근 방식 — A (셀프서비스, 키 입력)

- 사용자가 키를 직접 입력 → 서버가 **그 키로 LiteLLM을 조회** → LiteLLM이 알아서 필터해 준다.
- 따라서 LiteLLM의 접근 규칙(키 `models` / 팀 상속 / `openai/*` 와일드카드 / model access group /
  default team models)을 **우리가 재현할 필요가 없다.** LiteLLM이 판정한 결과를 그대로 쓴다.
- admin 키로 키 목록을 들추거나 사용자를 impersonate 할 필요도 없다.

## 3. 왜 구조 변경이 작은가 (핵심)

비싼 데이터(상태, LB 뒤 Pod 수)는 **deployment 단위라 키와 무관**하다. 그러니:

- admin 백그라운드 스냅샷(전체 + 상태 + Pod 수)은 **지금 그대로 재사용**한다.
- per-user 뷰 = 그 스냅샷을 **"이 키가 접근 가능한 model_name 집합"으로 필터**하는 얇은 레이어.
- 사용자 요청 시 추가 호출은 가볍다: 그 키로 `/v1/models`(+`/key/info`)만. (느린 `/health`·k8s 조회 없음)

```
[admin 백그라운드 스냅샷: 전체 모델 + 상태 + Pod 수]   ← 기존 (키 무관)
                    │  filter(model_name ∈ 키 접근목록)
[사용자 키] → /v1/models(+/key/info) → 접근목록 + 키메타 ┘
                    ↓
[per-user 뷰: 내 모델만 + 상태/Pod + 내 키 카드(spend/budget/limit)]
```

## 4. 사전 검증 (라이브 붙으면 먼저 — 버전마다 동작 차이 가능)

- [ ] 🚦 **Go/No-Go 게이트** — admin 키와 일반 키로 각각 `GET /v1/models` → **일반 키 결과가 실제로 필터**되는지 확인.
      일부 LiteLLM 설정/버전은 키와 무관하게 전체를 돌려준다 → 그러면 A안은 **전체 모델을 조용히 유출**한다.
      여기서 실패하면 A안 자체를 보류한다(단순 체크 항목 아님).
- [ ] `GET /key/info`(키 자신 조회) 응답 형태 확인: `models`, `spend`, `max_budget`,
      `tpm_limit`, `rpm_limit`, `expires`, `team_id` — **비-admin 키가 자기 키 정보를 읽을 수 있는지**도 함께 확인
- [ ] `/v1/models`의 `id` 가 `/model/info`의 `model_name`(public name)과 **동일 값으로 조인 가능**한지
- [ ] "제한 없음" 표현 확인: `models` 가 빈 배열 / `all-proxy-models` / `*` / `openai/*` 와일드카드
      → **전체 접근으로 처리**해야 함

## 5. 구현 계획

### 백엔드 ([model_monitor.py](model_monitor.py))
- [ ] `collect_user_access(url, user_key, timeout)` — 그 키로 `/v1/models`(접근 model_name 집합) +
      `/key/info`(키 메타) 수집
- [ ] **권한 판정의 단일 출처는 `/v1/models`(이미 해석된 결과).** `/key/info.models` 로 접근권을
      재유도하지 말 것 — 그러면 우리가 피하려던 와일드카드/팀상속 해석이 되살아난다.
      `/key/info` 는 메타(spend/budget/limit) 표시용으로만 쓴다.
      → 따라서 와일드카드/무제한(`*`,`openai/*`,빈 목록)은 `/v1/models` 가 알아서 풀어주므로 별도 처리 거의 불필요.
- [ ] `filter_snapshot_for_user(global_snap, accessible_set, key_info)` —
      deployments/groups 를 `accessible_set` 으로 필터, `summarize()` 재사용해 summary 재계산,
      `key_info` 첨부 (상태·Pod 수는 global 값 그대로 join)
- [ ] ⚠️ **공유 캐시 오염 주의** — 서버는 단일 `state["snap"]` 을 락으로 공유한다(`_snapshot()` 은 얕은 복사).
      필터는 반드시 **deepcopy 한 사본 위에서** 수행할 것. global 스냅샷의 `litellm.deployments` 를
      제자리(in-place) 로 필터하면 **모든 사용자의 global 뷰가 깨진다.**

### 웹 ([serve_dashboard](model_monitor.py))
- [ ] 라우트 `POST /api/snapshot/user` — 키를 **헤더(`X-LiteLLM-Key`) 전용**으로 받는다.
      ❗ **쿼리스트링 금지** — 쿼리는 ingress/LB/프록시 액세스 로그·브라우저 히스토리·Referer 에 남아
      "키 미노출" 원칙과 정면 모순된다(앱의 `log_message` 억제로는 앞단 프록시를 못 막음).
      캐시된 global 스냅샷 + `collect_user_access` → `filter_*` → 필터된 JSON 반환. **키는 저장/로그 없이 pass-through.**
- [ ] **fail-closed** — 키 검증 실패(401/만료)면 빈 결과/명확한 에러만. **절대 unfiltered global 로 폴백 금지.**
- [ ] 박제 export(`/snapshot.html`, `/snapshot.json`)에 **per-user 데이터·키가 절대 굽히지 않도록** 확인(global 전용 유지).
- [ ] UI: 헤더에 키 입력(`type=password`) + "내 모델만 보기" 토글.
      키는 **브라우저만 보관**(sessionStorage, 탭 닫으면 소멸), 매 요청에 실어 보냄.
- [ ] "내 키" 카드: 접근 가능 모델 수 / spend / 예산 잔액 / tpm·rpm / 만료.

### 보안
- [ ] 키는 비밀 — 서버 영속 저장 금지, **액세스 로그·에러 메시지에 키 노출 금지**(헤더 전용·쿼리 금지로 1차 차단).
- [ ] per-user 뷰에서 내부 `api_base`(클러스터 토폴로지) **숨김** 고려 → 상태/Pod 수만, 내부 URL은 admin 전용.
- [ ] 잘못된/만료 키 → global 뷰로 새지 않게 **명확한 에러** 표시(위 fail-closed 와 동일 원칙).
- [ ] 키가 DOM/`title` 속성에 안 들어가게 렌더 점검(`esc()` 사용처 확인).
- [ ] ⚠️ **TLS 전제** — 현재 배포는 HTTP(비TLS)다. 키를 폴링마다 평문으로 반복 전송하면 노출된다.
      per-user 기능은 **TLS 뒤에서만** 노출하거나(인그레스 TLS 종단), 폴링 주기를 늘릴 것.
- [ ] **남용 방지(선택)** — 인증 없는 엔드포인트가 임의 키를 LiteLLM 에 중계 = 키 검증/브루트포스 통로.
      내부 도구라 치명적이진 않으나, 가벼운 per-IP throttle 고려.

## 6. 열린 질문 (결정 필요)

- [ ] per-user 뷰에 **backend Pod 수까지** 보여줄지, **상태(UP/DOWN)만** 보여줄지 (내부정보 노출 범위)
      → *의견: 비-admin 뷰는 `api_base`·namespace 숨기고 status 위주. Pod 수 노출은 선택, 내부 URL은 admin 전용.*
- [ ] 한 화면에서 **여러 키 비교**가 필요한지 (필요하면 다중 키 입력 지원)
- [ ] spend/budget을 어디까지 노출할지 (`/key/info` 만 vs `/global/spend/...` 분석까지)

## 7. (선택) 나중에 — B: admin 총괄 뷰

admin 키 하나로 `/key/list`·`/team/list`·`/user/info` 를 받아 **키별/팀별 접근 모델을 한눈에**.
단, "설정값"이라 와일드카드/팀 상속/access group 을 **우리가 해석**해야 해 깨지기 쉽고,
모든 키 설정을 노출하므로 **admin 전용**으로 잠가야 함. A가 자리잡은 뒤 별도 탭으로 검토.

## 8. 참고 — LiteLLM 엔드포인트

| 용도 | 엔드포인트 | 비고 |
|------|-----------|------|
| 접근 가능한 모델(키 스코프) | `GET /v1/models` | 호출한 키 기준 필터 |
| deployment 정보(키 스코프) | `GET /model/info` | 비-admin 키는 `api_base` 마스킹 |
| 모델 그룹(키 스코프) | `GET /model_group/info` | 〃 |
| 키 메타(spend/budget/limit) | `GET /key/info` | 키 자신 또는 admin |
| (B용) 전체 키/팀/유저 | `GET /key/list`, `/team/list`, `/user/info` | admin 전용 |

> 위 "키 스코프 필터링" 동작은 LiteLLM 버전에 따라 다를 수 있으니 **4. 사전 검증**을 먼저 수행한다.

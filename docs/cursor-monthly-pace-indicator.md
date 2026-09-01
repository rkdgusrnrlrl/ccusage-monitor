# Cursor 월간 사용 페이스 표시 기획

## 조사 결과

현재 `cursor_usage.py`는 로컬 Cursor IDE의 `cursorAuth/accessToken`을 읽고 아래 비공개 Connect RPC를 30초 간격으로 호출한다.

```text
POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage
Authorization: Bearer <local Cursor token>
Connect-Protocol-Version: 1
Body: {}
```

확인된 응답 구조는 다음과 같다.

```json
{
  "billingCycleStart": "1768399334000",
  "billingCycleEnd": "1771077734000",
  "planUsage": {
    "totalSpend": 23222,
    "includedSpend": 23222,
    "bonusSpend": 0,
    "remaining": 16778,
    "limit": 40000,
    "remainingBonus": false,
    "autoPercentUsed": 31.2,
    "apiPercentUsed": 46.4,
    "totalPercentUsed": 38.7
  },
  "spendLimitUsage": {
    "totalSpend": 0,
    "individualLimit": 10000,
    "individualUsed": 0,
    "individualRemaining": 10000,
    "limitType": "user"
  },
  "autoModelSelectedDisplayMessage": "...",
  "namedModelSelectedDisplayMessage": "..."
}
```

- `billingCycleStart` / `billingCycleEnd`: 정확한 현재 결제 주기. 문자열 형태의 Unix 밀리초가 일반적이다.
- `autoPercentUsed`: 현재 UI의 `cur` 월간 사용률.
- `apiPercentUsed`: 현재 UI의 `api` 월간 사용률.
- `totalSpend`, `includedSpend`, `remaining`, `limit`: 센트 단위의 전체 포함 사용량/한도. `cur`와 `api` 각각의 금액으로 나눌 수 있는 값은 아니다.
- `spendLimitUsage`: 포함량 소진 뒤의 on-demand 한도와 사용량.
- 현재 구현은 `autoPercentUsed`, `apiPercentUsed`, `billingCycleEnd`만 보존하고 `billingCycleStart` 및 비용 필드를 버린다.
- 상세 일별 이벤트 API도 존재하지만 쿠키 인증, 페이지네이션, 호출량 증가가 필요하다. 월간 예산 페이스 판단에는 현재 RPC의 시작/종료/사용률만으로 충분하므로 이번 범위에서는 사용하지 않는다.

## 권장 UX

기존 Cursor 3행(`cur`, `api`, `bot`)과 창 크기를 유지하고, `cur`와 `api` 게이지에 **오늘까지 써도 되는 기대 사용률 위치를 세로 눈금으로 표시**한다.

```text
cur  █████████│────────  42%
              ↑ 기대 페이스 55%

api  █████████████│─────  68%
              ↑ 기대 페이스 55%
```

- 컬러 채움: 실제 누적 사용률. 기존 규칙인 80% 미만 파랑, 80~94% 주황, 95% 이상 빨강을 그대로 유지한다.
- 밝은 세로 눈금: 결제 주기 중 현재까지 경과한 비율.
- 채움 끝이 눈금 왼쪽이면 예산보다 천천히 사용 중, 오른쪽이면 예산보다 빠르게 사용 중이다.
- 기존 상세 문구 `42.00 / 100.00 reset 12d`는 Cursor 월간 행에서 `pace -13p · reset 12d`처럼 바꾼다.
  - 음수: 기대치보다 여유 있음.
  - 양수: 기대치보다 초과 사용 중.
  - `p`는 퍼센트포인트 차이다.
- `bot`은 별도 주간 한도이므로 현재 표시를 유지한다.
- 예상 월말 사용률은 초반 변동이 지나치게 커질 수 있어 상시 UI에는 표시하지 않는다.

## 계산 규칙

```text
cycle_duration = billingCycleEnd - billingCycleStart
elapsed_ratio = clamp((now - billingCycleStart) / cycle_duration, 0, 1)
expected_percent = elapsed_ratio * 100
pace_delta = actual_percent - expected_percent
```

예: 결제 주기의 40%가 지났는데 실제 사용률이 52%라면 `pace +12p`이며 게이지 채움이 기준 눈금보다 12포인트 앞선다.

판정 오차는 결제 주기 하루치 비율을 사용한다.

```text
daily_budget_percent = 100 / cycle_days
```

- `abs(pace_delta) <= daily_budget_percent`: 적정 범위.
- `pace_delta > daily_budget_percent`: 빠른 사용. 상세 pace 텍스트를 주황색으로 표시.
- `pace_delta > daily_budget_percent * 3`: 크게 빠른 사용. 상세 pace 텍스트를 빨간색으로 표시.
- 게이지 자체 색은 기존 누적 사용률 기준에서 변경하지 않는다.

날짜가 없거나 파싱 불가, 종료가 시작보다 이르거나 같음, 이미 종료된 오래된 응답이면 눈금과 pace 문구만 숨기고 기존 사용률/리셋 표시는 유지한다. 결제 주기 시작 직후에도 월말 예측은 하지 않고 실제 대비 기준 눈금만 표시한다.

## 구현 계획

### `cursor_usage.py`

- `normalize_cursor_usage()`에서 `billingCycleStart`와 `billingCycleEnd`를 읽는다.
- `_percent_window()`가 선택적으로 `periodStart`와 `periodEnd`를 보존하도록 확장한다.
- `cursorModels`와 `otherModels`에 동일한 결제 주기 메타데이터를 넣고 기존 `resetAt = billingCycleEnd` 호환성을 유지한다.
- 비용 값으로 `autoPercentUsed`/`apiPercentUsed`를 재계산하지 않고 Cursor가 제공한 퍼센트를 그대로 사용한다.
- `dump_cursor_usage()` 진단에는 토큰이나 원본 payload를 출력하지 않고, 허용 목록에 있는 주기 시작/종료와 정규화된 퍼센트만 추가한다.

### `ccusage.py`

- 기존 `normalize_timestamp()`를 재사용하는 순수 계산 함수로 월간 페이스 정보를 만든다.
- 반환값은 `expectedPercent`, `deltaPoints`, `dailyBudgetPercent`, 상태(`under`/`on`/`over`/`severe`)로 제한한다.
- 시스템 로컬 시간대가 아니라 epoch 기준으로 계산해 시간대 차이를 없앤다.

### `ccusage_window.pyw`

- `GaugeBar`에 선택적 `marker_percent` 인자를 추가한다.
- 트랙과 실제 사용량을 먼저 그린 뒤, 기대 페이스 위치에 1~2px 밝은 세로 눈금을 그린다. 0%/100%에서도 보이도록 캔버스 안쪽으로 위치를 제한한다.
- `_update_cursor_usage()`에서만 `cur`와 `api` 행에 페이스 계산 결과를 전달한다.
- `_update_row()`는 선택적 pace 메타데이터가 있을 때 상세 문구를 `pace ±Np · reset …`으로 표시한다.
- Codex, CommandCode, Grok Bot 렌더링과 기존 4열·3행 레이아웃은 변경하지 않는다.
- 주황/빨강 pace 상태는 상세 텍스트에만 적용하고 기존 게이지 색상 임계값은 유지한다.

### 문서

- `README.md`의 Cursor 설명에 “월간 막대의 세로 눈금은 현재 결제 주기의 기대 소비 위치”라는 짧은 범례를 추가한다.
- 비공개 API이며 응답 구조가 바뀔 수 있다는 기존 경고와 30초 최소 갱신 정책을 유지한다.

## 검증

- 모의 payload로 `billingCycleStart`, `billingCycleEnd`, `autoPercentUsed`, `apiPercentUsed`가 두 월간 창에 정확히 보존되는지 확인한다.
- 결제 주기 25%/50%/90% 경과 시 눈금이 각각 해당 위치에 표시되는지 확인한다.
- 실제 사용률이 기대치보다 낮음/비슷함/높음/크게 높음인 경우 delta와 상세 텍스트 색을 확인한다.
- 시작일 누락, 잘못된 타임스탬프, `end <= start`, 만료된 주기에서는 기존 사용률은 유지되고 눈금만 사라지는지 확인한다.
- 기존 `bot`, Codex, CommandCode 행의 게이지·색상·리셋 문구가 바뀌지 않는지 확인한다.
- 실제 Cursor 세션에서는 `python .\cursor_usage.py`의 정규화된 진단값과 Cursor 대시보드의 주기/퍼센트를 비교하되 토큰과 원본 응답은 출력하거나 저장하지 않는다.
- Python 문법 검사 후 배포용 `dist\ccusage-monitor.exe`를 다시 빌드한다.

## 참고 자료

- Cursor 공식 사용량 설명: https://cursor.com/help/models-and-usage/usage-limits
- GetCurrentPeriodUsage 구조 문서: https://pacebar.cbnsndwch.dev/docs/providers/cursor
- 비공개 대시보드 API 구조: https://gist.github.com/dmwyatt/1e9359b1862e7cbfe1e754fe4c8db764

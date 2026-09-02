# ccusage-monitor 작업 가이드

## 제품 범위와 UI

- 이 프로젝트는 Codex, Cursor, CommandCode 사용량을 작은 항상 위 Windows 창에서 보여주는 도구다.
- 화면은 설정에 따라 `Codex | Cursor | CommandCode 계정…`을 가로로 이어 붙인다. Codex 열은 항상 있다. Cursor 열은 `cursor.enabled`가 `false`가 아니면 표시한다. CommandCode 열은 `config.json`의 `commandcode_accounts` 개수만큼만 표시한다(0~2).
- `config.json`이 없거나 `commandcode_accounts`가 없으면 CommandCode 열을 만들지 않는다. 계정이 하나면 한 열만 만든다.
- 창 너비는 보이는 열 수에 비례한다. 열 하나당 210px에 크롬 30px를 더한다(4열일 때 기존 870px).
- 각 열은 같은 높이의 3행 자리를 쓴다. Codex와 CommandCode는 제목과 `5h` 사이에 약간의 여백을 두고, `5h`와 `7d` 사이 여백은 Cursor 한 행 높이의 절반이다. Cursor는 `cur`, `api`, `bot`을 여백 없이 붙여 넣는다.
- Cursor `cur`/`api`는 월간 `autoPercentUsed` / `apiPercentUsed`다. `bot`은 Grok Bot 주간 `usagePercent`다.
- Cursor 월간 `cur`/`api` 게이지에는 이번 주 일요일까지 월간 예산 중 써도 되는 한도를 세로 눈금으로 표시하고, 상세는 `pace ±Np · reset …`으로 그 한도와의 차이를 보여 준다. `bot`·Codex·CommandCode는 기존 사용량/리셋 표시를 유지한다.
- 게이지 색상은 80% 미만 파랑, 80~94% 주황, 95% 이상 빨강을 유지한다. Cursor 월간 pace의 주황/빨강은 상세 텍스트에만 적용한다.
- 기본 Windows 제목 표시줄은 의도적으로 숨겨져 있다. 앱 내부의 드래그 가능한 제목 바와 `X` 닫기 버튼을 유지한다.

## 인증 정보와 설정

- API 키, Codex 토큰, `config.json`, 로컬 인증 파일, 로그, 민감한 값이 보이는 스크린샷은 절대 커밋하지 않는다.
- 기본 설정 파일은 `config.json`이다. 실행 파일과 같은 폴더를 먼저 보고, 없으면 상위 프로젝트 폴더를 확인한다. Git에는 값이 비어 있는 `config.example.json`만 올린다.
- `config.json`의 `commandcode_accounts`에는 0~2개의 계정을 넣을 수 있고, 각 계정은 `id`와 `api_key`를 가져야 한다. 창에 CommandCode 열을 만들려면 이 키가 필요하다.
- `id`는 인증에 사용하지 않는 화면용 식별자다. 화면에 `CommandCode(<id>)`로 표시된다.
- 창은 CommandCode를 환경 변수나 `~/.commandcode/auth.json`만으로 켜지 않는다. CLI `ccusage.py`만 기존 환경 변수와 로컬 인증 파일을 쓴다.
- `ccusage.py`는 현재 로컬 CommandCode 인증 파일의 `userId`를 화면용 fallback 값으로만 읽을 수 있다. 진단 출력에 실제 값을 노출하지 않는다.
- `config.json`의 `cursor.enabled`가 `false`이면 Cursor 열을 생략하고 창 너비도 줄인다. 키가 없으면 Cursor는 켠 상태로 둔다.

## Codex 사용량 연동

- Codex 사용량은 현재 로그인된 로컬 Codex CLI의 app-server로 읽는다. 인증 토큰을 복사·파싱·로그·저장하지 않는다.
- `CodexRateLimitClient` app-server 프로세스는 갱신마다 새로 만들지 말고 계속 재사용한다. 실제 요청 또는 프로세스 실패 때만 재시작한다.
- Windows에서는 실패 복구 또는 앱 종료 때만 Codex 프로세스 트리 전체를 종료한다. 갱신마다 `codex`를 실행하는 구조로 되돌리지 않는다.
- `codex app-server`와 `taskkill`을 포함한 모든 보조 프로세스는 Windows 숨김 실행 옵션을 사용해야 한다. 재연결 중 콘솔 창이 나타나면 안 된다.
- Codex 백엔드의 503·timeout은 일시적 제공자 오류일 수 있다. UI 오류와 구분하고, 상세 내용은 `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`에만 남긴다.

## Cursor 사용량 연동

- Cursor 사용량은 현재 로그인된 로컬 Cursor IDE 세션으로 읽는다. 세션 토큰을 복사·로그·저장하지 않는다.
- 요청 순간에만 로컬 상태 DB에서 세션을 읽고, 요청이 끝나면 메모리에서 버린다.
- Cursor 월간 대시보드 API는 1초마다 호출하지 않는다. 최소 30초 간격을 유지하고 직전 성공 값을 재사용한다.
- Grok Bot 주간 사용량은 같은 세션으로 `GetSandUsageStatus`를 1초마다 읽는다. 실패하거나 한도가 없으면 `bot`만 비우고 월간 `cur`/`api`는 유지한다.
- Cursor의 503·timeout은 일시적 제공자 오류일 수 있다. UI 오류와 구분하고, 상세 내용은 `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`에만 남긴다.

## 빌드와 검증

- 기능 구현이나 수정이 끝나면 문법 검사 뒤에 배포용 exe도 다시 빌드한다.
- 배포용 실행 파일 이름은 반드시 `dist\ccusage-monitor.exe` 하나만 사용한다.
- 빌드 명령:

  ```powershell
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name ccusage-monitor .\ccusage_window.pyw
  ```

- 빌드 전 `ccusage-monitor.exe`가 실행 중인지 확인한다. 실행 중이면 Windows 파일 잠금 때문에 결과물을 덮어쓸 수 없다.
- `dist/`, `build/`은 Git에서 제외한다. 실행 파일과 일회성 빌드 spec은 커밋하지 않는다.
- Python 변경 뒤에는 최소한 아래 문법 검사를 실행한다.

  ```powershell
  python -m py_compile .\ccusage.py .\ccusage_window.pyw .\cursor_usage.py
  ```

- 계정 파싱 검증에는 placeholder나 mock만 사용한다. 실제 API 키나 인증 파일 내용을 출력하지 않는다.

## Git 작업 방식

- `main`이 아닌 작업 브랜치에서 변경한다.
- 현재 작업과 관련된 소스, 문서, 안전한 예시 파일만 커밋한다.
- 브랜치를 push하고 PR을 생성하거나 갱신한다. 사용자의 명시적 승인 없이 PR을 merge하지 않는다.

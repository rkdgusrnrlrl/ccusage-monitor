# ccusage-monitor 작업 가이드

## 제품 범위와 UI

- 이 프로젝트는 Codex와 CommandCode 사용량을 작은 항상 위 Windows 창에서 보여주는 도구다.
- 화면은 `Codex | CommandCode 계정 1 | CommandCode 계정 2`의 가로 3열 구조를 유지한다.
- 각 제공자는 `5h`, `7d` 두 행으로 짧은 사용량 창과 주간 사용량 창을 보여준다.
- 게이지 색상은 80% 미만 파랑, 80~94% 주황, 95% 이상 빨강을 유지한다.
- 기본 Windows 제목 표시줄은 의도적으로 숨겨져 있다. 앱 내부의 드래그 가능한 제목 바와 `X` 닫기 버튼을 유지한다.

## 인증 정보와 설정

- API 키, Codex 토큰, `config.json`, 로컬 인증 파일, 로그, 민감한 값이 보이는 스크린샷은 절대 커밋하지 않는다.
- 기본 설정 파일은 `config.json`이다. 실행 파일과 같은 폴더를 먼저 보고, 없으면 상위 프로젝트 폴더를 확인한다. Git에는 값이 비어 있는 `config.example.json`만 올린다.
- `config.json`의 `commandcode_accounts`에는 1개 또는 2개의 계정을 넣을 수 있고, 각 계정은 `id`와 `api_key`를 가져야 한다.
- `id`는 인증에 사용하지 않는 화면용 식별자다. 화면에 `CommandCode(<id>)`로 표시된다.
- `config.json`이 없을 때만 기존 환경 변수 방식을 fallback으로 유지한다.
  - 단일 계정: `COMMANDCODE_API_KEY` 또는 `COMMAND_CODE_API_KEY`
  - 개인 계정: `COMMANDCODE_API_KEY_PERSONAL`, `COMMANDCODE_USER_ID_PERSONAL`
  - 업무 계정: `COMMANDCODE_API_KEY_WORK`, `COMMANDCODE_USER_ID_WORK`
- `ccusage.py`는 현재 로컬 CommandCode 인증 파일의 `userId`를 화면용 fallback 값으로만 읽을 수 있다. 진단 출력에 실제 값을 노출하지 않는다.

## Codex 사용량 연동

- Codex 사용량은 현재 로그인된 로컬 Codex CLI의 app-server로 읽는다. 인증 토큰을 복사·파싱·로그·저장하지 않는다.
- `CodexRateLimitClient` app-server 프로세스는 갱신마다 새로 만들지 말고 계속 재사용한다. 실제 요청 또는 프로세스 실패 때만 재시작한다.
- Windows에서는 실패 복구 또는 앱 종료 때만 Codex 프로세스 트리 전체를 종료한다. 갱신마다 `codex`를 실행하는 구조로 되돌리지 않는다.
- `codex app-server`와 `taskkill`을 포함한 모든 보조 프로세스는 Windows 숨김 실행 옵션을 사용해야 한다. 재연결 중 콘솔 창이 나타나면 안 된다.
- Codex 백엔드의 503·timeout은 일시적 제공자 오류일 수 있다. UI 오류와 구분하고, 상세 내용은 `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`에만 남긴다.

## 빌드와 검증

- 배포용 실행 파일 이름은 반드시 `dist\ccusage-monitor.exe` 하나만 사용한다.
- 빌드 명령:

  ```powershell
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name ccusage-monitor .\ccusage_window.pyw
  ```

- 빌드 전 `ccusage-monitor.exe`가 실행 중인지 확인한다. 실행 중이면 Windows 파일 잠금 때문에 결과물을 덮어쓸 수 없다.
- `dist/`, `build/`은 Git에서 제외한다. 실행 파일과 일회성 빌드 spec은 커밋하지 않는다.
- Python 변경 뒤에는 최소한 아래 문법 검사를 실행한다.

  ```powershell
  python -m py_compile .\ccusage.py .\ccusage_window.pyw
  ```

- 계정 파싱 검증에는 placeholder나 mock만 사용한다. 실제 API 키나 인증 파일 내용을 출력하지 않는다.

## Git 작업 방식

- `main`이 아닌 작업 브랜치에서 변경한다.
- 현재 작업과 관련된 소스, 문서, 안전한 예시 파일만 커밋한다.
- 브랜치를 push하고 PR을 생성하거나 갱신한다. 사용자의 명시적 승인 없이 PR을 merge하지 않는다.

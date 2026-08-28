# ccusage-monitor

CommandCode와 Codex의 사용량을 작은 Windows 창에서 함께 확인하는 모니터링 도구야.

## 주요 기능

- 항상 위에 표시되는 가로형 컴팩트 창
- Codex 5시간 / 7일 사용량 표시
- CommandCode 5시간 / 주간 사용량 표시
- 사용률 게이지와 reset까지 남은 시간 표시
- 80% 이상 주황색, 95% 이상 빨간색 표시
- 기본 10초 주기 자동 갱신
- API 요청을 백그라운드에서 처리해 창이 멈추지 않음
- CommandCode와 Codex 중 한쪽에 문제가 생겨도 다른 쪽은 계속 표시
- Codex app-server를 재사용해 반복 실행과 강제 종료를 방지
- Codex 연결 오류 및 재연결 기록을 `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`에 저장

## 요구 사항

- Windows
- Python 3.10 이상
- Codex CLI 설치 및 로그인
- CommandCode API 키

## 설정

CommandCode API 키는 다음 순서로 찾는다.

1. `COMMANDCODE_API_KEY` 환경 변수
2. `COMMAND_CODE_API_KEY` 환경 변수
3. `%USERPROFILE%\.commandcode\auth.json`

예시:

```powershell
$env:COMMANDCODE_API_KEY = "your-commandcode-api-key"
```

Codex 사용량은 별도 토큰을 저장하지 않고, 현재 로그인된 Codex CLI의 로컬 app-server 인터페이스를 사용한다.

```powershell
codex login
```

## 실행

PowerShell에서 실행:

```powershell
python .\ccusage_window.pyw
```

갱신 주기를 5초로 변경:

```powershell
python .\ccusage_window.pyw --interval 5
```

`-i` 옵션도 사용할 수 있다. 최소 갱신 주기는 2초다.

## 파일 구성

- `ccusage.py`: CommandCode API 조회 및 공통 사용량 포맷팅 로직
- `ccusage_window.pyw`: 항상 위에 표시되는 Windows GUI

## 참고

Codex 사용량 응답은 설치된 Codex CLI의 app-server 프로토콜에 의존한다. Codex CLI가 크게 업데이트되어 해당 인터페이스가 변경되면 Codex 표시 기능을 조정해야 할 수 있다.

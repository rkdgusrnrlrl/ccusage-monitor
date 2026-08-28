# ccusage-monitor

CommandCode와 Codex의 사용량을 작은 Windows 창에서 함께 확인하는 모니터링 도구야.

## 주요 기능

- 항상 위에 표시되는 가로형 컴팩트 창
- Codex 5시간 / 7일 사용량 표시
- CommandCode 5시간 / 주간 사용량 표시
- 사용률 게이지와 reset까지 남은 시간 표시
- 80% 이상 주황색, 95% 이상 빨간색 표시
- 기본 1초 주기 자동 갱신
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

두 CommandCode 계정을 함께 표시하려면 각 계정의 키와 사용자 ID를 환경 변수로 설정한다.

```powershell
$env:COMMANDCODE_API_KEY_PERSONAL = "personal-api-key"
$env:COMMANDCODE_USER_ID_PERSONAL = "personal-user-id"
$env:COMMANDCODE_API_KEY_WORK = "work-api-key"
$env:COMMANDCODE_USER_ID_WORK = "work-user-id"
```

두 키 중 하나라도 설정하면 창은 Codex와 두 CommandCode 계정을 나란히 표시한다. CommandCode API 응답에는 사용자 ID가 없으므로, 두 번째 계정의 ID는 해당 환경 변수로 제공해야 한다.

### 설정 파일 사용

환경 변수 대신 실행 파일 또는 `ccusage_window.pyw`와 같은 폴더에 `config.json`을 둘 수 있다. `dist` 폴더에 실행 파일을 둔 개발 환경에서는 프로젝트 루트의 `config.json`도 자동으로 찾는다. 먼저 `config.example.json`을 복사해 `config.json`으로 이름을 바꾸고, 각 계정의 실제 API 키를 입력한다.

```json
{
  "commandcode_accounts": [
    {
      "id": "personal",
      "api_key": "first-commandcode-api-key"
    },
    {
      "id": "work",
      "api_key": "second-commandcode-api-key"
    }
  ]
}
```

`id`는 화면의 `CommandCode(id)` 제목에만 쓰이며, 계정 구분용 별칭을 넣어도 된다. `config.json`은 `.gitignore`에 포함되어 GitHub에 올라가지 않는다. 설정 파일이 있으면 환경 변수보다 먼저 사용한다.

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

`-i` 옵션도 사용할 수 있다. 최소 갱신 주기는 1초다.

## 실행 파일 만들기

PyInstaller가 설치되어 있다면 다음 명령으로 단일 실행 파일을 만들 수 있다.

```powershell
pyinstaller --onefile --windowed --name ccusage-monitor ccusage_window.pyw
```

생성된 `dist\ccusage-monitor.exe`를 더블클릭해 실행하면 된다. 실행 파일을 사용하려면 Codex CLI가 설치되어 있고 `codex login`이 완료되어 있어야 한다.

## 파일 구성

- `ccusage.py`: CommandCode API 조회 및 공통 사용량 포맷팅 로직
- `ccusage_window.pyw`: 항상 위에 표시되는 Windows GUI

## 참고

Codex 사용량 응답은 설치된 Codex CLI의 app-server 프로토콜에 의존한다. Codex CLI가 크게 업데이트되어 해당 인터페이스가 변경되면 Codex 표시 기능을 조정해야 할 수 있다.

## 공개 및 보안 참고

- 이 프로젝트에는 API 키나 Codex 인증 토큰이 포함되어 있지 않다.
- CommandCode API 키는 환경 변수 또는 로컬 인증 파일에서만 읽는다.
- Codex 인증은 설치된 Codex CLI의 로그인 상태를 사용한다.
- CommandCode의 비공개 API와 Codex CLI app-server 인터페이스에 의존하므로, 서비스나 CLI 업데이트에 따라 동작이 바뀔 수 있다.
- PyInstaller로 생성한 `dist` 폴더와 실행 파일은 저장소에서 추적하지 않는다.

## License

MIT License. See [LICENSE](LICENSE).

# ccusage-monitor

CommandCode, Codex, Cursor의 사용량을 작은 Windows 창에서 함께 확인하는 모니터링 도구야.

## 주요 기능

- 항상 위에 표시되는 가로형 컴팩트 창
- Codex·CommandCode는 5시간 / 7일 사용량을 위·아래에 두고 가운데는 비운다
- CommandCode 열은 `config.json`의 `commandcode_accounts` 개수만큼만 나오고, 창 너비도 열 수에 맞춰 줄어든다
- Cursor는 월간 `cur`(Cursor Models) / `api`(Other Models)와 주간 `bot`(Grok Bot)을 붙여서 표시
- Cursor 월간 막대의 세로 눈금은 이번 주 일요일까지 월간 예산 중 써도 되는 한도다. 채움이 눈금보다 왼쪽이면 일요일까지 여유가 있고, 오른쪽이면 이번 주 한도를 넘긴 것이다
- Cursor 월간 상세는 `pace ±Np · reset …`로 기대치와의 퍼센트포인트 차이를 보여 준다
- 사용률 게이지와 reset까지 남은 시간 표시
- 80% 이상 주황색, 95% 이상 빨간색 표시. Cursor 월간 pace가 하루 예산을 넘으면 상세 문구만 주황, 사흘치를 넘으면 빨강으로 바꾼다
- 기본 1초 주기 자동 갱신
- Cursor 월간 사용량은 대시보드 조회 때문에 30초마다 갱신하고, Grok Bot은 1초마다 갱신한다
- API 요청을 백그라운드에서 처리해 창이 멈추지 않음
- 한 제공자에 문제가 생겨도 다른 쪽은 계속 표시
- Codex app-server를 재사용해 반복 실행과 강제 종료를 방지
- 연결 오류 및 재연결 기록을 `%LOCALAPPDATA%\ccusage-monitor\ccusage.log`에 저장

## 요구 사항

- Windows
- Python 3.10 이상
- Codex CLI 설치 및 로그인
- Cursor 열을 쓸 경우 Cursor IDE 설치 및 로그인
- CommandCode 열을 쓸 경우 `config.json`의 `commandcode_accounts`

## 설정

창의 CommandCode 열은 `config.json`의 `commandcode_accounts`로만 켠다. 설정 파일이 없거나 이 키가 없으면 CommandCode 열을 만들지 않고, 창 너비도 나머지 열만 남도록 줄인다. 계정이 하나면 한 열만 표시한다.

CLI `ccusage.py`의 API 키는 다음 순서로 찾는다.

1. `COMMANDCODE_API_KEY` 환경 변수
2. `COMMAND_CODE_API_KEY` 환경 변수
3. `%USERPROFILE%\.commandcode\auth.json`

예시:

```powershell
$env:COMMANDCODE_API_KEY = "your-commandcode-api-key"
```

### 설정 파일 사용

실행 파일 또는 `ccusage_window.pyw`와 같은 폴더에 `config.json`을 둔다. `dist` 폴더에 실행 파일을 둔 개발 환경에서는 프로젝트 루트의 `config.json`도 자동으로 찾는다. 먼저 `config.example.json`을 복사해 `config.json`으로 이름을 바꾸고, 각 계정의 실제 API 키를 입력한다.

```json
{
  "cursor": {
    "enabled": true
  },
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

`id`는 화면의 `CommandCode(id)` 제목에만 쓰이며, 계정 구분용 별칭을 넣어도 된다. `commandcode_accounts`를 빼거나 빈 배열로 두면 CommandCode 열은 나오지 않는다. `config.json`은 `.gitignore`에 포함되어 GitHub에 올라가지 않는다. Cursor 열을 끄려면 `"cursor": { "enabled": false }`를 넣는다. Cursor를 끄거나 CommandCode 계정 수가 바뀌면 창 너비도 함께 바뀐다.

Codex 사용량은 별도 토큰을 저장하지 않고, 현재 로그인된 Codex CLI의 로컬 app-server 인터페이스를 사용한다.

```powershell
codex login
```

Cursor 사용량도 별도 토큰을 저장하지 않는다. 이 컴퓨터에서 Cursor IDE에 로그인해 두면 로컬 세션으로 월간 Cursor Models / Other Models와 Grok Bot 주간 사용량을 읽는다. 월간 대시보드는 최소 30초 간격으로만 조회하고, Grok Bot은 1초마다 조회한다.

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

생성된 `dist\ccusage-monitor.exe`를 더블클릭해 실행하면 된다. 실행 파일을 사용하려면 Codex CLI가 설치되어 있고 `codex login`이 완료되어 있어야 한다. Cursor 열은 같은 컴퓨터의 Cursor IDE 로그인 상태를 사용한다.

## 파일 구성

- `ccusage.py`: CommandCode API 조회 및 공통 사용량 포맷팅 로직
- `cursor_usage.py`: 로컬 Cursor 세션으로 월간 사용량과 Grok Bot 주간 사용량을 읽는 로직
- `ccusage_window.pyw`: 항상 위에 표시되는 Windows GUI

## 참고

Codex 사용량 응답은 설치된 Codex CLI의 app-server 프로토콜에 의존한다. Codex CLI가 크게 업데이트되어 해당 인터페이스가 변경되면 Codex 표시 기능을 조정해야 할 수 있다.

Cursor 사용량은 Cursor IDE의 로컬 세션과 비공식 대시보드 인터페이스에 의존한다. 응답 구조가 바뀌면 Cursor 표시 기능을 조정해야 할 수 있다.

## 공개 및 보안 참고

- 이 프로젝트에는 API 키나 Codex/Cursor 인증 토큰이 포함되어 있지 않다.
- CommandCode API 키는 환경 변수 또는 로컬 인증 파일에서만 읽는다.
- Codex 인증은 설치된 Codex CLI의 로그인 상태를 사용한다.
- Cursor 인증은 설치된 Cursor IDE의 로그인 상태를 사용하며, 세션 값은 저장하거나 로그에 남기지 않는다.
- CommandCode의 비공개 API, Codex CLI app-server, Cursor 대시보드 인터페이스에 의존하므로, 서비스나 CLI 업데이트에 따라 동작이 바뀔 수 있다.
- PyInstaller로 생성한 `dist` 폴더와 실행 파일은 저장소에서 추적하지 않는다.

## License

MIT License. See [LICENSE](LICENSE).

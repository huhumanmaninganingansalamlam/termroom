# Termroom

[English](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.md) · [한국어](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.ko.md)

**PC에서 하던 터미널 작업을 브라우저에서 그대로 이어 쓰는 개인용 작업공간입니다.**

프로젝트 폴더에서 `termroom .`을 실행하면 브라우저에서 터미널과 파일을 열 수 있습니다.
터미널은 `tmux`가 유지하므로 브라우저를 닫거나 다른 기기로 옮겨도 실행 중인 작업은
계속 남아 있습니다.

노트북에서 시작한 빌드나 AI 작업을 휴대폰에서 확인하고, 원격 Linux 서버의 터미널과
파일도 같은 화면에서 다루는 흐름을 목표로 합니다.

> **현재 상태: early release.** Termroom은 Python CLI 패키지로 배포되며 아직 빠르게
> 발전하는 단계입니다. 초기 버전 사이에는 사용 흐름이나 UI가 조금씩 바뀔 수 있습니다.

## 이런 때 유용합니다

- PC에서 오래 걸리는 명령을 실행해 두고 휴대폰이나 태블릿에서 상태를 확인할 때
- 브라우저를 닫아도 터미널 작업을 끊지 않고 계속 유지하고 싶을 때
- 프로젝트 파일을 브라우저에서 확인하거나 다운로드·업로드하고 싶을 때
- 여러 로컬 프로젝트를 한 화면에서 오가고 싶을 때
- SSH로 접속하는 Linux 서버도 로컬 프로젝트와 비슷한 방식으로 사용하고 싶을 때

Termroom은 클라우드 IDE가 아닙니다. **내 Linux 컴퓨터 또는 내가 연결한 SSH 서버에서
실제로 실행되는 터미널과 파일을 브라우저 UI로 연결**합니다.

## 빠른 시작

### 1. 준비

현재 버전은 다음이 필요합니다.

- Linux
- Python 3.12+
- `tmux`

Ubuntu/Debian 계열에서 `tmux`가 없다면 예를 들어:

```bash
sudo apt install tmux
```

### 2. Termroom 설치

CLI 앱은 다른 Python 패키지와 격리해서 설치하는 것이 편합니다.
[`uv`](https://docs.astral.sh/uv/)를 사용한다면:

```bash
uv tool install termroom
```

또는 `pipx`:

```bash
pipx install termroom
```

일반 `pip` 설치도 지원합니다.

```bash
pip install termroom
```

설치가 끝나면 쉘에서 `termroom` 명령을 사용할 수 있습니다. `uv tool`과 `pipx`는
Termroom의 의존성을 다른 Python 앱과 분리해주기 때문에 권장합니다.

> `termroom: command not found`가 나오면 `uv tool update-shell`을 한 번 실행한 뒤
> 새 터미널을 여세요. (`uv tool`로 설치한 경우)

### 3. 내 프로젝트 열기

로그인 비밀번호는 Termroom 전역 설정에 한 번 저장합니다.

```bash
mkdir -p ~/.config/termroom
printf '%s\n' 'TERMROOM_PASSWORD=내가-사용할-비밀번호' 'TERMROOM_LOCALE=ko' > ~/.config/termroom/.env
chmod 600 ~/.config/termroom/.env
```

그다음 사용할 프로젝트 폴더로 이동해서 실행합니다.

```bash
cd ~/my-project
termroom .
```

Termroom이 백그라운드에서 시작되고 브라우저가 자동으로 열립니다. 화면에 나온 주소를
직접 열어도 됩니다. 로그인할 때 `~/.config/termroom/.env`에 저장한 비밀번호를 사용합니다.

다음부터 같은 PC에서 Termroom Core가 이미 실행 중이면:

```bash
cd ~/another-project
termroom .
```

처럼 다른 프로젝트를 추가할 수 있습니다.

## 무엇을 할 수 있나요?

### 터미널

- 브라우저에서 실제 shell/TUI 프로그램 사용
- 브라우저를 닫아도 `tmux`에서 작업 유지
- Workspace마다 여러 터미널 생성·이름 변경·종료
- 다시 접속했을 때 같은 터미널로 복귀
- 모바일 한글/일본어/중국어 IME 입력과 터치 보조 키
- 긴 명령을 편하게 수정하는 명령 편집 모드
- 터미널 글자 크기 설정
- 기존 `tmux` scrollback 검색과 복사

### 파일

- 프로젝트 폴더 탐색과 검색
- 여러 파일·폴더를 선택해서 ZIP으로 다운로드
- 여러 파일 업로드와 덮어쓰기 확인
- 새 파일/폴더 만들기, 이름 변경, 삭제
- 작은 텍스트 파일을 브라우저에서 바로 편집
- 이미지/PDF, JSON/CSV, 큰 텍스트 일부 미리보기
- 로컬 프로젝트와 SSH 프로젝트에서 같은 파일 UI 사용

### 최근 작업

- 최근 생성·수정된 파일 확인
- 최근 사용한 터미널과 활동 시각 확인
- 계속 커지고 있는 파일 표시
- dependency/cache/hidden directory는 기본적으로 제외
- 프로젝트별 `.termroomignore` 지원

## 기본 사용법

Termroom에서는 **Workspace = 하나의 프로젝트 폴더**라고 생각하면 됩니다.

```text
Computer
└─ Workspace (프로젝트 폴더)
   ├─ Terminal
   ├─ Files
   └─ Recent
```

`Computer`는 이 PC 또는 등록한 SSH Linux 서버입니다.

### 로컬 프로젝트

가장 간단한 방법은 프로젝트 폴더에서 실행하는 것입니다.

```bash
cd ~/projects/example
termroom .
```

웹 화면에서도 이 컴퓨터의 다른 허용 폴더를 Workspace로 열 수 있습니다.

**위치 추가 → 폴더 찾아보기**를 누르면 홈 폴더부터 하위 폴더를 눌러가며 선택할 수
있습니다. 경로를 알고 있다면 절대 경로를 직접 입력하는 방식도 그대로 사용할 수 있습니다.

### SSH 서버

웹 화면에서 **SSH 컴퓨터 추가**를 선택한 뒤:

```text
SSH 주소 입력
→ host key fingerprint 확인
→ password / Termroom 관리 키 / 기존 키 중 인증 방법 선택
→ 원격 홈 폴더에서 프로젝트 폴더 찾아보기 또는 경로 직접 입력
→ Workspace 열기
```

원격 Linux에는 SSH 서버와 `tmux`가 설치되어 있어야 합니다.

## 자주 쓰는 명령

```bash
termroom .                    # 현재 프로젝트 열기
termroom /path/to/project     # 지정한 프로젝트 열기
termroom attach .             # 현재 Workspace의 tmux에 직접 attach
termroom stop .               # 현재 Workspace의 tmux session 종료
termroom stop --core          # Termroom 웹 Core 종료
```

여기서 **Core**는 브라우저 UI를 제공하는 Termroom 백그라운드 프로세스입니다. 보통 한
컴퓨터에서 하나만 실행되고 여러 Workspace를 함께 관리합니다.

Docker나 systemd가 프로세스를 직접 관리하는 환경에서는 foreground로 실행할 수 있습니다.

```bash
termroom /srv/projects --foreground --no-open
```

## 다른 기기에서 접속하기

기본값은 `127.0.0.1`이라 **Termroom을 실행한 PC에서만 접속할 수 있습니다.**

휴대폰이나 태블릿 등 다른 기기에서 사용할 때는 기존 LAN, VPN/Tailscale, 또는 직접
운영하는 HTTPS reverse proxy 사용을 권장합니다.

외부 인터페이스에 직접 열어야 한다면 명시적으로 지정합니다.

```bash
termroom ~/projects --host 0.0.0.0
```

HTTPS reverse proxy 뒤에서는 secure cookie도 켭니다.

```bash
termroom ~/projects --host 0.0.0.0 --secure-cookie
```

공개 인터넷에 그대로 노출하는 용도로 설계된 서비스는 아닙니다.

## 비밀번호와 설정

로그인에는 `TERMROOM_PASSWORD`가 필요합니다. 일반 설치에서는 Termroom 전역 설정 파일인
`~/.config/termroom/.env`에 두는 것을 권장합니다.

```text
TERMROOM_PASSWORD=change-this-password
TERMROOM_LOCALE=ko
```

`TERMROOM_LOCALE`은 아직 웹에서 언어를 직접 고르지 않은 브라우저의 초기 UI 언어를
정합니다. `en` 또는 `ko`를 사용할 수 있고, 웹에서 사용자가 직접 선택한 언어는 해당
브라우저에 저장되어 이 기본값보다 우선합니다.

다른 사용자가 읽지 못하도록 권한을 제한합니다.

```bash
chmod 600 ~/.config/termroom/.env
```

쉘이나 서비스 관리자가 비밀번호를 제공하는 환경에서는 `TERMROOM_PASSWORD` 환경변수가
전역 `.env`보다 우선합니다. 기존 버전과의 호환을 위해 프로젝트 폴더의 `.env`도 fallback으로
읽지만, Termroom 로그인 비밀번호를 프로젝트 설정과 섞지 않도록 전역 설정 사용을 권장합니다.

Termroom은 기본적으로 비밀번호 최소 길이를 강제하지 않습니다. 운영 정책이 필요하면:

```bash
TERMROOM_MIN_PASSWORD_LENGTH=12
```

영속 설정은 기본적으로 `~/.config/termroom/`에 저장됩니다.

```text
.env                 # 선택: 전역 비밀번호 / 기본 언어
termroom.sqlite3
access-token
credential-key
credentials/
ssh/
```

다른 위치는 `--config-dir` 또는 `TERMROOM_CONFIG_DIR`로 지정할 수 있습니다.

SSH 비밀번호는 프로젝트 파일이나 SQLite DB에 평문으로 저장하지 않고 config directory의
owner-only encrypted credential 파일에 저장합니다. 이 저장소가 hardware-backed vault를
대체하는 것은 아닙니다.

## Docker Compose

Docker로 실행하고 싶다면:

```bash
cp .env.example .env
# TERMROOM_PASSWORD 변경
docker compose up -d --build
```

기본 Compose는 다음을 사용합니다.

- `termroom-config:/config` — DB, SSH 키, credential 등 영속 설정
- `./workspaces:/workspaces` — Core가 접근할 로컬 폴더
- `127.0.0.1:8765:8765` — 기본 host publish

운영 환경에 맞게 volume/bind mount를 바꾸면 됩니다.

## PWA와 언어

브라우저에서 설치 가능한 PWA manifest/icon을 제공합니다. Service Worker는 인증된
Workspace/file/terminal 응답을 offline cache하지 않습니다.

UI 기본 언어는 영어이며 상단 언어 선택에서 한국어로 바꿀 수 있습니다. locale source는
`termroom/locales/`에 있습니다.

## 기술 구조

터미널 작업이 유지되는 핵심은 브라우저가 아니라 `tmux`입니다.

```text
Browser / PWA
      │
      ▼
Termroom Core
  ├─ Local filesystem + local tmux
  └─ SFTP + OpenSSH + remote tmux
```

로컬 터미널은 PTY/WebSocket을 통해 브라우저의 xterm.js와 연결되고, SSH Workspace는
로컬 OpenSSH와 SFTP를 사용합니다. 자세한 데이터 모델과 보안 경계는
[`docs/architecture.md`](docs/architecture.md)를 참고하세요.

## 개발

소스에서 개발할 때는 프로젝트용 virtual environment를 사용합니다.

```bash
git clone https://github.com/huhumanmaninganingansalamlam/termroom.git
cd termroom
uv sync --all-groups
```

검증 명령:

```bash
uv run --frozen ruff check termroom tests
uv run --frozen pytest
node --check termroom/static/app.js
node --check termroom/static/terminal.js
docker compose config
```

사용자 화면을 바꾸는 경우 모바일·태블릿·데스크톱 실제 브라우저에서도 전체 흐름을
확인합니다.

## 문서

- [`docs/architecture.md`](docs/architecture.md) — 데이터 모델, terminal/file pipeline,
  보안 경계
- [`docs/mobile-terminal.md`](docs/mobile-terminal.md) — 모바일 terminal/IME 입력 계약
- [`docs/i18n.md`](docs/i18n.md) — locale 추가와 번역 규칙
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — 개발 환경과 기여 규칙

## 라이선스

Termroom 자체 코드는 [MIT License](LICENSE)로 배포합니다.
Vendored `@xterm/xterm 6.0.0`도 MIT License이며 원 저작권 고지는
[`termroom/static/vendor/xterm.LICENSE`](termroom/static/vendor/xterm.LICENSE)에 보존합니다.

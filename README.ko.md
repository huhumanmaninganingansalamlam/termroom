# Termroom

[English](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.md) · [한국어](https://github.com/huhumanmaninganingansalamlam/termroom/blob/main/README.ko.md)

**PC에서 하던 터미널 작업을 브라우저에서 그대로 이어 쓰는 개인용 작업공간입니다.**

프로젝트 폴더에서 `termroom .`을 실행하면 브라우저에서 터미널과 파일을 열 수 있습니다.
터미널은 `tmux`가 유지하므로 브라우저를 닫거나 다른 기기로 옮겨도 실행 중인 작업은
계속 남아 있습니다.

노트북에서 시작한 빌드나 AI 작업을 휴대폰에서 확인하고, 직접 정한 몇 개의 프로젝트
명령을 실행하며, 원격 Linux 또는 macOS 컴퓨터의 터미널과 파일도 같은 화면에서 다루는 흐름을
목표로 합니다.

> **현재 상태: early release.** Termroom은 Python CLI 패키지로 배포되며 아직 빠르게
> 발전하는 단계입니다. 초기 버전 사이에는 사용 흐름이나 UI가 조금씩 바뀔 수 있습니다.

## 이런 때 유용합니다

- PC에서 오래 걸리는 명령을 실행해 두고 휴대폰이나 태블릿에서 상태를 확인할 때
- 브라우저를 닫아도 터미널 작업을 끊지 않고 계속 유지하고 싶을 때
- 프로젝트 파일을 브라우저에서 확인하거나 다운로드·업로드하고 싶을 때
- 여러 로컬 프로젝트를 한 화면에서 오가고 싶을 때
- SSH Linux 또는 macOS 컴퓨터와 outbound 연결만 가능한 Termroom Node를 같은 Workspace 흐름으로
  사용하고 싶을 때
- Workspace마다 명시적인 명령을 최대 3개 저장하고 root에서 바로 실행하고 싶을 때
- 현재 Python, JavaScript 또는 Bash 파일을 실행 명령 조합 없이 바로 실행하고 싶을 때
- Workspace snapshot, 공개 HTTPS Git 저장소 또는 ZIP을 다른 Remote에서 실행하고 나중에
  출력과 결과 파일을 회수하고 싶을 때
- 원격 실행 결과를 ZIP으로 받거나 충돌 없는 변경만 원래 Workspace로 가져오고 싶을 때

Termroom은 클라우드 IDE가 아닙니다. **내 Linux Core 컴퓨터 또는 내가 연결한 Remote에서
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
printf '%s\n' 'TERMROOM_PASSWORD=길고-고유한-비밀번호를-사용하세요' 'TERMROOM_LOCALE=ko' > ~/.config/termroom/.env
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
- 실제 PTY와 `tmux` 경로에서 Vim/Neovim과 alternate-screen TUI 사용
- 모바일 한글/일본어/중국어 IME 입력과 터치 보조 키
- 긴 명령을 편하게 수정하는 명령 편집 모드
- 터미널 글자 크기 설정
- 기존 `tmux` scrollback 검색과 복사

### 파일

- 프로젝트 폴더 탐색과 검색
- Workspace 열기의 폴더 탐색 화면에서 새 프로젝트 폴더를 만들고 바로 열기
- 여러 파일·폴더를 선택해서 ZIP으로 다운로드
- 여러 파일 업로드와 덮어쓰기 확인
- 새 파일/폴더 만들기, 이름 변경, 삭제
- 작은 텍스트 파일을 브라우저에서 바로 편집
- 모든 일반 파일을 persistent tmux Vim 세션으로 바로 열기. Neovim, Vim, Vi 순으로
  선택하며 같은 파일의 살아 있는 편집 세션을 재사용
- 이미지/PDF, JSON/CSV, 큰 텍스트 일부 미리보기
- Local, SSH, Termroom Node 프로젝트에서 같은 파일 UI 사용

### Workspace 명령 실행

- 지속 Workspace마다 사용자가 직접 정한 명령을 최대 3개 저장
- Workspace 상단의 눈에 보이는 실행 action에서 선택하고 Workspace root에서 실행
- 해당 computer에 이미 있는 tool, runtime, virtual environment와 사용자 권한을 그대로 사용
- package manifest에서 명령을 자동 추론하거나 argument, environment, task graph, 복잡한
  Run Recipe 설정은 제공하지 않음

### 현재 파일 실행

- 편집기에서 현재 Python 3, Node.js, Bash 또는 executable shebang 파일을 저장하고 실행
- Workspace마다 관리형 interactive Terminal 하나를 사용하고 정확한 exit status, 중지,
  강제 종료 제공
- 실행 상태를 저장된 파일과 연결하고 재접속 뒤에도 결과 복구
- 좁은 file shortcut이며 runtime이나 environment를 자동 설치하지 않음

### 원격 실행

- Local, SSH 또는 compatible Node Workspace 폴더, 공개 HTTPS Git 저장소, ZIP 하나를 등록한
  SSH 또는 compatible Node Remote의 임시 공간으로 복사
- 해당 Remote 사용자의 설치된 도구와 CPU/GPU/RAM으로 명령 하나 실행
- 브라우저 연결이 끊겨도 원격 전용 `tmux` session에서 실행 유지
- 준비가 끝나면 별도 로그 화면이 아니라 기존 Workspace Terminal·Files 화면을 그대로 사용
- command가 실패했더라도 file이 설정된 entry 수·directory 깊이·크기 안전 한도 안에 남아
  있으면 실행 folder를 결과 ZIP으로 다운로드
- Workspace Source는 추가·수정·충돌·건너뜀 file을 미리 본 뒤 적용 가능한 변경만 원래
  Workspace로 가져오기. 적용 대상은 작은 UTF-8 text file이며 새 file은 Source에 이미 있는
  directory 안에 있어야 함. 각 변경을 반영하기 직전에 현재 Source를 다시 확인하며 기존
  file은 atomic replace, 새 file은 기존 경로를 덮지 않는 방식으로 생성. Remote의 삭제는
  전파하지 않음. binary, 크기 초과, non-UTF-8, 새 하위 directory 안의 결과 등은 결과
  ZIP으로 받아 수동 merge. 이 검사는 외부 editor나 Git을 잠그는 CAS가 아님
- 완료 파일은 24시간 보관하고 임시 Workspace 상단에서 즉시 삭제 가능
- 환경 자동 구성, sandbox, 작업 queue, scheduler, 지속 sync와 Source 자동 반영은 제공하지 않음

### 최근 작업

- 최근 생성·수정된 파일 확인
- 최근 사용한 터미널과 활동 시각 확인
- 계속 커지고 있는 파일 표시
- dependency/cache/hidden directory는 기본적으로 제외
- 프로젝트별 `.termroomignore` 지원

Activity는 File Run과 Remote Run 결과로 돌아가기 위한 작은 보조 화면이며 Remote 연결 이력이나
monitoring dashboard가 아닙니다. Workspace 메뉴에는 확인 가능한 경우 CPU·memory·process
count의 제한된 추정값도 표시할 수 있지만 resource accounting이나 alert로 사용하지 않습니다.

## 기본 사용법

Termroom에서는 **Workspace = 하나의 프로젝트 폴더**라고 생각하면 됩니다.

```text
Computer
└─ Workspace (프로젝트 폴더)
   ├─ Run (명시적인 명령 최대 3개)
   ├─ Terminal
   ├─ Files
   └─ Recent
```

`Computer`는 이 Linux PC, SSH로 연결한 Linux 또는 macOS 컴퓨터, 또는 Termroom Node로
연결한 Linux 컴퓨터입니다.

Workspace 설정 메뉴에서 등록을 해제해도 실제 프로젝트 폴더, 파일, tmux 세션과 실행 중인
프로세스는 삭제되지 않습니다.

활동과 완료된 현재 파일 실행 기록은 30일간 보관합니다. 실행 중인 항목과 현재 실행
터미널에 연결된 항목은 사용이 끝날 때까지 유지합니다.

**원격 실행은 지속 프로젝트가 아닌 임시 Workspace shell입니다.** Source를 SSH 또는
compatible Node Remote로 복사한 뒤 기존 Terminal·Files UI를 재사용하지만 관리 폴더는
휘발성이며 최근 Workspace 목록에는 표시되지 않습니다.

### 로컬 프로젝트

가장 간단한 방법은 프로젝트 폴더에서 실행하는 것입니다.

```bash
cd ~/projects/example
termroom .
```

웹 화면에서도 이 컴퓨터의 다른 허용 폴더를 Workspace로 열 수 있습니다.

폴더 탐색 화면에서 **새 프로젝트**를 누르고 폴더 이름 하나만 입력하면 해당 폴더를 만든
뒤 기존 Workspace 흐름으로 바로 엽니다. 프로젝트 템플릿이나 개발 환경은 만들지 않습니다.

**위치 추가 → 폴더 찾아보기**를 누르면 홈 폴더부터 하위 폴더를 눌러가며 선택할 수
있습니다. 경로를 알고 있다면 절대 경로를 직접 입력하는 방식도 그대로 사용할 수 있습니다.

### SSH 서버

웹 화면에서 **SSH 컴퓨터 연결**을 선택한 뒤:

```text
SSH 주소 입력
→ host key fingerprint 확인
→ password / Termroom 관리 키 / 기존 키 중 인증 방법 선택
→ 원격 홈 폴더에서 프로젝트 폴더 찾아보기 또는 경로 직접 입력
→ Workspace 열기
```

원격 폴더 탐색 화면에서도 같은 **새 프로젝트** 동작을 사용할 수 있습니다. SFTP로 폴더를
만든 뒤 일반 SSH Workspace로 엽니다.

SSH 원격 컴퓨터는 Linux 또는 macOS를 사용할 수 있으며 SSH 서버, `/bin/bash`, `tmux`가
설치되어 있어야 합니다. Termroom은 해당 계정의 설정된 login shell에서 export된 명령 환경을
한 번 읽은 뒤 `command -v tmux`와 `tmux -V`로 실제 명령을 확인합니다. Homebrew나 다른
package manager의 디렉터리를 가정하지 않으며, 일반 SSH 로그인에서 사용할 수 있는 명령을
그 환경에서 찾아 이후 명령마다 shell 시작 파일을 다시 읽지 않고 재사용합니다.

### Termroom Node

Remote Linux가 outbound HTTP(S)/WS(S) 연결은 할 수 있지만 SSH server나 inbound port를 열 수
없다면 Termroom Node를 사용합니다.

```text
Core: Workspace 열기 → 컴퓨터 연결 → Node로 연결 → pairing code 생성
Remote: termroom node pair --core https://core.example --code <code> \
          --allow-root /home/user/projects
Core: Node fingerprint 확인 후 승인
Remote: termroom node install-service
```

`--allow-root`는 반복 지정할 수 있고 Node 사용자의 local control 아래에 남습니다. Core는
allowed root나 Node의 관리형 Remote Run root를 넓힐 수 없습니다. `install-service`는
systemd user service를 설치하고 즉시 시작합니다. `termroom node status`는 service와 Core
연결 상태를 함께 표시하고, `termroom node uninstall-service`는 Node identity와 pairing
설정을 보존한 채 service만 제거합니다.

Core가 private HTTPS CA를 사용한다면 `node pair`에 `--ca-file /path/to/core-ca.pem`을
추가합니다. 검증한 경로를 Node local config에 저장해 control connection에도 사용하며,
certificate 검증을 끄는 옵션은 제공하지 않습니다.

Core와 Node가 Tailscale 같은 운영자 관리 암호화 사설망에서 통신한다면 Core URL에 plain
HTTP를 사용할 수 있습니다. 일반 LAN이나 전송 기밀성이 따로 보장되지 않는 네트워크에서는
HTTPS를 사용하세요. 선택한 HTTP 또는 HTTPS scheme은 상시 control 연결의 WS 또는 WSS에도
그대로 적용됩니다.

pairing이 끝난 compatible Node는 SSH와 같은 Remote picker, Workspace Terminal, Files,
Workspace Run, File Run, Remote Run, 결과 회수와 재접속 흐름을 사용합니다. `/bin/bash`와
`tmux`는 필요하지만 inbound SSH 연결은 필요하지 않습니다.

#### Docker로 Node 실행

원격 Linux에 Termroom을 직접 설치하지 않으려면 같은 공식 이미지와 `compose.yaml`을
Node 모드로 실행할 수 있습니다. Node service는 포트를 열지 않고 outbound 연결만 사용하며,
설정·Node identity는 named volume에, 프로젝트 파일은 명시적인 host bind mount에 둡니다.

```bash
cp .env.example .env
chmod 600 .env
```

`.env`의 `TERMROOM_MODE` 하나를 `node`로 바꾸고 원격 컴퓨터의 mount 경로를 지정합니다.
`COMPOSE_PROFILES`는 이 값을 그대로 사용하므로 따로 바꾸지 않습니다.

```text
TERMROOM_MODE=node
TERMROOM_WORKSPACES_HOST_PATH=/home/user/projects
```

`TERMROOM_MODE=node`이면 같은 이미지가 Core 대신 Node process로 시작됩니다. 먼저 상시
container를 시작합니다. 아직 pairing되지 않은 container는 재시작 loop에 빠지지 않고 설정이
생길 때까지 실행 상태로 기다립니다.

```bash
docker compose up -d --remove-orphans
docker compose logs termroom-node
```

Core 화면에서 Node 연결 코드를 만든 뒤 같은 container 안에서 pairing합니다.

```bash
docker compose exec -u termroom termroom-node \
  termroom node --config-dir /config/node pair \
  --core https://termroom.example \
  --code <10분-일회용-code> \
  --allow-root /workspaces \
  --name build-node
```

pairing code는 10분 뒤 만료되고 pairing 요청에 사용되는 즉시 소비되는 일회용 값이므로
`.env`에 저장하지 않습니다. pairing은 Node identity와 설정을 named volume에 기록하며,
대기 중인 entrypoint가 이를 감지해 container restart 없이 Node를 자동으로 시작합니다.
Core에 표시된 fingerprint를 확인해 승인한 뒤 log를 확인합니다.

```bash
docker compose logs -f termroom-node
```

`--remove-orphans`는 같은 directory에서 Core와 Node 모드를 바꿀 때 이전 모드의 container가
남지 않게 합니다.

Docker에서는 `termroom node install-service`를 사용하지 않습니다. Compose의
`restart: unless-stopped`가 Node process를 관리합니다. 터미널과 명령은 host가 아니라
Node container 안에서 실행되므로 Git, Node.js, compiler, CUDA 등 추가 도구가 필요하면
공식 이미지를 기반으로 Node 전용 이미지를 만들어 설치해야 합니다. Core URL은 container
내부에서 접근 가능해야 하며, Tailscale 같은 암호화 사설망에서는 HTTP도 지원합니다.

## 자주 쓰는 명령

```bash
termroom serve .                    # 현재 프로젝트 열기
termroom serve /path/to/project     # 지정한 프로젝트 열기
termroom .                          # 이전 버전 호환 단축형
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

휴대폰이나 태블릿 등 다른 기기에서 사용할 때는 **Tailscale 같은 사설 VPN 안에서
운영하는 것을 권장합니다.** 가능하면 필요한 인터페이스에만 bind하세요. `0.0.0.0`은
접근 가능한 모든 LAN 인터페이스에도 Termroom을 노출합니다.

외부 인터페이스에 직접 열어야 한다면 명시적으로 지정합니다.

```bash
termroom ~/projects --host 0.0.0.0
```

Tailscale은 tailnet 기기 사이의 전송을 암호화하지만 일반 LAN의 HTTP는 그렇지 않습니다.
신뢰할 수 없는 네트워크에서는 HTTPS reverse proxy 뒤에 두고 secure cookie를 켭니다.

```bash
termroom ~/projects --host 0.0.0.0 --secure-cookie
```

**Termroom을 공개 인터넷에 직접 노출하지 마세요.** 비밀번호 로그인은 마지막 앱 경계일 뿐,
방화벽·사설 네트워크·HTTPS를 대신하지 않습니다. tailnet 안에서도 길고 고유한 비밀번호를
권장합니다.

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

로컬 Workspace는 기본적으로 활성화됩니다. Docker Core에서 SSH 또는 Termroom Node
Workspace만 열게 하려면 `.env`에 설정 하나를 추가합니다.

```text
TERMROOM_ALLOW_LOCAL_WORKSPACES=false
```

이 모드에서는 로컬 폴더와 Workspace가 UI에서 사라지고, 관련 탐색·생성·열기·파일·터미널
경로도 `404`를 반환합니다. SSH와 Termroom Node Workspace에는 영향이 없습니다.
`compose.yaml`의 `./workspaces:/workspaces` bind mount도 제거할 수 있습니다.

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
.env                 # 선택: 전역 비밀번호 / 기본 언어 / Workspace 정책
termroom.sqlite3
access-token
credential-key
credentials/
ssh/
```

다른 위치는 `--config-dir` 또는 `TERMROOM_CONFIG_DIR`로 지정할 수 있습니다.

SSH 비밀번호는 프로젝트 파일이나 SQLite DB에 평문으로 저장하지 않고 config directory의
owner-only encrypted credential 파일에 저장합니다. 이 저장소가 hardware-backed vault를
대체하는 것은 아닙니다. 로컬 암호화 키도 같은 owner-only config directory에 있기 때문입니다.
Termroom 로그인 비밀번호 자체는 `.env`에 평문으로 남습니다. 같은 위치의 키로 이를 다시
암호화해도 실질적인 보안 경계가 생기지 않습니다. 대신 Termroom은 비밀번호가 든 `.env`가
group/other 사용자에게 열려 있으면 시작을 거부하고, 로딩한 뒤 Core 프로세스 환경에서
`TERMROOM_PASSWORD`를 제거합니다.

## Docker Compose

PyPI 릴리스가 성공하면 같은 버전의 Docker 이미지를 GHCR에도 자동으로 배포합니다.
Docker 이미지 안에서도 해당 버전의 `termroom`을 PyPI에서 설치하므로 Python 패키지와
컨테이너 버전이 서로 어긋나지 않습니다. Docker 전용 수정은 Python package를 새로 배포하거나
기존 version tag를 덮어쓰지 않고 `main`에서 `latest`만 갱신할 수 있습니다.

배포된 이미지를 사용하려면:

```bash
cp .env.example .env
# Core는 TERMROOM_MODE=core 유지 후 TERMROOM_PASSWORD 변경
docker compose pull
docker compose up -d --remove-orphans
```

이미지는 `ghcr.io/huhumanmaninganingansalamlam/termroom:latest`로 제공하고 `0.1.1`,
`0.1` 같은 버전 태그도 함께 배포합니다. PyPI 패키지로 Docker 이미지를 로컬 빌드하려면
`docker compose up -d --build`를 사용할 수 있고, Dockerfile의 `TERMROOM_VERSION` build
argument로 설치할 버전을 지정할 수 있습니다.

기본 `.env`의 `TERMROOM_MODE=core`는 Core service를, `TERMROOM_MODE=node`는 outbound-only
Node service를 선택합니다. 기본 Compose는 다음을 사용합니다.

- `termroom-config:/config` — DB, SSH 키, credential 등 영속 설정
- `${TERMROOM_WORKSPACES_HOST_PATH}:/workspaces` — 선택한 host 프로젝트 폴더
- Core 모드에서 `${TERMROOM_BIND_HOST}:8765:8765` — 기본값은 loopback

tailnet 안에서 Core와 Node를 직접 연결하려면 `TERMROOM_BIND_HOST`를 Core host의 특정
Tailscale IP로 설정하고 Node를 `http://<tailscale-ip>:8765`에 pairing합니다. Node 쪽에
포트가 열리지는 않습니다. Tailscale Serve나 HTTPS reverse proxy를 쓰면 기본 loopback을
유지하세요. host firewall이 접근 범위를 제한하지 않는다면 `0.0.0.0`은 사용하지 마세요.

HTTPS reverse proxy를 사용한다면 `TERMROOM_BIND_HOST=127.0.0.1`을 유지하고
`TERMROOM_SECURE_COOKIE=true`로 설정합니다. Caddy·Nginx 같은 proxy는 원래 Host header와
WebSocket upgrade를 전달해야 하며 일반적인 reverse proxy 기본 설정은 둘 다 처리합니다.
Termroom의 native HTTP는 proxy 뒤에 남고 browser와 HTTPS/WSS Node는 proxy URL을 사용합니다.

Termroom은 Content Security Policy, Permissions Policy, frame, content-type, referrer
header를 직접 설정하고, 클라이언트가 gzip을 허용하면 적합한 동적 text 응답도
압축합니다. Proxy는 `Content-Encoding`과 `Vary`를 그대로 전달해야 합니다. 이 host에
`Content-Security-Policy`, `Permissions-Policy`, `X-Frame-Options`,
`X-Content-Type-Options`, `Referrer-Policy`를 한 번 더 추가하지 말고, 공용 proxy
template의 해당 지시문을 끄거나 upstream 값을 한 곳에서만 명시적으로 대체하세요.
Application 정책은 직접 local HTTP와 HTTPS/WSS reverse proxy를 모두 지원하며 local
URL을 강제로 HTTPS로 바꾸지 않습니다. Permissions Policy는 camera, microphone,
geolocation, payment, USB 접근만 차단하며 일반 browser workspace 흐름에서 사용하는
clipboard와 fullscreen API는 차단하지 않습니다.

Core를 SSH/Node-only로 운영할 때는 `TERMROOM_MODE=core`를 유지하면서
`TERMROOM_ALLOW_LOCAL_WORKSPACES=false`를 설정합니다. 이것은 Docker Node process를 선택하는
`TERMROOM_MODE=node`와 다른 Core 정책입니다.

## PWA와 언어

secure context에서 **설정 → Termroom 설치**를 선택하면 browser install prompt를 사용할 수
있습니다. iPhone/iPad Safari에서는 설정에서 **공유 → 홈 화면에 추가** 경로를 안내합니다.
설치 action은 작게 제공하고 이미 설치 앱으로 실행 중일 때는 숨깁니다.

PWA 설치에는 HTTPS 또는 browser가 인정하는 loopback secure context가 필요합니다. Service
Worker는 인증된 Workspace/file/terminal/Run 응답을 offline cache하지 않습니다.

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
  ├─ SFTP + OpenSSH + remote tmux
  └─ Termroom Node outbound WSS + remote filesystem/tmux
```

로컬 터미널은 PTY/WebSocket을 통해 브라우저의 xterm.js와 연결되고, SSH Workspace는
로컬 OpenSSH와 SFTP를 사용합니다. Node Workspace는 local allowed-root policy를 가진
pairing 및 capability 기반 Node 연결을 사용합니다. 자세한 데이터 모델과 보안 경계는
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
node --check termroom/static/remote_run.js
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

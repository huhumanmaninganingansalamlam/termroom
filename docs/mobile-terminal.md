# Termroom mobile terminal contract

모바일 terminal은 데스크톱 terminal을 작은 화면에 그대로 축소하는 기능이 아니다.
Termroom의 모바일 terminal은 **실제 PTY/xterm 호환성**과 **휴대폰 입력 편의성**을
동시에 만족해야 한다.

## 1. 기본 입력과 보조 편집기의 역할

기본 입력은 xterm의 실제 terminal input이다.

```text
software / hardware keyboard
→ xterm hidden textarea + IME composition
→ xterm onData
→ Termroom WebSocket
→ PTY
→ tmux
→ shell / TUI / REPL
```

이 경로는 `vim`, REPL, `fzf`, interactive CLI, coding-agent CLI처럼 키 입력을 즉시
해석하는 프로그램에 사용한다. Termroom이 한글 자모 조합을 별도로 재구현하거나
composition 중간 값을 PTY에 직접 전송하지 않는다.

`명령 편집 / Edit command`는 필요할 때만 여는 보조 도구다. 사용자가 평소에
"입력 모드"를 선택하게 만들지 않는다. terminal 자체가 항상 기본 입력 영역이다.

- 긴 명령 전체를 일반 textarea에서 확인·수정한다.
- 여러 줄 붙여넣기는 자동 실행하지 않는다.
- IME composition 중에는 submit하지 않는다.
- 사용자가 `실행 / Run`을 눌렀을 때만 하나의 command로 보낸다.
- 실행 또는 닫기 후 editor를 접어 terminal 출력 공간을 즉시 복구한다.

따라서 command editor는 IME 우회책이나 가짜 terminal이 아니다.

## 2. xterm dependency

Termroom은 vendored `@xterm/xterm 6.0.0`을 사용한다. asset bootstrap은 version marker를
검사하며 버전이 바뀌면 JS/CSS를 함께 갱신한다. vendored JS/CSS에는 고정 SHA-256을
두어 다른 버전이나 손상된 CDN 응답이 같은 파일명으로 조용히 들어오지 않게 한다.

IME 조합 알고리즘은 Termroom에서 fork하지 않는다. xterm upstream의 IME 수정과
terminal-mode 처리를 그대로 받을 수 있어야 한다.

Termroom이 직접 책임지는 부분은 다음이다.

- xterm textarea focus lifecycle
- visual viewport / software keyboard layout
- WebSocket reconnect와 PTY resize
- helper key semantics
- bracketed paste
- terminal focus와 command editor open/close

## 3. IME 규칙

xterm helper textarea에는 모바일 browser의 자동 text transform을 끈다.

- `autocomplete=off`
- `autocapitalize=off`
- `autocorrect=off`
- `spellcheck=false`
- `inputmode=text`

Termroom은 `compositionstart/update/end`를 관찰할 수 있지만 **composition update를
직접 WebSocket으로 보내면 안 된다.** 최종 commit은 xterm의 `onData`만 source of
truth로 사용한다.

자동 browser QA에서는 한국어 composition을 첫 자모부터 다음처럼 재현한다.

```text
ㅎ → 하 → 한 → 한ㄱ → 한그 → 한글 → … → 한글테스트
```

합격 조건:

- intermediate jamo WebSocket input: 0건
- final input: `한글테스트` 1건
- tmux 화면에서도 완성 음절이 유지됨

synthetic browser event는 실제 Gboard/Samsung Keyboard/iOS IME의 완전한 대체물이
아니다. 따라서 아래 실기기 검증도 release gate다.

## 4. Paste

terminal의 붙여넣기는 clipboard text를 WebSocket에 직접 write하지 않는다.

```text
clipboard
→ Terminal.paste(text)
→ xterm bracketed-paste handling
→ onData
→ PTY
```

shell/TUI가 bracketed paste mode를 켰다면 xterm이 `CSI 200~` / `CSI 201~`를 처리한다.
command editor에 붙여넣은 내용은 실행 전 textarea에서 수정할 수 있다.

## 5. Helper keys

화면의 Esc, Tab, Ctrl, C-c, 방향키 등도 별도 raw WebSocket bypass를 만들지 않는다.
가능한 입력은 `Terminal.input(..., true)`을 통해 xterm `onData` 경로로 보낸다.

방향키는 xterm의 `applicationCursorKeysMode`를 따라야 한다.

```text
normal cursor mode       ↑ → ESC [ A
application cursor mode  ↑ → ESC O A
```

왼쪽/오른쪽/아래 방향키도 같은 규칙을 따른다. 그래야 shell과 `vim` 같은 TUI에서
화면 helper와 실제 키보드가 서로 다른 키처럼 동작하지 않는다.

## 6. Vim/Neovim과 tmux 계약

Termroom은 browser IDE를 제공하는 대신 Vim/Neovim과 다른 TUI가 실제 tmux Terminal에서
자연스럽게 동작하도록 한다.

- Files 목록과 파일 보기의 `Vim에서 열기`는 선택한 파일을 새 shell에 명령어만 채워 넣지
  않고, 해당 Workspace의 tmux 안에서 바로 연다. 같은 파일의 Vim 창이 살아 있으면 그 창을
  재사용한다.
- browser의 작은 텍스트 편집기에서 `Vim에서 열기`를 누르면 CAS 저장이 먼저 성공해야 한다.
  외부 변경 충돌이 있으면 Vim으로 이동하지 않고 작성 중인 내용을 browser에 유지한다.
- Local과 SSH는 이 경로를 기본 제공하고, Node는 `terminal_editor` capability와
  `terminal.editor.open` fixed operation을 사용한다.
- Vim의 alternate screen은 viewport resize와 reconnect 뒤 현재 tmux 화면으로 다시 그려진다.
- insert mode의 CJK IME, Esc, Tab, Ctrl 조합과 방향키는 모두 실제 xterm input path를 쓴다.
- bracketed paste mode가 켜져 있으면 여러 줄 paste도 Vim이 구분할 수 있는 sequence로 보낸다.
- software keyboard가 열려도 Vim command line과 cursor가 있는 마지막 row를 볼 수 있어야 한다.
- browser text selection/copy와 tmux copy mode는 PTY input을 뜻하지 않는 동작을 임의로
  전송하지 않는다.
- browser reconnect는 editor state를 재구성하지 않는다. Vim process와 buffer의 지속성은
  tmux session이 소유한다.

모든 연결 화면은 하나의 tmux pane 문자 grid를 공유하며, 가장 최근에 실제 사용자 입력을
보낸 viewport의 rows와 columns가 크기를 결정한다. 다른 화면의 연결, focus, reload와
수동적인 viewport resize는 공유 grid를 바꾸지 않는다.

256-color와 bundled Nerd Font glyph를 기본 확인 범위로 둔다. true color는 browser, tmux와
target terminfo가 함께 지원할 때만 보장할 수 있으므로 실제 end-to-end 검증 없이 완전 지원으로
표현하지 않는다.

## 7. Software keyboard layout

software keyboard가 올라오는 동안 visual viewport가 크게 줄어든다. Termroom은
`window.visualViewport`를 기준으로 keyboard-open 상태를 판단한다.

terminal 직접 입력 중 keyboard가 열리면:

- workspace header와 bottom navigation을 잠시 숨긴다.
- terminal chrome은 유지한다.
- terminal 출력 영역과 cursor가 있는 마지막 row를 최대한 보존한다.
- 44px helper-key row는 keyboard 바로 위에 유지한다.

command editor 중 keyboard가 열리면:

- workspace header와 bottom navigation을 숨긴다.
- live-terminal helper row와 recent-command strip을 숨긴다.
- terminal context와 command textarea를 동시에 유지한다.

keyboard가 닫히면 정상 Workspace UI로 돌아온다.

### 짧은 landscape

software keyboard가 없어도 landscape에서 세로 공간이 매우 짧을 수 있다. terminal
페이지가 1023px 이하 폭, 520px 이하 높이의 landscape가 되면 다음 compact 규칙을
사용한다.

- Workspace header는 56px 기본 높이를 유지하고 현재 프로젝트와 홈 복귀를 남긴다.
- bottom navigation은 잠시 숨긴다.
- terminal chrome은 44px로 유지한다.
- 입력에 필수적이지 않은 보조 action을 숨긴다.
- command editor가 열리면 live-terminal helper row와 recent command를 숨긴다.
- terminal과 composer의 bottom이 visual viewport를 넘지 않아야 한다.

terminal chrome, terminal 본문, composer, bottom navigation의 좌우 padding은
`safe-area-inset-left/right`를 고려한다. notch가 있는 iPhone landscape에서도 핵심
터치 컨트롤과 terminal 문자가 safe area 밖에 걸리지 않게 한다.

## 8. Representative device matrix

자동 QA 외에 release 전 Android와 iOS의 대표 실기기 흐름을 확인한다. 모든 keyboard vendor,
tablet 크기와 browser version 조합을 매 release gate로 만들지는 않는다.

| 환경 | 필수 검증 |
|---|---|
| Android Chrome + installed PWA | 대표 한국어 keyboard의 composition, focus, keyboard open-close, rotation |
| iPhone Safari + installed PWA | composition, punctuation, copy/paste, safe area |
| Desktop 또는 Bluetooth keyboard | Esc/Tab/Ctrl/arrows, terminal focus와 resize |

각 기기에서 최소 다음 시나리오를 실행한다.

1. shell prompt에 `한글테스트` 입력
2. interactive CLI/REPL에서 `echo 한글`
3. `vim`/`nvim` insert mode 한글 입력, Esc와 command line visibility
4. alternate screen 상태의 resize, disconnect와 reconnect redraw
5. normal/application cursor mode의 방향키
6. Ctrl+C / Ctrl+D / Tab
7. 여러 줄 bracketed paste
8. command editor에서 한글·여러 줄 편집 후 Run
9. keyboard open/close와 portrait/landscape rotation
10. browser selection/copy와 tmux copy mode

실기기에서 확인하지 않은 항목을 문서나 release note에서 "완벽 지원"으로 표현하지
않는다.

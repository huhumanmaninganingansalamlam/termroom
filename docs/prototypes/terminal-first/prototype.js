(() => {
  "use strict";
  // Synthetic review fixture only. There are deliberately no fetch/WebSocket/form-post paths.
  const app = document.querySelector("#app");
  const layout = document.documentElement.dataset.layout;
  const params = new URLSearchParams(location.search);
  const navButtons = [...document.querySelectorAll(".view-nav [data-view]")];
  const aux = document.querySelector("#aux-surface");
  const scrim = document.querySelector("#surface-scrim");
  const auxTitle = document.querySelector("#aux-title");
  const announcer = document.querySelector("#announcer");
  const notice = document.querySelector("#notice");
  const delivery = document.querySelector("#delivery");
  const terminalTabs = document.querySelector("#terminal-tabs");
  const terminalViewport = document.querySelector("[data-terminal-viewport]");
  const compose = document.querySelector("#compose-panel");
  const composeToggle = document.querySelector("[data-action='compose']");
  const commandInput = document.querySelector("#command-draft");
  document.querySelector(".review-controls").hidden = params.get("review") !== "1";
  const scenarioInput = document.querySelector("#scenario");
  const keyboardInput = document.querySelector("#keyboard-open");
  const fileSearch = document.querySelector("#file-search");
  const drafts = new Map();
  let activeTerminal = "shell";
  let activeView = "terminal";
  let activeMenu = null;
  let menuTrigger = null;
  let previousViewTrigger = null;
  let fontSize = 14;
  let newTerminalCounter = 3;

  function announce(message) {
    announcer.textContent = "";
    window.requestAnimationFrame(() => { announcer.textContent = message; });
  }

  const terminalSurface = document.querySelector(".terminal-surface");
  const workspaceNav = document.querySelector(".view-nav");
  const appHeader = document.querySelector(".app-header");

  function setBackgroundAccessibility(showAux) {
    const overlay = showAux && layout === "dock";
    // Inert never changes terminal dimensions, DOM identity or stored draft/scroll.
    terminalSurface.inert = showAux;
    terminalSurface.setAttribute("aria-hidden", String(showAux));
    workspaceNav.inert = overlay;
    appHeader.inert = overlay;
    if (overlay) {
      workspaceNav.setAttribute("aria-hidden", "true");
      appHeader.setAttribute("aria-hidden", "true");
      aux.setAttribute("role", "dialog");
      aux.setAttribute("aria-modal", "true");
    } else {
      workspaceNav.removeAttribute("aria-hidden");
      appHeader.removeAttribute("aria-hidden");
      aux.removeAttribute("aria-modal");
      if (showAux) aux.setAttribute("role", "region");
      else aux.removeAttribute("role");
    }
  }

  function switchView(name, { focus = true, opener = null } = {}) {
    if (name !== "terminal" && !document.querySelector('[data-panel="' + name + '"]')) return;
    closeMenu({ restoreFocus: false });
    if (name !== "terminal" && opener) previousViewTrigger = opener;
    activeView = name;
    const isTerminal = name === "terminal";
    app.classList.toggle("panel-visible", !isTerminal);
    // Un-inert before restoring focus on return, and move focus into the drawer
    // before hiding its former trigger from the accessibility tree.
    if (isTerminal) setBackgroundAccessibility(false);
    aux.hidden = isTerminal;
    scrim.hidden = isTerminal || layout === "strip";
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== name;
    });
    navButtons.forEach((button) => {
      if (button.dataset.view === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (!isTerminal) {
      const labels = { files: "파일", recent: "최근", run: "실행", activity: "활동", settings: "설정" };
      auxTitle.textContent = labels[name];
      document.querySelector("#aux-kicker").textContent = name === "run" ? "이 작업 공간 · 실행" : "payments-service";
      if (focus) auxTitle.focus({ preventScroll: true });
      setBackgroundAccessibility(true);
    } else if (focus) {
      const button = navButtons.find((item) => item.dataset.view === "terminal");
      (button || terminalViewport).focus({ preventScroll: true });
    }
  }

  function dismissAux() {
    const opener = previousViewTrigger;
    switchView("terminal", { focus: false });
    const target = layout === "dock" && opener?.isConnected
      ? opener : navButtons.find((item) => item.dataset.view === "terminal");
    target?.focus({ preventScroll: true });
  }

  function openMenu(name, trigger) {
    closeMenu({ restoreFocus: false });
    const panel = document.querySelector("#" + name + "-menu");
    if (!panel) return;
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    activeMenu = panel;
    menuTrigger = trigger;
    const first = panel.querySelector("input,button");
    first?.focus({ preventScroll: true });
  }

  function closeMenu({ restoreFocus = true } = {}) {
    if (!activeMenu) return;
    activeMenu.hidden = true;
    menuTrigger?.setAttribute("aria-expanded", "false");
    if (restoreFocus) menuTrigger?.focus({ preventScroll: true });
    activeMenu = null;
    menuTrigger = null;
  }

  function switchTerminal(id, { focus = false } = {}) {
    if (!terminalTabs.querySelector('[data-terminal="' + id + '"]')) return;
    drafts.set(activeTerminal, commandInput.value);
    activeTerminal = id;
    commandInput.value = drafts.get(id) || "";
    terminalTabs.querySelectorAll("[data-terminal]").forEach((button) => {
      const selected = button.dataset.terminal === id;
      button.setAttribute("aria-pressed", String(selected));
      if (selected && focus) button.focus({ preventScroll: true });
    });
    document.querySelectorAll("[data-output]").forEach((node) => {
      node.hidden = node.dataset.output !== id;
    });
    terminalViewport.setAttribute("aria-label", "터미널 출력: " + id);
    document.querySelector("#rename-input").value = id;
    document.querySelector('[data-terminal="' + id + '"] .tab-dot')?.remove();
    announce(id + " 터미널");
  }

  function setMessage(message, severity = "warning") {
    notice.hidden = !message;
    notice.textContent = message || "";
    notice.dataset.severity = severity;
  }

  function setDelivery(message) {
    delivery.hidden = !message;
    delivery.textContent = message || "";
  }

  function setScenario(scenario, { navigate = true } = {}) {
    app.dataset.scenario = scenario;
    scenarioInput.value = scenario;
    document.querySelector("#panel-alert").hidden = true;
    document.querySelector("#empty-run").hidden = true;
    document.querySelectorAll(".saved-command").forEach((el) => { el.hidden = false; });
    document.querySelector("#file-run-actions").hidden = true;
    document.querySelector("#remote-result").hidden = true;
    setDelivery("");
    const label = document.querySelector("#header-status-text");
    const stateButton = document.querySelector("[data-status-button]");
    if (scenario === "offline") {
      label.textContent = "연결 끊김";
      setMessage("연결이 끊겼습니다. 기존 터미널 출력은 마지막으로 받은 내용입니다. 다시 연결해도 임의로 명령을 재실행하지 않습니다.");
    } else if (scenario === "refused") {
      label.textContent = "입력 제어 대기";
      setMessage("다른 화면에서 현재 입력을 제어하고 있습니다. 여기서는 명령 전송이 거부됩니다. 화면 내용을 읽거나 출력 기록을 볼 수 있습니다.");
    } else if (scenario === "conflict") {
      label.textContent = "연결됨";
      setMessage("파일이 외부에서 수정되어 저장이 거부됐습니다. 편집 중인 문서는 유지하고 변경 내용을 비교한 뒤 다시 저장하세요.");
      if (navigate) switchView("files");
      document.querySelector("#file-actions").hidden = false;
      const alert = document.querySelector("#panel-alert");
      alert.textContent = "저장 충돌 · 서버 파일의 수정 시각이 달라졌습니다. 현재 입력을 유지하고 비교하세요.";
      alert.hidden = false;
    } else if (scenario === "host-key") {
      label.textContent = "SSH 확인 필요";
      setMessage("SSH 호스트 키가 이전에 승인한 값과 다릅니다. 임의로 신뢰하지 않습니다. 호스트 관리자에게 변경 사실을 확인하세요.");
    } else if (scenario === "pairing") {
      label.textContent = "Node 연결 승인 필요";
      setMessage("Node 연결 승인 또는 페어링이 만료됐습니다. 대상 컴퓨터에서 다시 승인한 뒤 재연결하세요.");
    } else if (scenario === "unknown") {
      label.textContent = "결과 확인 중";
      setMessage("명령 전달 결과를 확인할 수 없습니다. 중복 실행 방지를 위해 자동으로 재시도하지 않습니다. 터미널 출력을 먼저 확인하세요.");
    } else if (scenario === "empty") {
      label.textContent = "연결됨";
      setMessage("");
      if (navigate) switchView("recent");
      document.querySelector('[data-panel="recent"] .panel-help').textContent = "최근 접근 항목이 없습니다. 파일을 열거나 명령을 실행하면 여기에 표시됩니다.";
      document.querySelector('[data-panel="recent"] .file-list').hidden = true;
      document.querySelector("#empty-run").hidden = false;
      document.querySelectorAll(".saved-command").forEach((el) => { el.hidden = true; });
    } else if (scenario === "file-run") {
      label.textContent = "연결됨";
      setMessage("");
      document.querySelector("#run-status").textContent = "app/services/invoice.py · 실행 중 · 경과 00:13 · 출력은 터미널에서 확인";
      document.querySelector("#file-run-actions").hidden = false;
      if (navigate) switchView("run");
    } else if (scenario === "remote-run") {
      label.textContent = "연결됨";
      setMessage("");
      document.querySelector("#run-status").textContent = "app/services/invoice.py · 최근 로컬 실행 성공";
      document.querySelector("#remote-result").hidden = false;
      if (navigate) switchView("run");
      setDelivery("원격 실행 완료 · build-node-02 · 종료 코드 0 · 결과 ZIP과 수집 검토 가능 (시안)");
    } else {
      label.textContent = "연결됨";
      setMessage("");
      document.querySelector("#run-status").textContent = "app/services/invoice.py · 실행 이력 없음";
      document.querySelector('[data-panel="recent"] .panel-help').textContent = "이 작업 공간의 최근 접근 항목";
      document.querySelector('[data-panel="recent"] .file-list').hidden = false;
    }
    if (stateButton) stateButton.setAttribute("aria-label", "연결 상태: " + label.textContent);
    announce("검토 상태: " + scenarioInput.selectedOptions[0].text);
  }

  function setKeyboardReview(open) {
    keyboardInput.checked = open;
    app.classList.toggle("keyboard-open", open);
    if (open && window.matchMedia("(max-width: 1023px)").matches) {
      switchView("terminal", { focus: false });
    }
  }

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view, { opener: button }));
  });
  document.querySelector("#close-aux").addEventListener("click", dismissAux);
  scrim.addEventListener("click", dismissAux);
  terminalTabs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-terminal]");
    if (button) switchTerminal(button.dataset.terminal, { focus: true });
  });
  document.querySelectorAll("[data-menu-toggle]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      const panel = document.querySelector("#" + trigger.dataset.menuToggle + "-menu");
      if (panel === activeMenu) closeMenu();
      else openMenu(trigger.dataset.menuToggle, trigger);
    });
  });
  document.querySelectorAll("[data-close-menu]").forEach((button) => {
    button.addEventListener("click", () => closeMenu());
  });
  document.addEventListener("pointerdown", (event) => {
    if (activeMenu && !activeMenu.contains(event.target) && !menuTrigger?.contains(event.target)) {
      closeMenu({ restoreFocus: false });
    }
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && layout === "dock" && !aux.hidden) {
      const selectors = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex=\"-1\"])";
      const elements = [...aux.querySelectorAll(selectors)].filter((element) => element.getClientRects().length > 0 && !element.closest("[hidden]"));
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (!first) { event.preventDefault(); auxTitle.focus({ preventScroll: true }); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === auxTitle || !aux.contains(document.activeElement))) {
        event.preventDefault(); last.focus({ preventScroll: true }); return;
      }
      if (!event.shiftKey && (document.activeElement === last || !aux.contains(document.activeElement))) {
        event.preventDefault(); first.focus({ preventScroll: true }); return;
      }
    }
    if (event.key === "Escape") {
      if (activeMenu) { closeMenu(); event.preventDefault(); return; }
      if (!compose.hidden) { closeCompose(); event.preventDefault(); return; }
      if (activeView !== "terminal") { dismissAux(); event.preventDefault(); }
      return;
    }
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = { "1": "terminal", "2": "files", "3": "recent", "4": "run" }[event.key];
    if (target) { event.preventDefault(); switchView(target, { opener: navButtons.find((b) => b.dataset.view === target) }); }
  });
  function openCompose() {
    compose.hidden = false;
    composeToggle.setAttribute("aria-expanded", "true");
    commandInput.focus({ preventScroll: true });
  }
  function closeCompose() {
    drafts.set(activeTerminal, commandInput.value);
    compose.hidden = true;
    composeToggle.setAttribute("aria-expanded", "false");
    composeToggle.focus({ preventScroll: true });
  }
  composeToggle.addEventListener("click", () => compose.hidden ? openCompose() : closeCompose());
  document.querySelector('[data-action="close-compose"]').addEventListener("click", closeCompose);
  commandInput.addEventListener("input", () => drafts.set(activeTerminal, commandInput.value));
  compose.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!commandInput.value.trim()) { announce("명령을 입력하세요."); return; }
    if (app.dataset.scenario === "refused") {
      setMessage("입력 제어가 다른 화면에 있어 전송이 거부됐습니다. 작성한 명령은 보존됩니다.");
      return;
    }
    setDelivery("가상 시안: 전송 동작만 미리 봤습니다. 실제 터미널에 명령이 전달되지는 않았습니다.");
    drafts.set(activeTerminal, commandInput.value);
    closeCompose();
  });
  document.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.key === "Ctrl") {
      const next = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", String(next));
    }
    announce(button.dataset.key + " 키 · 시안에서는 PTY에 전송하지 않음");
    closeMenu({ restoreFocus: false });
  }));

  fileSearch.addEventListener("input", () => {
    const q = fileSearch.value.toLocaleLowerCase();
    document.querySelectorAll("#file-list [data-file]").forEach((button) => {
      button.hidden = !button.dataset.file.toLocaleLowerCase().includes(q);
    });
  });
  document.querySelectorAll("[data-file]").forEach((button) => button.addEventListener("click", () => {
    document.querySelector("#selected-file").textContent = button.dataset.file;
    document.querySelector("#file-actions").hidden = false;
    document.querySelector("#file-actions button")?.focus({ preventScroll: true });
  }));
  document.querySelectorAll("[data-action]").forEach((button) => {
    if (["compose", "close-compose"].includes(button.dataset.action)) return;
    button.addEventListener("click", () => {
      const action = button.dataset.action;
      if (action === "new-terminal") {
        const id = "shell-" + (++newTerminalCounter);
        const tab = document.createElement("button");
        tab.type = "button";
        tab.dataset.terminal = id;
        tab.textContent = "shell " + newTerminalCounter;
        tab.setAttribute("aria-pressed", "false");
        terminalTabs.appendChild(tab);
        const output = document.createElement("div");
        output.className = "terminal-content";
        output.dataset.output = id;
        output.hidden = true;
        const pre = document.createElement("pre");
        pre.textContent = "# New terminal preview · " + id + "\ndev@laptop ~/projects/payments-service $ ";
        output.appendChild(pre);
        terminalViewport.appendChild(output);
        switchTerminal(id, { focus: true });
        setDelivery("가상 터미널 탭을 추가했습니다. 실제 tmux 세션에는 영향이 없습니다.");
      } else if (action === "rename") {
        const name = document.querySelector("#rename-input").value.trim();
        if (!name) return;
        const tab = terminalTabs.querySelector('[data-terminal="' + activeTerminal + '"]');
        tab.textContent = name;
        closeMenu();
        announce("가상 터미널 이름 변경: " + name);
      } else if (action === "close-terminal") {
        if (terminalTabs.querySelectorAll("[data-terminal]").length <= 1) {
          announce("마지막 터미널은 이 시안에서 닫을 수 없습니다.");
          return;
        }
        if (window.confirm("현재 터미널을 닫으시겠습니까? 시안에서만 삭제됩니다.")) {
          terminalTabs.querySelector('[data-terminal="' + activeTerminal + '"]').remove();
          document.querySelector('[data-output="' + activeTerminal + '"]')?.remove();
          drafts.delete(activeTerminal);
          switchTerminal(terminalTabs.querySelector("[data-terminal]").dataset.terminal);
          closeMenu();
        }
      } else if (action === "smaller" || action === "larger") {
        fontSize = Math.max(11, Math.min(22, fontSize + (action === "larger" ? 1 : -1)));
        document.querySelector("#font-display").textContent = fontSize + "px";
        document.querySelectorAll(".terminal-content pre").forEach((pre) => {
          pre.style.fontSize = fontSize + "px";
          pre.style.lineHeight = (fontSize + 5) + "px";
        });
      } else if (action === "focus-terminal") {
        closeMenu({ restoreFocus: false });
        terminalViewport.focus({ preventScroll: true });
      } else if (action === "file-run") {
        setScenario("file-run");
        setDelivery("파일 실행 미리보기 · 파일/환경 확인 후 실행, 중지 및 강제 종료는 실제 제품에서 제공됩니다.");
      } else if (action === "remote-run") {
        setScenario("remote-run");
        setDelivery("원격 실행 미리보기 · build-node-02 · 종료 코드 0 · 결과 ZIP/수집 검토 (전송 없음)");
      } else if (action === "recent-file") {
        switchView("files");
        document.querySelector("#file-actions").hidden = false;
      } else if (action === "recent-shell") {
        switchView("terminal");
        switchTerminal("shell");
      } else if (action === "output") {
        setDelivery("스크롤백/선택/검색은 실제 제품의 터미널 출력 화면에서 유지됩니다. 이 미리보기는 해당 경로를 열지 않습니다.");
      } else if (action === "saved-run") {
        setDelivery("저장된 명령 실행 미리보기: 실제 명령은 전송하지 않았습니다.");
      } else if (action === "save-command") {
        announce("저장 미리보기 · 서버에 기록하지 않았습니다.");
      } else if (action === "upload" || action === "create" || action === "browser-edit" || action === "vim-open") {
        announce("시안의 " + button.textContent.trim() + " 동작 · 파일은 변경하지 않습니다.");
      }
    });
  });
  scenarioInput.addEventListener("change", () => setScenario(scenarioInput.value));
  keyboardInput.addEventListener("change", () => setKeyboardReview(keyboardInput.checked));
  document.querySelectorAll("[data-review-layout]").forEach((button) => {
    button.classList.toggle("is-selected", button.dataset.reviewLayout === layout);
    button.addEventListener("click", () => {
      const query = new URLSearchParams(location.search);
      query.set("layout", button.dataset.reviewLayout);
      location.search = query.toString();
    });
  });
  setScenario(params.get("state") || "ready", { navigate: true });
  setKeyboardReview(params.get("keyboard") === "1");
  switchTerminal("shell");
})();

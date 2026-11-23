"""Human browser workflow: Terminal -> Files drawer -> command draft/menu.

This is real authenticated Chrome/Core/tmux with a disposable project and
browser profile, not a synthetic prototype or DOM-only simulator.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from tests.support.chrome import open_chrome_page
from tests.support.core import start_isolated_core


@pytest.mark.skipif(
    not (shutil.which("google-chrome") and shutil.which("tmux")),
    reason="Real Chrome and isolated tmux required",
)
def test_real_terminal_files_search_and_run_menu_preserve_draft_and_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    project = root / "qa"
    project.mkdir(parents=True)
    (project / "README-한글.md").write_text("Korean document content\n", encoding="utf-8")
    (project / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "hello.py").write_text("print('run')\n", encoding="utf-8")
    settings = Settings.create(
        root, state_dir=tmp_path / "state",
        login_password="workflow-only-password",
        access_token="workflow-only-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("qa")
    terminal = app.state.terminals.ensure_workspace(workspace)[0]
    core = start_isolated_core(app)
    try:
        with httpx.Client(base_url=core.base_url) as client:
            login = client.post("/login", data={"password": "workflow-only-password"})
            assert login.status_code == 303
            cookie = client.cookies.get("termroom_session")
            assert cookie
        with open_chrome_page(tmp_path / "chrome-profile") as browser:
            cdp = browser.cdp
            js = browser.evaluate
            cdp("Page.enable")
            cdp("Page.bringToFront")
            cdp("Runtime.enable")
            cdp("Network.enable")
            # Exercise the visible desktop Terminal rail. An 800px virtual
            # mobile viewport intentionally hides its bottom nav while the
            # software keyboard/composer is focused.
            cdp("Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900,
                "deviceScaleFactor": 1, "mobile": False,
            })
            assert cdp("Network.setCookie", {
                "name": "termroom_session", "value": cookie,
                "url": core.base_url + "/", "httpOnly": True,
            })["success"]
            cdp("Page.navigate", {
                "url": (
                    f"{core.base_url}/w/{workspace['id']}/terminal"
                    f"?terminal={terminal['id']}"
                ),
            })

            def until(expression: str, stage: str, seconds: float = 12) -> Any:
                deadline = time.monotonic() + seconds
                last = None
                while time.monotonic() < deadline:
                    try:
                        last = js(expression)
                        if last:
                            return last
                    except AssertionError:
                        pass
                    time.sleep(0.08)
                raise AssertionError(f"Chrome workflow stage {stage}: last={last!r}")

            until(
                "document.readyState === 'complete' && "
                "!document.querySelector('#terminal-copy-mode').disabled && "
                "!!document.querySelector('[data-terminal-files-trigger]')",
                "terminal DOM and copy entrypoint",
            )
            until(
                "document.querySelector('#terminal').termroomControlsInput === true",
                "real WebSocket/PTY input owner",
            )
            assert js("performance.getEntriesByType('resource').some("
                      "r => r.name.includes('/static/workspace_run_menu.js'))")
            assert js("performance.getEntriesByType('resource').some("
                      "r => r.name.includes('/static/terminal_files_drawer.js'))")
            assert js("performance.getEntriesByType('resource').some("
                      "r => r.name.includes('/static/terminal_copy.js'))")
            initial = js("""(() => {
              document.querySelector('#open-command-editor').click();
              const input=document.querySelector('#command-input');
              input.value='unsent draft 가나다 and English';
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.focus();input.setSelectionRange(7,12);
              const host=document.querySelector('#terminal');
              return {terminal:host, id:host.dataset.terminalId,
                open:!document.querySelector('#terminal-command-editor').hidden,
                rows:host.querySelector('.xterm-rows')?.childElementCount,
                draft:input.value,start:input.selectionStart,end:input.selectionEnd};
            })()""")
            assert initial["id"] == terminal["id"]
            assert initial["open"]
            assert initial["draft"] == "unsent draft 가나다 and English"
            # xterm resizes asynchronously when the command editor first
            # opens. Compare Files against the settled baseline, not the
            # intermediate CSS layout frame.
            stable_rows = initial["rows"]
            stable_since = time.monotonic()
            deadline = stable_since + 6
            while time.monotonic() < deadline:
                current_rows = js(
                    "document.querySelector('#terminal .xterm-rows')?.childElementCount"
                )
                if current_rows != stable_rows:
                    stable_rows = current_rows
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= 0.7:
                    break
                time.sleep(0.08)
            else:
                raise AssertionError("Command editor failed to settle xterm geometry")
            initial["rows"] = stable_rows

            trigger = js("""(() => {
              const element=document.querySelector('[data-terminal-files-trigger]');
              const rect=element.getBoundingClientRect();
              return {x:rect.x+rect.width/2,y:rect.y+rect.height/2,
                width:rect.width,height:rect.height,visible:element.getClientRects().length>0};
            })()""")
            assert trigger["visible"] and trigger["width"] >= 36, trigger
            # Real pointer click, not HTMLElement.click(): Chrome must focus
            # the opening navigation control as part of the actual gesture.
            for kind in ("mousePressed", "mouseReleased"):
                cdp("Input.dispatchMouseEvent", {
                    "type": kind, "x": trigger["x"], "y": trigger["y"],
                    "button": "left", "clickCount": 1,
                })
            until("document.querySelector('[data-terminal-files-drawer]').open",
                  "Files modal open")
            until(
                "document.querySelector('[data-terminal-files-list]')"
                ".textContent.includes('README-한글.md') && "
                "document.querySelector('[data-terminal-files-list]')"
                ".textContent.includes('alpha.txt') && "
                "document.querySelector('[data-terminal-files-list]')"
                ".getAttribute('aria-busy') !== 'true'",
                "authenticated file partial rendered",
            )
            listing = js("""(() => {
              const drawer=document.querySelector('[data-terminal-files-drawer]');
              const anchors=[...drawer.querySelectorAll('[data-terminal-files-list] a')];
              return {focus:document.activeElement?.hasAttribute('data-terminal-files-close'),
                links:anchors.map(a=>({
                  href:a.getAttribute('href'),target:a.target,rel:a.rel,
                  label:a.getAttribute('aria-label')
                }))};
            })()""")
            assert listing["focus"], "Files modal must focus its visible close control"
            file_links = [item for item in listing["links"]
                          if "/view/" in item["href"]]
            assert file_links and all(item["target"] == "_blank"
                                      and item["rel"] == "noopener"
                                      and item["label"] for item in file_links)
            # Search is read-only and keeps the Terminal attached and active.
            js("""(() => {
              const input=document.querySelector('#terminal-files-drawer-query');
              input.value='README';
              document.querySelector('[data-terminal-files-search]').requestSubmit();
            })()""")
            until(
                "document.querySelector('[data-terminal-files-list]')"
                ".textContent.includes('README-한글.md') && "
                "!document.querySelector('[data-terminal-files-list]')"
                ".textContent.includes('alpha.txt')",
                "Files live search filtering",
            )
            assert app.state.store.get_workspace(workspace["id"])["last_tab"] == "terminal"
            # Native Escape and close event must restore the opener focus.
            cdp("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Escape", "code": "Escape",
                "windowsVirtualKeyCode": 27,
            })
            cdp("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Escape", "code": "Escape",
                "windowsVirtualKeyCode": 27,
            })
            until("!document.querySelector('[data-terminal-files-drawer]').open",
                  "Files Escape dismissal")
            # Native dialog close dispatches its event asynchronously after
            # the open state flips; await the required focus restoration.
            until(
                "document.activeElement?.hasAttribute('data-terminal-files-trigger')",
                "Files modal restores its opening navigation focus",
            )
            after = js("""(() => {
              const host=document.querySelector('#terminal');
              const input=document.querySelector('#command-input');
              return {id:host.dataset.terminalId,
                rows:host.querySelector('.xterm-rows')?.childElementCount,
                draft:input.value,start:input.selectionStart,end:input.selectionEnd,
                open:!document.querySelector('#terminal-command-editor').hidden,
                selected:document.querySelector('.terminal-tabs a.active')?.dataset
                  .terminalActivityTab,
                focus:document.activeElement?.hasAttribute('data-terminal-files-trigger')};
            })()""")
            assert after["focus"], f"Files focus did not return: {after}"
            assert (after["id"], after["rows"], after["draft"],
                    after["start"], after["end"], after["open"], after["selected"]) == (
                initial["id"], initial["rows"], initial["draft"],
                initial["start"], initial["end"], initial["open"], initial["id"],
            ), after

            # The new menu module still owns exact state, progressive command
            # slots and unsaved edits; it cannot accidentally submit a command.
            menu_opened = js("""(() => {
              const menus=[...document.querySelectorAll('[data-workspace-run-menu]')];
              const menu=menus.find(m=>m.getClientRects().length>0);
              if(!menu)return false;
              menu.querySelector('summary').click();return menu.open;
            })()""")
            assert menu_opened, "quick-command menu was not discoverable"
            menus = js("""(() => {
              const menu=[...document.querySelectorAll('[data-workspace-run-menu]')]
                .find(m=>m.open);
              const cards=[...menu.querySelectorAll('[data-workspace-run-command-card]')];
              const card=cards.find(c=>!c.hidden);
              const command=card.querySelector('input[name="command"]');
              const name=card.querySelector('input[name="name"]');
              name.value='Local QA';command.value='printf safe-test';
              name.dispatchEvent(new Event('input',{bubbles:true}));
              command.dispatchEvent(new Event('input',{bubbles:true}));
              return {cards:cards.length,save:card.querySelector(
                '[data-workspace-run-command-save]').disabled,
                dirty:card.classList.contains('is-dirty')};
            })()""")
            assert menus["cards"] == 3 and menus["dirty"] and not menus["save"]
            js("""(() => {
              const menu=[...document.querySelectorAll('[data-workspace-run-menu]')]
                .find(m=>m.open);
              menu.querySelector('[data-workspace-run-command-cancel]').click();
              menu.querySelector('[data-workspace-run-close]').click();
            })()""")
            assert js("""(() => {
              const menus=[...document.querySelectorAll('[data-workspace-run-menu]')];
              const selected=menus.find(m=>m.getClientRects().length>0);
              const card=selected.querySelector('[data-workspace-run-command-card]');
              return !selected.open && !card.classList.contains('is-dirty')
                && card.querySelector('input[name="command"]').value === ''
                && document.activeElement === selected.querySelector('summary');
            })()"""), "Quick command cancel or focus restoration broke"
            assert not app.state.store.list_workspace_quick_commands(workspace["id"])
            assert app.state.terminals.control.client_count(terminal["id"]) > 0
    finally:
        core.close()
        app.state.terminals._run_tmux(
            "kill-session", "-t", workspace["tmux_session"], check=False,
        )

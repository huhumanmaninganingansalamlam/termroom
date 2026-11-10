# T41 · Terminal-first Workspace UX — isolated design review

Status: **A selected by the user for a local implementation trial** (2026-11-09 Asia/Seoul). These are still synthetic prototype fixtures, not production code. Existing dirty product changes must remain protected; no deployment was authorized.

## Open the review fixtures

Open the following local files from this directory in a browser. Data is synthetic and every action is an inert preview; there are no fetch, WebSocket, PTY, database or remote calls.

- **Existing CSS baseline:** [baseline.html](baseline.html). Manual DOM fixture based on production Workspace/Terminal templates, with the **unmodified production** app.css. It is *not* a fully rendered authenticated page.
- **Candidate A:** [Open A](index.html?layout=dock) — 56px desktop rail, full-height Terminal, temporary overlay drawer for Files, Recent, Run, Activity and Settings. Mobile keeps bottom tabs and uses a full-width sheet. Terminal stays mounted and its measured rectangle is unchanged on drawer open/close.
- **Candidate B:** [Open B](index.html?layout=strip) — top workspace-view strip and full-width mutually exclusive surfaces. Terminal remains mounted but is visually hidden when another surface opens. Visible labels cost desktop Terminal height.
- **Review controls:** [Open A with scenario controls](index.html?layout=dock&review=1), or append &review=1 for the tester's variant and scenario selector. Otherwise controls are hidden to avoid obscuring live-action chrome. You may also directly append &state=offline, refused, conflict, unknown, host-key, pairing, empty, file-run or remote-run. The optional &keyboard=1 simulates a keyboard-open compact viewport, not a physical phone.
- **Keyboard paths:** Alt+1 Terminal, Alt+2 Files, Alt+3 Recent, Alt+4 Run; Escape closes the active menu, composer or auxiliary panel. Candidate A's modal drawer traps Tab/Shift+Tab, marks the background inert and returns focus to the opener; B's nonmodal switched surface marks its hidden Terminal inert. Native buttons, labeled regions, visible focus styles and screen-reader status announcements are included.

Artifacts: index.html, prototype.css, prototype.js, baseline.html, measure.mjs, measurements.json and PNG files in screenshots/.

Visual comparison: [Current desktop](screenshots/baseline-desktop.png) · [A desktop](screenshots/A-desktop.png) · [B desktop](screenshots/B-desktop.png) · [A Files drawer](screenshots/A-files.png) · [B Files view](screenshots/B-files.png) · [A mobile](screenshots/A-mobile.png) · [B mobile](screenshots/B-mobile.png).

Run the isolated headless verifier from the product repo with:

    node docs/prototypes/terminal-first/measure.mjs

The runner creates a fresh temporary Chrome profile, opens only these file:// fixtures via CDP at exact viewport dimensions, checks interactive states and captures screenshots, then closes Chrome and deletes only its own temporary profile. It cannot contact the user's Termroom, SSH or Node sessions.

## Inventory of the real product — preserve, do not pretend to add

Inspected: Workspace/Terminal/Files/Recent/base Jinja templates; app.css, app.js, terminal.js; tests/test_workspace_run_menu_contract.py; tests/test_assets.py; docs/mobile-terminal.md; docs/architecture.md.

| Existing capability / boundary | Current implementation | Prototype representation and implementation guard |
| --- | --- | --- |
| Workspace identity and navigation | Home, name/path, Local/SSH/Node/Server and Remote Run modes; desktop 248px sidebar, mobile 56px header / 60px bottom nav; Activity and Settings | A puts identity in Terminal bar and Activity/Settings at rail bottom; B retains full top bar; mobile action access survives |
| Multiple terminals | tmux session, named tabs, unread dots, create/rename/delete, focus/control release, current identity | Three realistic synthetic tabs plus new/rename/close preview; the underlying tmux lifecycle must remain unchanged |
| Output, selection and access | xterm 6, Unicode11, 5000-line scrollback, selection/copy/search, split-pane targeting, alternate screen, accessible visible rows and opt-in screen-reader mode | Static text only, visible Output/management controls, accessible preview semantics. Never claim to emulate xterm or tmux |
| Input ownership | T39 protects human focus/draft/scroll against automation, refuses competing input, distinguishes unknown delivery | Refusal/unknown cases use honest action copy. No Agent dashboard or inferred Agent state |
| Helper keys and composer | Esc/Tab/Ctrl/arrows/C-c, More keys, xterm onData and bracketed paste, IME, optional command editor and explicit submit | Toggles and feedback simulated; no actual PTY transmission. Production must keep xterm as sole input path |
| Software keyboard | visualViewport threshold, temporary hiding of header/nav, helper row maintained, safe-area and landscape modes | 390×500 CSS state only; not a real iPhone/Safari keyboard or physical-device test |
| Files | Breadcrumbs, upload/create, live search, noise toggle, multi-select ZIP, rename/delete/download, browser edit with CAS, Vim open | Browser/search/action entry points shown. Advanced CRUD, editor and bulk operations remain real existing routes, not reimplemented here |
| Recent | Files and live terminals, refresh/empty/error | Both show entries and empty-state navigation |
| Named commands | Up to three stored commands with revision guards, add/edit/delete/launch and progressive slots | Two saved examples and an empty-state preview; no launch |
| File Run | Start/status/stop/force-stop/connection recovery, return to file and Terminal | Run state, stop affordances and Files entry shown; no process touched |
| Remote Run | Source/target, stop/force-stop, retention, status unknown, result ZIP/collect/retry | Run setup + completed result card, ZIP/collect entry points; no network action |
| Activity/Settings | Notification claims, usage, language/theme, workspace rename/remove, PWA install | Explicit auxiliary surfaces; real account/device actions are not imitated |
| Trust/conflict paths | SSH host-key change, Node pairing, offline/unknown result, CAS file conflict | Named scenarios with truthful next-step text, including in-panel file conflict. Never fake a trust grant |
| Accessibility | Semantic buttons/details, aria-expanded, Escape/focus restoration, readable terminal snapshots | A: labeled modal dialog, heading focus, Tab/Shift+Tab containment, Escape/close/scrim dismissal, opener focus return and inert background. B: labeled nonmodal region with hidden Terminal inert. Automated DOM checks are not a screen-reader audit |

## Web benchmarking — official primary documentation, retrieved 2026-11-09

1. [VS Code: Terminal Basics](https://code.visualstudio.com/docs/terminal/basics) — terminal tab lists and split groups work within flexible workbench placement. Keep terminal sessions directly accessible, without adopting a full IDE shell.
2. [VS Code: Custom Layout](https://code.visualstudio.com/docs/configure/custom-layout) — sidebars/panels can be toggled and restored. Any temporary Files/Run surface needs a reliable Escape/close path and must not steal the main terminal by default.
3. [JetBrains IntelliJ IDEA: Terminal](https://www.jetbrains.com/help/idea/terminal-emulator.html) — terminals have tabs, session switching and flexible placement. Termroom already has the essential session model; a decorative session dashboard adds no value.
4. [GitHub Codespaces: Developing in a codespace](https://docs.github.com/en/codespaces/developing-in-a-codespace) — browser and command-shell work are intentional entry paths. This is an integration lesson, not a requirement to clone VS Code.
5. [W3C WAI-ARIA APG: Modal Dialog Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/) — a modal panel keeps focus inside, closes with Escape and returns focus on dismissal; background content must be inert. Applied to A's overlay, not B's nonmodal surface.
6. [W3C WCAG 2.2: Contrast (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html) — normal text requires a 4.5:1 contrast ratio for level AA. Used to check the prototype's subtle label color; this is not a full WCAG audit.

## Layout evidence — same sample output, same specimen font

An isolated Chrome session used exact 1440×900, 1024×768, 390×844 and 390×500 viewport overrides. All three fixtures used a 14px / 18px monospace specimen. Below: **Terminal host width × height in CSS pixels; approximate rows × columns in parentheses**.

| Viewport | Existing CSS fixture | A — drawer | B — switched surface |
| --- | --- | --- | --- |
| 1440×900 desktop | 1192×795 (44×138) | **1384×794 (44×161)** | 1440×694 (38×167) |
| 1024×768 narrow | 776×663 (36×89) | **968×662 (36×111)** | 1024×562 (31×118) |
| 390×844 mobile | 390×614 (34×43) | 390×632 (35×43) | 390×642 (35×43) |
| 390×500 keyboard simulation | 390×386 (21×43) | 390×404 (22×43) | 390×404 (22×43) |

No page-level horizontal overflow in those four states. A preserves desktop terminal height within 1px while gaining 192px width; B loses 101px height in exchange for 248px width. On mobile both are close to baseline. The **first** A draft cost 55px terminal height due to a full-width top bar; it was rejected and revised before final comparison.

These dimensions come from current CSS with manually mirrored baseline DOM and synthetic candidate DOM, not from an authenticated app, xterm FitAddon or live tmux PTY. Estimated cells are font-metric proxies, not actual shared PTY geometry. No real mobile keyboard, VoiceOver, TalkBack, IME, cold/warm network navigation or throughput was measured. Do not use this table to certify production behavior or invent latency budgets.

## Interaction verification and decision matrix

The isolated script checks at desktop/narrow/mobile: Files panel opens with focus on its heading and appropriate dialog/region semantics; the hidden Terminal is inert; the background cannot take focus while A is open; A's Tab/Shift+Tab remains in its dialog; dismiss restores Terminal; draft, selected tab and Terminal rectangle survive the surface transition; alternative Terminal output switches and returns; composer focuses and closes by Escape without losing draft; Escape closes Files. It also renders offline, refusal, conflict plus in-panel error, unknown, SSH host-key change, Node pairing, empty Recent/Run, File Run stop controls and Remote Run result controls, without real effects. JavaScript syntax checks and browser probe must report no failures.

The prototype's `--subtle` text token was raised from `#8592a1` to `#98a6b6`: calculated contrast on the default `#242b35` chrome is ~4.50:1 → ~5.75:1 (and ~3.68:1 → ~4.70:1 on the sampled `#303946` control surface). This is a targeted token check, not an exhaustive rendered-state contrast or assistive-technology audit.

| Criterion | Current | A — drawer | B — view switch |
| --- | --- | --- | --- |
| Default Terminal | Full-height, 248px left sidebar | **Full-height, +192px desktop width** | Full width, but **−101px desktop height** |
| Files/Run path | Separate routes | One nav action, close/Escape/scrim; output remains behind drawer | One top-tab action; Terminal visually hidden until return |
| Discoverability | Desktop text nav, mobile bottom nav | Compact labeled-icon rail and labeled mobile tabs, drawer heading | Top text tabs, but mobile thumb reach worse |
| Multiple tmux tabs | Already implemented | Preserved | Preserved |
| Focus/identity | Existing semantic close paths | Prototype focus trap, inert background, opener return and stable geometry passed; real xterm/IME/tmux integration still risky | Prototype hidden-Terminal inert/return and geometry passed; real show/hide/tmux resize still risky |
| Mobile keyboard | Real viewport contract | Simulated 404px host / helper retained | Same simulated host |
| Regression risk | None | **Medium**: overlay exclusivity, focus and real tmux geometry need testing | **High**: lost Terminal visibility and vertical rows |

Opening Files uses one primary action in each design; returning requires one navigation/close action. These are discrete action counts, **not measured human task times**. Static fixtures cannot validate real cold/warm navigation, output throughput or backend permissions. Implementation must define budgets from isolated server-driven baselines.

**Current recommendation: A for further user review, not yet for production.** B's lost desktop rows are a meaningful cost for Vim/TUI users. Retaining the current UI remains an acceptable outcome if A's actual implementation requires too much risk.

## Selection gate and next implementation task

Decision **recorded 2026-11-09**: user chose A (“a로진행해봐일단”). Implement in T-20261107101221Z, staging real working surfaces rather than copying a synthetic drawer or modifying live sessions.

If A is chosen, implement only within the separate T-20261107101221Z after narrowing its exact file scope: keep xterm mounted; test real tmux pane rows/cols and input-control ownership across drawer transitions; verify draft/scroll/tab and keyboard focus/IME; preserve all existing Files/Run/Recent/Activity/Settings/SSH/Node behaviors, output search and screen reader; compare against live isolated fixture; run actual mobile Safari and Android keyboard checks with the user's permission. If A fails those gates, retain current and ship a smaller improvement instead.

Synthetic screenshots: baseline-desktop/mobile/keyboard, A-desktop/mobile/keyboard/files, B-desktop/mobile/keyboard/files and selected error/result states, all in screenshots/. These are design evidence, not product correctness proof.

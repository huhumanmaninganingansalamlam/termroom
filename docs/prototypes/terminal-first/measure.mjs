// T41 synthetic fixtures only; isolated Chrome CDP, no live Termroom or browser profile.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const directory = dirname(fileURLToPath(import.meta.url));
const shots = join(directory, "screenshots");
const profile = mkdtempSync(join(tmpdir(), "termroom-t41-chrome-"));
const chrome = spawn("/usr/bin/google-chrome-stable", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  "--no-first-run", "--no-default-browser-check", "--disable-extensions",
  "--disable-background-networking", "--disable-sync", "--remote-allow-origins=*",
  "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"
], { stdio: "ignore" });

let socket;
let serial = 0;
const waiting = new Map();
const problems = [];

function send(method, params = {}) {
  return new Promise((ok, fail) => {
    const id = ++serial;
    const timeout = setTimeout(() => { waiting.delete(id); fail(new Error("CDP timeout: " + method)); }, 15000);
    waiting.set(id, { ok, fail, timeout });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function connect() {
  const portFile = join(profile, "DevToolsActivePort");
  for (let tries = 0; tries < 100 && !existsSync(portFile); tries++) await delay(100);
  if (!existsSync(portFile)) throw new Error("isolated Chrome did not open debug port");
  const port = Number(readFileSync(portFile, "utf8").split("\n")[0]);
  const endpoint = await fetch("http://127.0.0.1:" + port + "/json");
  const page = (await endpoint.json()).find((target) => target.type === "page");
  if (!page) throw new Error("Chrome opened no empty page");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((ok, fail) => {
    const timeout = setTimeout(() => fail(new Error("CDP WebSocket timeout")), 10000);
    socket.addEventListener("open", () => { clearTimeout(timeout); ok(); }, { once: true });
    socket.addEventListener("error", fail, { once: true });
  });
  socket.addEventListener("message", (message) => {
    const packet = JSON.parse(String(message.data));
    if (packet.method === "Runtime.exceptionThrown") problems.push(packet.params?.exceptionDetails?.text || "JS exception");
    const pending = waiting.get(packet.id);
    if (!pending) return;
    waiting.delete(packet.id);
    clearTimeout(pending.timeout);
    if (packet.error) pending.fail(new Error(packet.error.message));
    else pending.ok(packet.result);
  });
  await send("Page.enable");
  await send("Runtime.enable");
}

async function js(fn) {
  const expression = typeof fn === "function" ? "(" + fn.toString() + ")()" : fn;
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error("JS evaluate exception: " + result.exceptionDetails.text);
  return result.result?.value;
}

async function navigate(file, width, height, query = "") {
  await send("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile: width < 600, screenWidth: width, screenHeight: height
  });
  await send("Page.navigate", { url: pathToFileURL(resolve(directory, file)).href + query });
  for (let tries = 0; tries < 60; tries++) {
    if (await js(() => document.readyState === "complete" && !!document.querySelector("[data-terminal-viewport],#terminal"))) {
      await delay(160);
      return;
    }
    await delay(100);
  }
  throw new Error("fixture did not load: " + file);
}

function measure() {
  const node = document.querySelector("[data-terminal-viewport],#terminal");
  const pre = document.querySelector(".terminal-content:not([hidden]) pre,.terminal-reference");
  const box = (n) => {
    if (!n) return null;
    const r = n.getBoundingClientRect();
    return { x:+r.x.toFixed(1),y:+r.y.toFixed(1),width:+r.width.toFixed(1),height:+r.height.toFixed(1) };
  };
  const font = pre ? getComputedStyle(pre) : null;
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (context && font) context.font = font.font;
  const glyph = context ? context.measureText("M").width : 0;
  const line = font ? parseFloat(font.lineHeight) : 0;
  const terminal = box(node);
  return {
    viewport:{width:innerWidth,height:innerHeight}, terminal,
    header:box(document.querySelector(".app-header,.workspace-header")),
    nav:box(document.querySelector(".view-nav,.bottom-nav")),
    chrome:box(document.querySelector(".terminal-toolbar,.terminal-chrome")),
    helper:box(document.querySelector(".terminal-bottom,.terminal-composer")),
    panel:box(document.querySelector(".aux-surface:not([hidden])")),
    font:{size:font?.fontSize,lineHeight:line,glyphWidth:+glyph.toFixed(2)},
    estimatedRows:terminal&&line?Math.floor(terminal.height/line):null,
    estimatedCols:terminal&&glyph?Math.floor((terminal.width-25)/glyph):null,
    pageOverflowX:document.documentElement.scrollWidth>innerWidth,
    visible:!!node && getComputedStyle(node).visibility!=="hidden",
    tabCount:document.querySelectorAll("[data-terminal],.terminal-tabs a").length,
    activeTab:document.querySelector('[data-terminal][aria-pressed="true"]')?.dataset.terminal||"shell",
    activeView:document.querySelector('.view-nav [aria-current="page"]')?.dataset.view||"terminal"
  };
}

function testNavigation() {
  const before = document.querySelector("[data-terminal-viewport]").getBoundingClientRect();
  const name = document.querySelector('[data-terminal][aria-pressed="true"]').dataset.terminal;
  const draft = document.querySelector("#command-draft");
  draft.value = "pytest tests/test_invoice.py -q";
  draft.dispatchEvent(new Event("input",{bubbles:true}));
  document.querySelector('.view-nav [data-view="files"]').click();
  const panel = document.querySelector("#aux-surface");
  const panelOpen = !panel.hidden;
  const focusInPanel = document.activeElement?.id === "aux-title";
  const dock = document.documentElement.dataset.layout === "dock";
  const semantics = dock
    ? panel.getAttribute("role") === "dialog" && panel.getAttribute("aria-modal") === "true" && document.querySelector(".view-nav").inert
    : panel.getAttribute("role") === "region" && !document.querySelector(".view-nav").inert;
  const hiddenTerminalInert = document.querySelector(".terminal-surface").inert && document.querySelector(".terminal-surface").getAttribute("aria-hidden") === "true";
  document.querySelector('[data-terminal="api"]').focus();
  const focusNotBehind = !document.querySelector(".terminal-surface").contains(document.activeElement);
  let tabContained = true;
  if (dock) {
    const last = [...panel.querySelectorAll("button, input, select, textarea")].filter(el => el.getClientRects().length && !el.closest("[hidden]")).at(-1);
    last?.focus();
    document.dispatchEvent(new KeyboardEvent("keydown",{key:"Tab",bubbles:true,cancelable:true}));
    tabContained = panel.contains(document.activeElement) && document.activeElement?.id === "close-aux";
  }
  const after = document.querySelector("[data-terminal-viewport]").getBoundingClientRect();
  document.querySelector("#close-aux").click();
  document.querySelector('[data-terminal="api"]').click();
  const switchedTerminal = document.querySelector('[data-terminal="api"]').getAttribute("aria-pressed")==="true" && !document.querySelector('[data-output="api"]').hidden;
  document.querySelector('[data-terminal="shell"]').click();
  document.querySelector('[data-action="compose"]').click();
  const composerFocused = document.activeElement?.id==="command-draft";
  document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
  const composerClosed = document.querySelector("#compose-panel").hidden && draft.value==="pytest tests/test_invoice.py -q";
  document.querySelector('.view-nav [data-view="files"]').click();
  document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
  const escapeClosesPanel = document.querySelector("#aux-surface").hidden;
  return {
    panelOpen, focusInPanel, semantics,hiddenTerminalInert,focusNotBehind,tabContained,switchedTerminal,composerFocused,composerClosed,escapeClosesPanel,
    retained:draft.value === "pytest tests/test_invoice.py -q",
    terminalSameSize:Math.abs(before.width-after.width)<1 && Math.abs(before.height-after.height)<1,
    terminalSameIdentity:document.querySelector('[data-terminal][aria-pressed="true"]').dataset.terminal===name,
    returnedToTerminal:document.querySelector("#aux-surface").hidden
  };
}

function checkScenario() {
  return {
    notice:document.querySelector("#notice").textContent,
    panel:document.querySelector("#aux-title").textContent,
    panelShown:!document.querySelector("#aux-surface").hidden,
    state:document.querySelector("#app").dataset.scenario
  };
}

async function capture(name) {
  const screenshot = await send("Page.captureScreenshot", {
    format: "png", fromSurface: true, captureBeyondViewport: false
  });
  writeFileSync(join(shots,name+".png"),Buffer.from(screenshot.data,"base64"));
}

async function run() {
  mkdirSync(shots,{recursive:true});
  await connect();
  const variants = [["baseline","baseline.html",""],["A","index.html","?layout=dock"],["B","index.html","?layout=strip"]];
  const sizes = [["desktop",1440,900],["narrow",1024,768],["mobile",390,844]];
  const results = [];
  for (const [name,file,query] of variants) {
    for (const [size,width,height] of sizes) {
      await navigate(file,width,height,query);
      const metric = await js(measure);
      results.push({variant:name,size,...metric});
      if (size==="desktop"||size==="mobile") await capture(name+"-"+size);
      if (name==="baseline") continue;
      const action = await js(testNavigation);
      results[results.length-1].interaction = action;
      if (size === "desktop") {
        await js(() => document.querySelector('.view-nav [data-view="files"]').click());
        await capture(name+"-files");
      }
      if (!Object.values(action).every(Boolean)) problems.push(name+"-"+size+" navigation failed: "+JSON.stringify(action));
    }
    if (name==="baseline") {
      await navigate(file,390,500,"?keyboard=1");
      results.push({variant:name,size:"keyboard-390x500",...await js(measure)});
      await capture(name+"-keyboard");
    }
    if (name!=="baseline") {
      await navigate(file,390,500,query+"&keyboard=1");
      results.push({variant:name,size:"keyboard-390x500",...await js(measure)});
      await capture(name+"-keyboard");
      for (const state of ["offline","refused","conflict","unknown","host-key","pairing","empty","file-run","remote-run"]) {
        await navigate(file,390,844,query+"&state="+state);
        const result = await js(checkScenario);
        if (result.state!==state) problems.push(name+" scenario didn't select "+state);
        if (["offline","refused","conflict","unknown","host-key","pairing"].includes(state)&&!result.notice) problems.push(name+" missing error "+state);
        if (["empty","file-run","remote-run"].includes(state)&&!result.panelShown) problems.push(name+" missing panel "+state);
        if (state==="conflict" && !await js(() => !document.querySelector("#panel-alert").hidden)) problems.push(name+" missing contextual conflict notice");
        if (state==="remote-run" && !await js(() => !document.querySelector("#remote-result").hidden)) problems.push(name+" missing result review");
        if (state==="file-run" && !await js(() => !document.querySelector("#file-run-actions").hidden)) problems.push(name+" missing stop control");
        if (name==="A" && ["conflict","remote-run","offline"].includes(state)) await capture(name+"-state-"+state);
        if (name==="B" && state==="file-run") await capture(name+"-state-"+state);
        results.push({variant:name,size:"state-"+state,scenario:result});
      }
    }
  }
  const output = {
    fixture:"Synthetic HTML/CSS. Baseline mirrors current DOM with production app.css unmodified.",
    browser:"Isolated Chrome headless CDP; exact device metric overrides",
    measuredAt:new Date().toISOString(),
    rowsCols:"Estimates only, based on specimen monospace font/line-height; NOT xterm FitAddon/tmux grid",
    results,problems
  };
  writeFileSync(join(directory,"measurements.json"),JSON.stringify(output,null,2)+"\n");
  console.log(JSON.stringify({
    table:results.filter((r)=>["desktop","narrow","mobile","keyboard-390x500"].includes(r.size))
      .map((r)=>({variant:r.variant,size:r.size,viewport:r.viewport,terminal:r.terminal,
        estimatedRows:r.estimatedRows,estimatedCols:r.estimatedCols,
        overflow:r.pageOverflowX,interaction:r.interaction})),problems
  }));
  if (problems.length) process.exitCode=1;
}

try { await run(); }
catch (error) { console.error(error); process.exitCode=1; }
finally {
  socket?.close();
  chrome.kill("SIGTERM");
  await delay(300);
  rmSync(profile,{recursive:true,force:true}); // exact temp profile created above only
}

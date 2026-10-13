// A minimal headless-Chrome driver over the DevTools protocol, for the /app
// browser tests (Node 22+ has WebSocket built in, so no packages).
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function launch(port, extraArgs = []) {
  const prof = mkdtempSync(join(tmpdir(), 'sparrow_cdp_prof_'));
  const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`,
    `--user-data-dir=${prof}`, '--no-first-run', '--no-default-browser-check',
    ...extraArgs, 'about:blank'], { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 50 && !targets; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); }
    catch (e) { await sleep(200); }
  }
  const page = targets.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      const p = pending.get(d.id); pending.delete(d.id);
      d.error ? p.rej(new Error(d.error.message)) : p.res(d.result);
    }
  };
  await new Promise((r) => { ws.onopen = r; });
  const send = (method, params = {}) => {
    const id = ++seq;
    ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => pending.set(id, { res, rej }));
  };
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true,
                                               returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 600));
    return r.result.value;
  };
  const waitFor = async (expr, ms) => {
    for (let t = 0; t < ms; t += 200) {
      try { if (await evalJs(expr)) return true; } catch (e) { /* navigating */ }
      await sleep(200);
    }
    return false;
  };
  const open = async (url) => {
    await send('Page.navigate', { url });
    if (!await waitFor('typeof syncHeld === "function" && document.readyState === "complete"', 20000))
      throw new Error('app did not load');
  };
  const close = async () => {
    try { ws.close(); } catch (e) {}
    chrome.kill();
    await sleep(500);
    try { rmSync(prof, { recursive: true, force: true }); } catch (e) {}
  };
  await send('Page.enable'); await send('Runtime.enable');
  return { send, evalJs, waitFor, open, close };
}

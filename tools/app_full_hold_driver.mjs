// Drives the REAL /app page in headless Chrome for test_app_full_hold.py.
//   node tools/app_full_hold_driver.mjs <base> <node_id> <token> <ids.json> <jpeg data url file>
// Prints one JSON line: what the phone holds after each step.
import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [base, nodeId, token, idsFile, jpegFile] = process.argv.slice(2);
const ids = JSON.parse(readFileSync(idsFile, 'utf8'));
const jpeg = readFileSync(jpegFile, 'utf8').trim();
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9337;
const prof = mkdtempSync(join(tmpdir(), 'sparrow_app_prof_'));
const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${prof}`, '--no-first-run', '--no-default-browser-check', 'about:blank'],
  { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ws, seq = 0;
const pending = new Map();
function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}
async function evalJs(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true,
                                             returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 600));
  return r.result.value;
}
async function open(url) {
  await send('Page.navigate', { url });
  for (let i = 0; i < 100; i++) {
    await sleep(100);
    try { if (await evalJs('typeof syncHeld === "function" && document.readyState === "complete"')) return; }
    catch (e) { /* navigating */ }
  }
  throw new Error('app did not load');
}

const out = {};
try {
  let targets;
  for (let i = 0; i < 50; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json(); break; }
    catch (e) { await sleep(200); }
  }
  const page = targets.find((t) => t.type === 'page');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      const p = pending.get(d.id); pending.delete(d.id);
      d.error ? p.rej(new Error(d.error.message)) : p.res(d.result);
    }
  };
  await new Promise((r) => { ws.onopen = r; });
  await send('Page.enable'); await send('Runtime.enable');

  // The key arrives the way a volunteer's does: a #k= fragment.
  await open(`${base}/app#k=${nodeId}.${token}`);
  out.node = await evalJs('node && node.id');

  // Hold a clear crop for each sighting, as submit() does. `old` ones are
  // backdated past the grace period, as if captured 20 minutes ago.
  const ago = 20 * 60 * 1000;
  await evalJs(`(async () => {
    const ids = ${JSON.stringify(ids)}, data = ${JSON.stringify(jpeg)};
    for (const [name, id] of Object.entries(ids)) holdFull(id, data);
    await new Promise((r) => setTimeout(r, 300));
    await fullTx('readwrite', (s) => {
      for (const [name, id] of Object.entries(ids)) if (name !== 'fresh') {
        const g = s.get(id); g.onsuccess = () => s.put({ ...g.result, at: Date.now() - ${ago} });
      }
    });
  })()`);
  out.held_before = await evalJs('fullRows().then((r) => r.map((x) => x.id).sort())');

  await evalJs('syncHeld()');
  out.after_sync = await evalJs('fullRows().then((r) => r.map((x) => [x.id, x.keep]))');
  out.full_sent = await evalJs('fullSent');

  // A reload must not forget: the old hold lived in memory only.
  await open(`${base}/app`);
  out.after_reload = await evalJs('fullRows().then((r) => r.map((x) => [x.id, x.keep]))');
} catch (e) {
  out.error = String(e && e.stack || e);
} finally {
  console.log(JSON.stringify(out));
  try { ws && ws.close(); } catch (e) {}
  chrome.kill();
  await sleep(500);
  try { rmSync(prof, { recursive: true, force: true }); } catch (e) {}
  process.exit(0);
}

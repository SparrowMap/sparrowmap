// Drives the REAL /app page in headless Chrome for test_app_full_hold.py.
//   node tools/app_full_hold_driver.mjs <base> <node_id> <token> <ids.json> <jpeg data url file>
// Prints one JSON line: what the phone holds after each step.
import { readFileSync } from 'node:fs';
import { launch } from './cdp.mjs';

const [base, nodeId, token, idsFile, jpegFile] = process.argv.slice(2);
const ids = JSON.parse(readFileSync(idsFile, 'utf8'));
const jpeg = readFileSync(jpegFile, 'utf8').trim();

const out = {};
const b = await launch(9337);
try {
  // The key arrives the way a volunteer's does: a #k= fragment.
  await b.open(`${base}/app#k=${nodeId}.${token}`);
  out.node = await b.evalJs('node && node.id');

  // Hold a clear crop for each sighting, as submit() does. All but `fresh` are
  // backdated past the grace period, as if captured 20 minutes ago.
  const ago = 20 * 60 * 1000;
  await b.evalJs(`(async () => {
    const ids = ${JSON.stringify(ids)}, data = ${JSON.stringify(jpeg)};
    for (const [name, id] of Object.entries(ids)) holdFull(id, data);
    await new Promise((r) => setTimeout(r, 300));
    await fullTx('readwrite', (s) => {
      for (const [name, id] of Object.entries(ids)) if (name !== 'fresh') {
        const g = s.get(id); g.onsuccess = () => s.put({ ...g.result, at: Date.now() - ${ago} });
      }
    });
  })()`);
  out.held_before = await b.evalJs('fullRows().then((r) => r.map((x) => x.id).sort())');

  await b.evalJs('syncHeld()');
  out.after_sync = await b.evalJs('fullRows().then((r) => r.map((x) => [x.id, x.keep]))');
  out.full_sent = await b.evalJs('fullSent');

  // A reload must not forget: the old hold lived in memory only.
  await b.open(`${base}/app`);
  out.after_reload = await b.evalJs('fullRows().then((r) => r.map((x) => [x.id, x.keep]))');
} catch (e) {
  out.error = String(e && e.stack || e);
} finally {
  console.log(JSON.stringify(out));
  await b.close();
  process.exit(0);
}

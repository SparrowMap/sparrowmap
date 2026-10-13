// Drives the REAL /app page in headless Chrome for test_app_selfupdate.py.
//   node tools/app_selfupdate_driver.mjs <base> <node_id> <token>
// The hub reports a newer app version than the page loaded. Prints one JSON
// line: how many times the page loaded and whether it is watching afterwards.
import { launch, sleep } from './cdp.mjs';

const [base, nodeId, token] = process.argv.slice(2);
const out = {};
// A fake camera, granted without a prompt: what a volunteer's phone has after
// the first time they allowed it.
const b = await launch(9338, ['--use-fake-ui-for-media-stream',
                              '--use-fake-device-for-media-stream']);
try {
  // Count every document load in this tab.
  await b.send('Page.addScriptToEvaluateOnNewDocument', { source:
    "try { sessionStorage.setItem('t.loads', String(+(sessionStorage.getItem('t.loads') || 0) + 1)); } catch (e) {}" });
  await b.open(`${base}/app#k=${nodeId}.${token}`);
  out.app_v_loaded = await b.evalJs('APP_V');

  // The volunteer taps Start, as they do once.
  await b.evalJs('start()');
  out.watching_before = await b.waitFor('running === true', 60000);

  // start() beats at once; the hub says there is a newer version. The page
  // must reload, and come back watching with no tap.
  out.reloaded = await b.waitFor("+sessionStorage.getItem('t.loads') >= 2", 60000);
  out.watching_after = await b.waitFor('running === true', 90000);

  // And not loop: past the next beat (30 s) it must still be the same load.
  await sleep(40000);
  out.loads = await b.evalJs("+sessionStorage.getItem('t.loads')");
  out.watching_end = await b.evalJs('running === true');
  out.beats_ok = await b.evalJs("document.body.innerText.includes('LIVE on the map')");
} catch (e) {
  out.error = String(e && e.stack || e);
} finally {
  console.log(JSON.stringify(out));
  await b.close();
  process.exit(0);
}

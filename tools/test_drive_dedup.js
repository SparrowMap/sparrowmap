/* Does drive.html send every vehicle once, and no vehicle twice?
 *
 * This does NOT reimplement the rule - a test that restates the logic it is
 * checking passes for both the right and the wrong version. It pulls the
 * decisive blocks out of drive.html as TEXT (the queue join, the overflow
 * trim, the enrolment gate, the sender, the dashboard filter) and runs those,
 * so an inverted comparison or a dropped clause in the shipped file fails here.
 *
 * 2026-10-20: the 5 s "largest vehicle wins the window" rule became a queue,
 * after his drive past a patrol car sent the same white car three times and
 * his own dashboard once.
 *
 *   node tools/test_drive_dedup.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'public', 'drive.html');
const src = fs.readFileSync(SRC, 'utf8');

function need(re, what) {
  const m = src.match(re);
  if (!m) { console.error('FAIL: could not find ' + what + ' in drive.html'); process.exit(1); }
  return m;
}

const num = (re, what) => parseFloat(need(re, what)[1]);
const K = {
  RESEND_WIDTH: num(/var RESEND_WIDTH=([\d.]+);/, 'RESEND_WIDTH'),
  MIN_FRAMES: num(/MIN_FRAMES=(\d+)/, 'MIN_FRAMES'),
  MIN_SEND_PX: num(/var MIN_SEND_PX=(\d+);/, 'MIN_SEND_PX'),
  MIN_STABLE_MS: num(/var MIN_STABLE_MS=(\d+);/, 'MIN_STABLE_MS'),
  SEND_GAP_MS: num(/SEND_GAP_MS=(\d+)/, 'SEND_GAP_MS'),
  HOUR_BUDGET: num(/HOUR_BUDGET=(\d+)/, 'HOUR_BUDGET'),
  QUEUE_MAX: num(/QUEUE_MAX=(\d+)/, 'QUEUE_MAX'),
};
const join = need(/(var trW = tr\.bestBox[\s\S]*?sendQ\.push\(tr\);\s*\})/, 'the queue join')[1];
const trim = need(/(if\(sendQ\.length>QUEUE_MAX\)\{[\s\S]*?\}\);\s*\})/, 'the overflow trim')[1];
const enrol = need(/if\(!driveNode && !driveNodePending && me && ([^)]+)\)\{/, 'the enrolment gate')[1];
const send = need(/(while\(sendTimes\.length[\s\S]*?sendTimes\.push\(now\);\s*\})/, 'the sender')[1];
const dstable = need(/(function dstable\(tr,now\)\{[\s\S]*?\n  \})/, 'dstable')[1];
const dash = need(/if\((h\.box\[3\] > v\.videoHeight[^\n]*?)\) continue;/, 'the dashboard filter')[1];
const roi = need(/if\((h\.box\[1\] > v\.videoHeight\*[\d.]+)\) continue;/, 'the bottom-third ROI')[1];

console.log('from drive.html:', JSON.stringify(K));
console.log('  enrolment gate : ' + enrol);
console.log('  dashboard drop : ' + dash);
console.log();

/* The page's state for one simulated drive. `with` so the extracted blocks
 * read and assign their own variable names exactly as they do in dloop. */
function drive() {
  const ctx = Object.assign({}, K, {
    sendQ: [], sendTimes: [], lastSend: 0, me: [40, -80], driveNode: { id: 'n', token: 't' },
    driveNodePending: false, sent: [], tr: null, v: { videoWidth: 1280, videoHeight: 720 }, now: 0,
    dmotion: () => null,
    submitCrop: (b) => ctx.sent.push({ id: b.tr.id, w: b.box[2] - b.box[0], at: ctx.now }),
  });
  ctx.dstable = new Function('ctx', 'with(ctx){ return ' + dstable + ' }')(ctx);
  const run = (code) => new Function('ctx', 'with(ctx){' + code + '}');
  const J = run(join), T = run(trim), S = run(send);
  return {
    ctx,
    see(tr) { ctx.tr = tr; tr.seen++; tr.last = ctx.now; J(ctx); },   // one detection of tr this frame
    endFrame() { T(ctx); S(ctx); },
  };
}
function track(id, w, t0) {
  return { id: id, seen: 0, first: t0 || 0, best: 'crop', bestBox: [0, 0, w, w / 2], bestArea: w * w / 2 };
}
function grow(tr, w) { tr.bestBox = [0, 0, w, w / 2]; tr.bestArea = w * w / 2; }

let fails = 0;
function check(name, got, want) {
  const ok = got === want;
  console.log('  [' + (ok ? 'ok  ' : 'FAIL') + '] ' + name.padEnd(60) + ' got ' + got + ', want ' + want);
  if (!ok) fails++;
}
const FRAME = 333;                                       // the phone runs YOLO at ~3 fps

// ---- 1. the car he was following: same size for 2.5 minutes ----------------
{ const d = drive(), lead = track('lead', 300);
  for (let f = 0; f < 450; f++) { d.ctx.now = 1000 + f * FRAME; d.see(lead); d.endFrame(); }
  check('a car followed for 2.5 min is uploaded once', d.ctx.sent.length, 1); }

// ---- 2. his 10-20 miss: a big car and the smaller one beside it -------------
{ const d = drive(), big = track('white car', 400), small = track('cruiser', 150);
  for (let f = 0; f < 30; f++) { d.ctx.now = 1000 + f * FRAME; d.see(big); d.see(small); d.endFrame(); }
  const ids = d.ctx.sent.map(s => s.id);
  check('the bigger car is sent', ids.includes('white car'), true);
  check('the smaller car beside it is sent too', ids.includes('cruiser'), true);
  check('  each exactly once', ids.length, 2);
  const gap = d.ctx.sent.length === 2 ? d.ctx.sent[1].at - d.ctx.sent[0].at : -1;
  check('  the second within 2 s of the first', gap > 0 && gap <= 2000, true); }

// ---- 3. five vehicles at once: all go, spaced, none dropped -----------------
{ const d = drive(), cars = [1, 2, 3, 4, 5].map(i => track('c' + i, 130 + i * 20));
  for (let f = 0; f < 6; f++) { d.ctx.now = 1000 + f * FRAME; cars.forEach(c => d.see(c)); d.endFrame(); }
  // they leave the frame now; the queue still holds their crops
  for (let f = 6; f < 40; f++) { d.ctx.now = 1000 + f * FRAME; d.endFrame(); }
  check('five vehicles seen together are all sent', d.ctx.sent.length, 5);
  const gaps = d.ctx.sent.slice(1).map((s, i) => s.at - d.ctx.sent[i].at);
  check('  never two inside SEND_GAP_MS', gaps.every(g => g >= K.SEND_GAP_MS), true); }

// ---- 4. a vehicle we close on gets a second look, not one per frame ---------
{ const d = drive(), c = track('closing', 130);
  for (let f = 0; f < 60; f++) { d.ctx.now = 1000 + f * FRAME; grow(c, 130 + f * 6); d.see(c); d.endFrame(); }
  const n = d.ctx.sent.length;
  check('a vehicle that gets much closer is re-sent', n > 1, true);
  check('  but only when 1.8x wider (130 -> 484 px: at most 3)', n <= 3, true); }

// ---- 5. the small-vehicle floor still holds ---------------------------------
{ const d = drive(), t = track('tiny', K.MIN_SEND_PX - 20);
  for (let f = 0; f < 30; f++) { d.ctx.now = 1000 + f * FRAME; d.see(t); d.endFrame(); }
  check('a vehicle under MIN_SEND_PX is never uploaded', d.ctx.sent.length, 0); }

// ---- 6. one unstable frame is not a vehicle ---------------------------------
{ const d = drive(), t = track('flash', 300);
  d.ctx.now = 1000; d.see(t); d.endFrame();
  for (let f = 1; f < 10; f++) { d.ctx.now = 1000 + f * FRAME; d.endFrame(); }
  check('a single detection is never sent', d.ctx.sent.length, 0); }

// ---- 7. a busy road for an hour stays under the hub's 900/h -----------------
{ const d = drive(); let id = 0;
  for (let f = 0; f < 3 * 3600 * 1.5; f++) {             // 90 minutes, a new car every frame
    d.ctx.now = 1000 + f * FRAME;
    const c = track('b' + (id++), 200, d.ctx.now - 1000); c.seen = K.MIN_FRAMES - 1;
    d.see(c); d.endFrame();
  }
  const ts = d.ctx.sent.map(s => s.at); let worst = 0;
  for (let i = 0, j = 0; i < ts.length; i++) { while (ts[i] - ts[j] >= 3600000) j++; worst = Math.max(worst, i - j + 1); }
  check('busiest rolling hour stays under the hub cap of 900', worst <= 900, true);
  check('  and the budget is actually used (>= HOUR_BUDGET)', worst >= K.HOUR_BUDGET, true);
  check('  the queue never grows past QUEUE_MAX', d.ctx.sendQ.length <= K.QUEUE_MAX, true); }

// ---- 8. overflow keeps the biggest ------------------------------------------
{ const d = drive(); d.ctx.driveNode = null;              // not enrolled yet: nothing drains
  const cars = []; for (let i = 0; i < K.QUEUE_MAX + 6; i++) cars.push(track('q' + i, 130 + i * 5));
  for (let f = 0; f < 4; f++) { d.ctx.now = 1000 + f * FRAME; cars.forEach(c => d.see(c)); d.endFrame(); }
  const kept = d.ctx.sendQ.map(t => t.id);
  check('overflow keeps QUEUE_MAX', kept.length, K.QUEUE_MAX);
  check('  and drops the smallest', kept.includes('q0'), false);
  check('  and keeps the biggest', kept.includes('q' + (K.QUEUE_MAX + 5)), true); }

// ---- 9. opening the page must not mint a camera -----------------------------
// 426 of 570 enrolled volunteer cameras had never sent a crop, because /drive
// enrolled on a GPS fix alone. Enrolment must wait for a vehicle worth sending.
function wouldEnrol(sendQ) { return new Function('sendQ', 'return ' + enrol)(sendQ); }
check('parked with the page open enrols nothing', !!wouldEnrol([]), false);
check('a vehicle worth uploading does enrol', !!wouldEnrol([{}]), true);

// ---- 10. his dashboard (crop 4 of 10-20) is not a car -----------------------
function dropped(box, vw, vh) {
  const h = { box: box }, v = { videoWidth: vw, videoHeight: vh };
  return eval(roi) || eval(dash);                         // eslint-disable-line no-eval
}
check('a dash box across the bottom of a 1280x720 frame is dropped', dropped([60, 330, 1270, 720], 1280, 720), true);
check('the same on a 1920x1080 frame', dropped([0, 500, 1900, 1079], 1920, 1080), true);
check('a car ahead through the windshield is kept', dropped([520, 300, 820, 520], 1280, 720), false);
check('a close car ahead cut by the hood is kept', dropped([300, 250, 980, 600], 1280, 720), false);
check('a car alongside reaching the bottom corner is kept', dropped([900, 320, 1280, 720], 1280, 720), false);

console.log();
if (fails) { console.error(fails + ' check(s) FAILED'); process.exit(1); }
console.log('all checks passed');

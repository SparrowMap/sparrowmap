/* SparrowMap, as one program.
 *
 * One page, three modes, one piece of state. The detector logic here is the
 * same code that was verified in the browser - letterboxing, the anchor
 * decode, the pass tracker, the sub-resolution crop - moved rather than
 * rewritten, because it was measured working and a rewrite would have to earn
 * that back.
 *
 * Modes a person cannot use yet are DISABLED rather than hidden-and-broken:
 * before a camera is registered there is exactly one thing to do.
 */
const $ = (s) => document.querySelector(s);
const KEY = 'sparrow.node';

let node = JSON.parse(localStorage.getItem(KEY) || 'null');
// The ?v= this file was loaded with; the beat says when the hub has a newer one.
const APP_V = (() => {
  try { const m = (document.currentScript.src || '').match(/[?&]v=(\d+)/); return m ? +m[1] : 0; }
  catch (e) { return 0; }
})();
let pos = null, chosenCam = '';

/* ---- 0 · a key handed to this device ---------------------------------
 * A scanned QR or an opened key file lands as #k=<id>.<token>.
 * 🚨 THE FRAGMENT IS NEVER SENT TO A SERVER. That is the point: the capability
 * travels inside the picture and is read here, so no request line, proxy log
 * or Referer header ever contained it. Stripped from history immediately, so
 * it cannot be shoulder-read from the URL bar or re-adopted after the camera
 * has been handed on.
 */
function adoptKey() {
  const m = (location.hash || '').match(/^#k=([A-Za-z0-9_]+)\.([A-Za-z0-9_\-]+)$/);
  if (!m) return;
  const [, id, token] = m;
  const prior = node;
  if (prior && prior.id && prior.id !== id &&
      !confirm(`This device already holds ${prior.id}.\n\nReplace it with ${id}?`)) {
    history.replaceState(null, '', location.pathname);
    return;
  }
  node = { id, token, name: (prior && prior.id === id && prior.name) || id };
  localStorage.setItem(KEY, JSON.stringify(node));
  history.replaceState(null, '', location.pathname);
  return true;
}
adoptKey();

// A key arriving while the app is ALREADY open changes only the fragment, so
// nothing reloads and nothing would have happened - which is exactly what a
// person pasting a key into the address bar, or following a key link from
// another tab, would do.
window.addEventListener('hashchange', () => {
  if (adoptKey()) { refreshChrome(); go('watch'); }
});

/* ---- chrome ---------------------------------------------------------- */
const say = (el, t, ok) => { el.textContent = t;
  el.className = 'msg' + (ok === true ? ' ok' : ok === false ? ' bad' : ''); };

function net(state, text) {
  $('#netdot').className = 'dot' + (state ? ' ' + state : '');
  $('#nettext').textContent = text;
}

function refreshChrome() {
  const has = !!(node && node.id && node.token);
  $('#who').textContent = has ? `${node.name || node.id} · ${node.id}`
                              : 'no camera on this device';
  document.querySelectorAll('nav.modes button').forEach((b) => {
    if (b.dataset.m !== 'setup') b.disabled = !has;
  });
  if (has && $('#nname')) {
    $('#nname').value = node.name && node.name !== node.id ? node.name : '';
  }
}

let mode = 'setup';
function go(m) {
  if (m !== 'setup' && !(node && node.token)) return;
  mode = m;
  document.querySelectorAll('main section').forEach((s) =>
    s.classList.toggle('hidden', s.id !== 'm-' + m));
  document.querySelectorAll('nav.modes button').forEach((b) =>
    b.classList.toggle('on', b.dataset.m === m));
  // Leaving a mode must release its camera, or the next mode cannot open one.
  if (m !== 'watch' && running) stop();
  if (m === 'key') { loadQR(); removalState(); }
  if (m === 'setup' || m === 'watch') listCameras();
}
document.querySelectorAll('nav.modes button').forEach((b) => {
  b.onclick = () => go(b.dataset.m);
});

/* ---- cameras --------------------------------------------------------- */
async function listCameras() {
  try {
    const cams = (await navigator.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === 'videoinput');
    const sel = $('#camsel');
    if (!sel) return;
    // Labels only exist after permission has been granted once, which is why
    // this is called again when a stream starts rather than only on load.
    sel.innerHTML = '<option value="">default camera</option>'
      + cams.map((c, i) => `<option value="${c.deviceId}">${
          (c.label || ('camera ' + (i + 1))).replace(/[<>]/g, '')}</option>`).join('');
    sel.value = chosenCam;
  } catch (e) { /* a convenience, never a blocker */ }
}
document.addEventListener('change', (e) => {
  if (e.target && e.target.id === 'camsel') chosenCam = e.target.value;
});

function videoConstraints() {
  return chosenCam
    ? { deviceId: { exact: chosenCam }, width: { ideal: 1280 }, height: { ideal: 960 } }
    // `ideal` not `exact`: exact fails outright on any device with no rear
    // camera, which is every desktop.
    : { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 960 } };
}

function camError(e) {
  return location.protocol === 'https:' || location.hostname === 'localhost'
    ? 'Camera refused: ' + e.message
    : 'The camera needs https. Open this page on the https address.';
}

/* ---- 1 · setup ------------------------------------------------------- */
$('#btnGeo').onclick = () => {
  say($('#setupMsg'), 'Asking for location…');
  navigator.geolocation.getCurrentPosition(
    (p) => { pos = p.coords;
      say($('#setupMsg'), `Got it: ${p.coords.latitude.toFixed(5)}, `
                        + `${p.coords.longitude.toFixed(5)}`, true); },
    (e) => say($('#setupMsg'), 'Location refused: ' + e.message, false),
    { enableHighAccuracy: true, timeout: 15000 });
};

/* 🚨 EVERY TAP MINTED A WHOLE CAMERA.
 * Registering takes a second or two - it does a road lookup - and nothing said
 * so, so people tapped again. Each tap was a fresh POST with no node_id, and
 * enroll() treats that as a NEW camera: "3162" five times in 26 seconds,
 * "Driving 2026-08-12" four times in the same instant, six names duplicated
 * inside two minutes.
 *
 * The damage was not cosmetic. Those duplicates inflated the camera count,
 * consumed the network-wide enrol limit in one burst (which is what locked him
 * out of registering a real camera), and poisoned the setup funnel: the extra
 * rows sit at 'registered' for ever, and I read that as "half of all
 * volunteers never press Watch". Collapsing duplicates, the real figure was 1
 * in 11, and the start rate was 64% rather than 41%. A metric can be broken by
 * a button.
 */
let enrolling = false;
$('#btnEnroll').onclick = async () => {
  if (enrolling) return;
  const name = $('#nname').value.trim();
  if (!name || (!pos && !node)) {
    return say($('#setupMsg'), 'Need a name and a position.', false);
  }
  enrolling = true;
  $('#btnEnroll').disabled = true;
  $('#btnEnroll').textContent = 'Registering…';
  // What KIND of camera this is changes what the map draws. A fixed camera in
  // a window watches a stretch of road and that stretch is published; a phone
  // you carry watches wherever it happens to be pointing and must never have a
  // road drawn for it. Hard-coding 'fixed' here gave four phones an imaginary
  // 80 m road running due north - so ask, and default to the safe answer.
  const kind = $('#kindsel') ? $('#kindsel').value : 'phone';
  const body = { name, kind, reach_m: +$('#reach').value || 30 };
  if (pos) { body.lat = pos.latitude; body.lon = pos.longitude;
             if (pos.heading != null) body.heading = pos.heading; }
  // Proving ownership when updating. Without it the server refuses, which is
  // what stops anyone re-registering somebody else's camera.
  if (node) { body.node_id = node.id; body.token = node.token; }

  const r = await fetch('/api/enroll', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then((x) => x.json()).catch((e) => ({ error: String(e) }));

  // Re-enable on FAILURE only. On success the button is gone from the flow -
  // leaving it live is how a second camera gets made by someone checking their
  // work.
  enrolling = false;
  $('#btnEnroll').textContent = 'Register this camera';
  if (r.error) {
    $('#btnEnroll').disabled = false;
    return say($('#setupMsg'), r.error, false);
  }

  node = { id: r.id, token: r.token, name };
  if (r.review_token) node.review_token = r.review_token;
  localStorage.setItem(KEY, JSON.stringify(node));
  say($('#setupMsg'), `Registered as ${r.id}.`, true);
  stage('registered');
  showKeyNow(node);
  showReviewBanner(node.review_token);
  refreshChrome();
};

/* 🔑 SHOW THE KEY THE MOMENT IT EXISTS, NOT ONE TAB AWAY.
 *
 * HIS REPORT: "you didnt show the token where the popup tells you to copy it
 * now - had to go to key page - two clicks - show token first."
 *
 * Registering said "Watch and Key are open now", which is a signpost, not a
 * key. The one moment a person is guaranteed to be paying attention to their
 * credential is the second after they create it - and that was the moment we
 * used to point at a tab. Every extra tap between "here is your key" and "it is
 * saved somewhere" is a chance to lose it, and losing it is already the single
 * biggest cause of dead cameras on this map: a browser that forgets its key
 * enrols a SECOND camera and orphans the first.
 *
 * So the key is printed here, once, with a copy button and the same link the
 * Key page offers. The Key page still exists and is still correct - this is not
 * a second implementation of it, it is the same two strings shown at the moment
 * they matter.
 *
 * ⚠️ Built with DOM calls, no innerHTML: the strict CSP on this deployment
 * refuses inline script/style, and this block carries a live credential.
 */
function showKeyNow(n) {
  if (!(n && n.id && n.token)) return;
  const key = n.id + '.' + n.token;
  let el = document.getElementById('keyNow');
  if (!el) {
    el = document.createElement('div');
    el.id = 'keyNow';
    Object.assign(el.style, {
      margin: '10px 0', padding: '12px 14px', borderRadius: '12px',
      border: '1px solid #3a5', background: '#0e1a14', color: '#d8f3e4',
      font: '13px/1.6 system-ui, sans-serif' });
    const host = document.querySelector('main') || document.body;
    host.insertBefore(el, host.firstChild);
  }
  el.textContent = '';

  const h = document.createElement('div');
  h.textContent = '🔑 This is your camera key. Save it now.';
  Object.assign(h.style, { fontWeight: '700', marginBottom: '2px' });
  el.appendChild(h);

  const why = document.createElement('div');
  why.textContent = 'It is the only way back into this camera if this browser '
                  + 'forgets it. Nothing else can recover it.';
  Object.assign(why.style, { color: '#9dc7b2', fontSize: '12.5px' });
  el.appendChild(why);

  const box = document.createElement('div');
  box.textContent = key;
  Object.assign(box.style, {
    margin: '9px 0', padding: '9px 10px', borderRadius: '8px',
    background: '#06100b', border: '1px solid #24402f',
    font: '12.5px ui-monospace, Consolas, monospace', wordBreak: 'break-all',
    userSelect: 'all' });
  el.appendChild(box);

  const row = document.createElement('div');
  Object.assign(row.style, { display: 'flex', gap: '8px', flexWrap: 'wrap' });
  if (window.sparrowCopyButton) {
    row.appendChild(window.sparrowCopyButton(key,
      { label: '📋 Copy key', aria: 'copy your camera key' }));
    row.appendChild(window.sparrowCopyButton(
      location.origin + '/signin#k=' + encodeURIComponent(key),
      { label: '🔗 Copy sign-in link', aria: 'copy a sign-in link' }));
  }
  el.appendChild(row);
}

/* Show the owner where to review their OWN camera's catches. The token is
 * captured at enrollment; a camera enrolled before tokens existed fetches it
 * once, proving ownership with its node token. Built with DOM calls (no inline
 * style/script) so it survives the strict CSP. */
function showReviewBanner(tok) {
  // ⚠️ The aim link must NOT be hostage to the review token.
  // This returned early whenever there was no review token, and the aim link
  // hung off the end of it - so the cameras least likely to have been set up
  // carefully would have been the ones never offered the chance to aim.
  if (!tok) { showAimBanner(); return; }
  let el = document.getElementById('reviewBanner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'reviewBanner';
    Object.assign(el.style, {
      margin: '10px 0', padding: '10px 12px', borderRadius: '10px',
      border: '1px solid #244', background: '#0f1621', color: '#bfe3d0',
      font: '13px/1.5 system-ui, sans-serif', wordBreak: 'break-all' });
    const host = document.querySelector('main') || document.body;
    host.insertBefore(el, host.firstChild);
  }
  el.textContent = '';

  // 🔑 A TAPPABLE LINK, NOT A TOKEN TO COPY.
  // This used to print the raw token and expect the volunteer to select 32
  // characters out of a banner and paste them into a box on their phone.
  // Most never did, so cameras contributed catches their owner never saw and
  // could not rule on. The token rides in the FRAGMENT, which is never sent to
  // the server, and /rv strips it from the address bar as soon as it is read.
  const line = document.createElement('div');
  line.textContent = 'Your camera has catches to review. ';
  const a = document.createElement('a');
  a.href = location.origin + '/rv#t=' + encodeURIComponent(tok);
  a.textContent = 'Open your review page →';
  Object.assign(a.style, { color: '#7fd1ff', fontWeight: '600' });
  line.appendChild(a);

  const hint = document.createElement('div');
  hint.textContent = 'Bookmark it. It only ever shows YOUR camera.';
  Object.assign(hint.style, { opacity: '.7', marginTop: '4px', fontSize: '12px' });

  // Kept, smaller, for anyone moving to another device or re-adding by hand.
  //
  // 🔑 AND NOW WITH A COPY BUTTON, BECAUSE THE LINK ONLY SOLVES ONE HALF.
  // The tappable link above covers "same browser, right now" and the wallet
  // remembers it afterwards. It does nothing for the case people actually hit:
  // camera app on the phone, review page on the laptop - or an installed PWA,
  // whose storage the browser tab cannot see. Those all end at this line, where
  // the only instruction was to select 32 characters by hand. A volunteer
  // setting up a camera reported exactly that: "the key is difficult to use".
  const code = document.createElement('code');
  code.textContent = tok;
  Object.assign(code.style, { userSelect: 'all', color: '#7fd1ff' });
  const lbl = document.createElement('div');
  Object.assign(lbl.style, { opacity: '.55', marginTop: '6px', fontSize: '11px' });
  lbl_label(lbl, 'token: ');
  lbl.appendChild(code);

  const row = document.createElement('div');
  Object.assign(row.style, { display: 'flex', gap: '8px', marginTop: '8px',
                             flexWrap: 'wrap' });
  if (window.sparrowCopyButton) {
    row.appendChild(window.sparrowCopyButton(tok, { label: '📋 Copy token' }));
    // The whole link, for sending to another device - a token on its own still
    // needs the receiver to know where to paste it, and this one does not.
    row.appendChild(window.sparrowCopyButton(
      () => location.origin + '/rv#t=' + encodeURIComponent(tok),
      { label: '🔗 Copy review link', done: '✓ Link copied' }));
  }
  lbl.appendChild(row);

  el.appendChild(line); el.appendChild(hint); el.appendChild(lbl);
  showAimLink(el);
}

/* 🧭 THE AIM LINK, BECAUSE UNTIL NOW THERE WAS NOWHERE TO AIM FROM.
 * The placement screen only ever existed inside camctl, the DESKTOP tool - so
 * no volunteer has ever been asked which way their camera points, and 28 of 31
 * cameras sit at heading 0. The server then treats that 0 as a real aim and
 * points the cone due north, which is how a camera watching one street ends up
 * drawn on the one perpendicular to it.
 *
 * The key rides in the SAME #k=<id>.<token> fragment the camera app already
 * uses, so it is never in a request line, and /aim strips it from the address
 * bar as soon as it is read. */
function showAimBanner() {
  if (!node || !node.id || !node.token) return;
  // The review banner already carries the aim link, so never show both.
  if (document.getElementById('aimBanner') ||
      document.getElementById('reviewBanner')) return;
  const el = document.createElement('div');
  el.id = 'aimBanner';
  Object.assign(el.style, {
    margin: '10px 0', padding: '10px 12px', borderRadius: '10px',
    border: '1px solid #244', background: '#0f1621', color: '#bfe3d0',
    font: '13px/1.5 system-ui, sans-serif' });
  const host = document.querySelector('main') || document.body;
  host.insertBefore(el, host.firstChild);
  showAimLink(el);
}

function showAimLink(host) {
  if (!node || !node.id || !node.token) return;
  const wrap = document.createElement('div');
  Object.assign(wrap.style, { marginTop: '8px' });
  const a = document.createElement('a');
  a.href = location.origin + '/aim#k=' + encodeURIComponent(node.id) +
           '.' + encodeURIComponent(node.token);
  a.textContent = 'Point this camera at the right road →';
  Object.assign(a.style, { color: '#3ddc97', fontWeight: '600' });
  const why = document.createElement('div');
  why.textContent = 'Do this when you set it up, and again any time you move it.';
  Object.assign(why.style, { opacity: '.7', marginTop: '4px', fontSize: '12px' });
  wrap.appendChild(a); wrap.appendChild(why);
  host.appendChild(wrap);
}
function lbl_label(node, text) { node.appendChild(document.createTextNode(text)); }

async function ensureReviewToken() {
  if (!(node && node.id && node.token)) return;
  if (node.review_token) { showReviewBanner(node.review_token); return; }
  try {
    const r = await fetch('/api/rv/my-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.token },
      body: JSON.stringify({ node_id: node.id }),
    }).then((x) => x.json());
    if (r && r.new && r.review_token) {
      node.review_token = r.review_token;
      localStorage.setItem(KEY, JSON.stringify(node));
      showReviewBanner(node.review_token);
    } else {
      showAimBanner();          // no review token to mint, but aiming still applies
    }
  } catch (e) { showAimBanner(); /* offline review is fine; aiming is local */ }
}

/* ---- 2 · watch (the detector) ---------------------------------------- */
const SIZE = 320, CONF = 0.45, SEND_EDGE = 200, MIN_FRAMES = 3, GONE_MS = 900;
const VEHICLE = { 2: 'car', 3: 'motorcycle', 5: 'bus', 7: 'truck' };
let session = null, stream = null, running = false, wakeLock = null, beatTimer = null;

/* The last moment a frame actually went through the detector. The beat reads
 * it, so "online" means this phone is really watching - not just that a timer
 * is ticking in a tab whose detector has wedged or been throttled by the
 * browser. A quiet street still beats; only a stopped pipeline goes dark. */
let lastWork = 0;
const STALL_AFTER_MS = 120000;

/* 🚨 THE BEAT REPORTS ITSELF, IMMEDIATELY.
 *
 * The map only learns a camera is up on its next poll, and the operator had no
 * way to tell "working" from "silently broken" without staring at another
 * device for a minute. That is unusable feedback. The phone knows the answer
 * the instant the request returns, so it says so here: LIVE on a 200, and the
 * actual reason on anything else, rather than swallowing the failure. */
const QUIET_AFTER_S = 24 * 3600;   // a whole day with no vehicle at all
let lastBeatReply = null;
async function sendBeat() {
  if (!(node && node.id && node.token)) return;
  if (!lastWork || Date.now() - lastWork > STALL_AFTER_MS) {
    net('warn', 'starting…');
    return;
  }
  try {
    const r = await fetch('/api/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.token },
      body: JSON.stringify({ node_id: node.id }),
    });
    if (r.ok) {
      net('on', 'LIVE on the map');
      // The hub only asks for pictures of vehicles it has already published.
      try {
        const d = await r.json();
        lastBeatReply = d;
        if (d && d.want_full && d.want_full.length) sendHeldFull(d.want_full);
        if (d) maybeUpdate(d.app_v);
      } catch (e) { /* an older hub answers without want_full */ }
      say($('#watchMsg'), 'Watching — this camera is LIVE on the map.', true);
      // 🔕 Live, but nothing reaching the map for a day: the phone is working and
      // seeing no vehicles at all (every vehicle counts, not just police), which
      // is almost always the lens - turned off the road, fogged, or blocked.
      const ls = lastBeatReply && lastBeatReply.last_seen;
      if (ls && Date.now() / 1000 - ls > QUIET_AFTER_S) {
        const days = Math.round((Date.now() / 1000 - ls) / 86400 * 10) / 10;
        net('warn', 'LIVE, but quiet');
        say($('#watchMsg'), `Live on the map, but no vehicle has reached it for ${days} days. `
          + 'Check the camera still sees the road (not fogged or turned away): open Aim, '
          + 'or reload this page.', false);
      }
    } else if (r.status === 401) {
      net('warn', 'not signed in');
      say($('#watchMsg'), 'The hub rejected this camera’s key (401). '
        + 'Re-register it in Setup.', false);
    } else if (r.status === 404) {
      net('warn', 'unknown camera');
      say($('#watchMsg'), 'The hub does not know this camera (404). '
        + 'Re-register it in Setup.', false);
    } else {
      net('warn', 'hub error ' + r.status);
    }
  } catch (e) {
    // Offline or the hub is unreachable. Detection keeps running; say so
    // plainly rather than leaving a stale "LIVE" on screen.
    net('warn', 'offline — retrying');
  }
}
let tracks = [], nextId = 1, seen = 0, sent = 0, fpsEMA = 0, lastT = 0;
let LB = { s: 1, ox: 0, oy: 0 };

const pre = document.createElement('canvas'); pre.width = pre.height = SIZE;
const pctx = pre.getContext('2d', { willReadFrequently: true });
const cropCv = document.createElement('canvas');

/* The model is ~38 MB and on a phone that is a real wait. Fetching it here
 * rather than letting the runtime do it silently means the bar can move: a
 * stalled download and a slow one look identical otherwise, and that is exactly
 * the ambiguity that made "is my camera working?" unanswerable. */
async function loadModel() {
  net('busy', 'loading detector');
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.simd = true;
  ort.env.wasm.wasmPaths = '/vendor/';

  let bytes = null;
  try {
    const res = await fetch('/vendor/yolo11s.onnx');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const total = +res.headers.get('Content-Length') || 37925610;
    const reader = res.body && res.body.getReader ? res.body.getReader() : null;
    if (reader) {
      const chunks = []; let got = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value); got += value.length;
        const pct = Math.min(99, Math.round((got / total) * 100));
        $('#loadbar').style.width = pct + '%';
        // Name the thing being downloaded, every time it repaints. This line
        // is what someone stares at while deciding whether to kill it, and
        // "Downloading the detector" alone does not say that the detector is
        // a model file running ON THIS PHONE - which is the answer to the
        // question actually being asked.
        say($('#watchMsg'), `Downloading the vehicle detector — `
          + `${(got / 1048576).toFixed(0)} of ${(total / 1048576).toFixed(0)} MB `
          + `(${pct}%). Runs on this phone; first time only.`);
      }
      bytes = new Uint8Array(got); let at = 0;
      for (const c of chunks) { bytes.set(c, at); at += c.length; }
    } else {
      bytes = new Uint8Array(await res.arrayBuffer());
    }
  } catch (e) {
    // Fall back to letting the runtime fetch it by URL; no progress, but it
    // still works (and a cached copy makes this instant anyway).
    bytes = null;
  }

  say($('#watchMsg'), 'Detector downloaded. Starting it on this phone…');
  session = await ort.InferenceSession.create(bytes || '/vendor/yolo11s.onnx',
    { executionProviders: ['wasm'], graphOptimizationLevel: 'all' });
  $('#loadbar').style.width = '100%';
  say($('#watchMsg'), 'Detector ready and cached — instant next time.', true);
}

/* 🚨 LETTERBOX, NEVER STRETCH. Squashing the frame into the square distorts
   every vehicle by the aspect ratio: the same model on the same picture found
   ZERO vehicles stretched and a car at 0.88 letterboxed. */
function letterbox(src, sw, sh) {
  LB.s = Math.min(SIZE / sw, SIZE / sh);
  const dw = Math.round(sw * LB.s), dh = Math.round(sh * LB.s);
  LB.ox = (SIZE - dw) / 2; LB.oy = (SIZE - dh) / 2;
  pctx.fillStyle = '#727272';
  pctx.fillRect(0, 0, SIZE, SIZE);
  pctx.drawImage(src, LB.ox, LB.oy, dw, dh);
}

const iou = (a, b) => {
  const x0 = Math.max(a[0], b[0]), y0 = Math.max(a[1], b[1]);
  const x1 = Math.min(a[2], b[2]), y1 = Math.min(a[3], b[3]);
  const i = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
  const ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - i;
  return ua > 0 ? i / ua : 0;
};

function decode(out) {
  const d = out.data, N = out.dims[2], hits = [];
  for (let i = 0; i < N; i++) {
    let best = -1, bs = 0;
    for (const c of [2, 3, 5, 7]) { const v = d[(4 + c) * N + i]; if (v > bs) { bs = v; best = c; } }
    if (bs < CONF) continue;
    const cx = d[i], cy = d[N + i], w = d[2*N + i], h = d[3*N + i];
    // Undo the padding, THEN the scale. Order matters.
    hits.push({ cls: VEHICLE[best], score: bs, box: [
      (cx - w/2 - LB.ox) / LB.s, (cy - h/2 - LB.oy) / LB.s,
      (cx + w/2 - LB.ox) / LB.s, (cy + h/2 - LB.oy) / LB.s] });
  }
  // The export carries no NMS at this opset; without this one car arrives as
  // six overlapping boxes and six "vehicles".
  hits.sort((a, b) => b.score - a.score);
  const keep = [];
  for (const h of hits) if (!keep.some((k) => iou(k.box, h.box) > 0.45)) keep.push(h);
  return keep;
}

function drawOverlay(hits, vw, vh) {
  const cv = $('#ov'), g = cv.getContext('2d');
  if (cv.width !== cv.clientWidth) { cv.width = cv.clientWidth; cv.height = cv.clientHeight; }
  const s = Math.max(cv.width / vw, cv.height / vh);
  const ox = (cv.width - vw * s) / 2, oy = (cv.height - vh * s) / 2;
  g.clearRect(0, 0, cv.width, cv.height);
  g.lineWidth = 2; g.strokeStyle = '#3d8cff'; g.fillStyle = '#3d8cff';
  g.font = '600 11px ui-monospace,monospace';
  for (const h of hits) {
    const [x0, y0, x1, y1] = h.box;
    g.strokeRect(x0*s+ox, y0*s+oy, (x1-x0)*s, (y1-y0)*s);
    // The detector's own class (car/truck/bus/motorcycle) is a claim this
    // project never publishes - the map has two public categories, a vehicle
    // or a government vehicle. The body type still travels with the sighting;
    // it is simply not asserted on screen. See camctl VEHICLE_LABEL.
    g.fillText('vehicle', x0*s+ox, Math.max(10, y0*s+oy - 3));
  }
}

/* The crop is shrunk below plate legibility HERE, on the device. This is the
   privacy control, not a bandwidth saving - and the server measures it rather
   than believing this page. */
function cropOf(video, box, maxEdge) {
  const [x0, y0, x1, y1] = box;
  // Asymmetric: a roof light bar sits just outside the detector's box, and a
  // crop that clips it removes the feature the decision rests on. Nothing
  // diagnostic hangs off the bottom. See drive.html cropOf for the full note.
  const bw = x1-x0, bh = y1-y0;
  const padX = bw * 0.10, padTop = bh * 0.28, padBot = bh * 0.08;
  const sx = Math.max(0, x0-padX), sy = Math.max(0, y0-padTop);
  const sw = Math.min(video.videoWidth - sx, bw + 2*padX);
  const sh = Math.min(video.videoHeight - sy, bh + padTop + padBot);
  const s = Math.min(1, (maxEdge || SEND_EDGE) / Math.max(sw, sh));
  cropCv.width = Math.max(1, Math.round(sw * s));
  cropCv.height = Math.max(1, Math.round(sh * s));
  cropCv.getContext('2d').drawImage(video, sx, sy, sw, sh, 0, 0, cropCv.width, cropCv.height);
  // Only the small preview is redacted; the 900px copy keeps its plate.
  if (!maxEdge || maxEdge <= SEND_EDGE)
    redactPlateBand(cropCv, { padX: padX, padTop: padTop, bw: bw, bh: bh, sw: sw, sh: sh });
  return cropCv.toDataURL('image/jpeg', 0.82);
}

/* 🚨 BLOCK THE PLATE OUT OF THE PREVIEW, ALWAYS.
 *
 * The 200px cap is measured on the CROP, not on the plate - so it only makes a
 * plate illegible when the vehicle is a reasonable distance away. A car that
 * fills a dashcam frame is scaled to the same 200px, which means its plate is
 * proportionally just as big and can still be read. He reported exactly that:
 * "sometimes a citizen is slipping in and close up from a dashcam view".
 *
 * There is a real plate detector, but it lives in the heavy pipeline on a GPU.
 * A phone cannot run it and the hub cannot afford it at a quarter of a million
 * sightings a day. So this is geometric: a plate sits in the lower middle of a
 * vehicle whether you are behind it or in front of it, and that band is painted
 * out of the PREVIEW before anything leaves the device.
 *
 * ⚠️ THE PREVIEW ONLY. The full-resolution copy held on the device keeps its
 * plate untouched: a government vehicle is published WITH a legible plate on
 * purpose, and that copy is only ever handed over for a vehicle the hub has
 * already published.
 *
 * ⚠️ IT DOES NOT COVER THE LIVERY. The band is the bumper strip; a light bar,
 * door decals, a push bar and the body shape are all outside it, which is what
 * the classifier and the human reviewer actually decide on.
 */
function redactPlateBand(cv, geom) {
  try {
    var g = cv.getContext('2d');
    // Where the VEHICLE sits inside the padded crop.
    var vx = (geom.padX / geom.sw) * cv.width;
    var vy = (geom.padTop / geom.sh) * cv.height;
    var vw = (geom.bw / geom.sw) * cv.width;
    var vh = (geom.bh / geom.sh) * cv.height;
    // A plate: lower middle, generous because this must fail safe.
    var x = vx + vw * 0.20, w = vw * 0.60;
    var y = vy + vh * 0.56, h = vh * 0.40;
    if (w <= 0 || h <= 0) return;
    g.save();
    g.fillStyle = '#000';
    g.fillRect(Math.max(0, x), Math.max(0, y),
               Math.min(w, cv.width), Math.min(h, cv.height));
    g.restore();
  } catch (e) { /* a preview that cannot be redacted is not uploaded raw:
                   see the callers, which treat a throw as fatal. */ }
}


/* 🚨 THE FULL PICTURE, HELD ON THE DEVICE THAT TOOK IT.
 *
 * What gets UPLOADED stays capped at SEND_EDGE (200px) - that cap is the plate
 * guarantee for every private vehicle and it does not move. But the hub cannot
 * improve a picture it never received, and by the time the head decides a
 * vehicle was a police one, the only copy on the server is the ruined one.
 * Confirming on /rv cannot fix it either: that reviewer is looking at somebody
 * else's sighting and never had the original.
 *
 * So the camera keeps a better copy of its OWN sightings for a while, and hands
 * it over only when the hub says that sighting was PUBLISHED. Nothing is sent
 * on the strength of this phone's opinion.
 *
 * ⚠️ BOUNDED HARD. At most FULL_MAX_OPEN undecided crops, each deleted once the
 * hub says it can never be published; only candidates are kept for days. */
// 🚨 THE HUB DECIDES HOW LONG A CROP IS HELD, NOT THE PHONE.
// 2026-10-13: every phone-camera police sighting published that week kept the
// painted-out preview (7 of 7). The phone guessed: 48 crops for 1 hour, in
// memory. A busy road (~200 cars an hour) pushed a crop out in ~15 minutes, a
// review lands 2.6 h after capture at the median and up to 6.9 days later, and
// a reload forgot everything. And a phone crop is never `parked` at ingest -
// box_puller scores it at home and moves it to the pen minutes later - so the
// phone cannot tell a candidate from ordinary traffic on its own.
//
// So the clear crop goes to IndexedDB (survives a reload), and once a minute
// the phone names what it holds on /api/node/parked. The hub answers
// `want_full` (published: upload it now) and `keep` (still in the inbox or the
// review pen: may yet be published). Anything else is deleted after
// FULL_GRACE_MS, which covers the inbox -> pen hop. So only candidates are kept
// for days; ordinary traffic leaves the phone within minutes.
const FULL_EDGE = 900;
const FULL_KEEP_MS = 10 * 24 * 3600 * 1000;   // a pen card has waited 6.9 days
const FULL_GRACE_MS = 15 * 60 * 1000;         // box_puller's inbox -> pen hop
const FULL_ORPHAN_MS = 12 * 3600 * 1000;      // never answered (mirror.INBOX_TTL)
const FULL_MAX_OPEN = 300;                    // undecided crops, ~100 KB each
const fullMem = new Map();   // the fallback when IndexedDB is unavailable
let _fullDb;

function fullDb() {
  if (_fullDb !== undefined) return Promise.resolve(_fullDb);
  return new Promise((res) => {
    try {
      const rq = indexedDB.open('sparrow-app-full', 1);
      rq.onupgradeneeded = () => rq.result.createObjectStore('full', { keyPath: 'id' });
      rq.onsuccess = () => res(_fullDb = rq.result);
      rq.onerror = () => res(_fullDb = null);
    } catch (e) { res(_fullDb = null); }
  });
}

async function fullTx(mode, fn) {
  const d = await fullDb();
  if (!d) return fn(null);
  return new Promise((res) => {
    try {
      const t = d.transaction('full', mode);
      let out;
      Promise.resolve(fn(t.objectStore('full'))).then((v) => { out = v; });
      t.oncomplete = () => res(out);
      t.onerror = t.onabort = () => res(out);
    } catch (e) { res(fn(null)); }
  });
}

function holdFull(id, data) {
  if (!id || !data || !node) return;
  const row = { id: id, node: node.id, data: data, at: Date.now(), keep: false };
  fullMem.set(id, row);
  // Out of memory only once the disk copy has committed.
  fullTx('readwrite', (s) => { if (s) { s.put(row); return true; } })
    .then((ok) => { if (ok) fullMem.delete(id); });
  // No IndexedDB: memory is all there is, so bound it the same way.
  if (fullMem.size > FULL_MAX_OPEN) {
    const open = [...fullMem.values()].filter((r) => !r.keep).sort((a, b) => a.at - b.at);
    open.slice(0, fullMem.size - FULL_MAX_OPEN).forEach((r) => fullMem.delete(r.id));
  }
}

function fullGet(id) {
  if (fullMem.has(id)) return Promise.resolve(fullMem.get(id));
  return fullTx('readonly', (s) => s && new Promise((res) => {
    const r = s.get(id); r.onsuccess = () => res(r.result || null); r.onerror = () => res(null);
  }));
}

function fullDel(id) {
  fullMem.delete(id);
  return fullTx('readwrite', (s) => { if (s) s.delete(id); });
}

/* Every held row WITHOUT its picture, expired and foreign ones removed. */
function fullRows() {
  const now = Date.now();
  const ok = (v) => node && v.node === node.id
    && now - v.at < (v.keep ? FULL_KEEP_MS : FULL_ORPHAN_MS);
  const mem = [...fullMem.values()];
  return fullTx('readwrite', (s) => {
    if (!s) return mem.filter(ok);
    return new Promise((res) => {
      const rows = [];
      const c = s.openCursor();
      c.onsuccess = () => {
        const cur = c.result;
        if (!cur) {
          // Undecided crops past the cap go oldest first; kept ones never do.
          const open = rows.filter((r) => !r.keep).sort((a, b) => b.at - a.at);
          open.slice(FULL_MAX_OPEN).forEach((r) => s.delete(r.id));
          const drop = new Set(open.slice(FULL_MAX_OPEN).map((r) => r.id));
          return res(rows.filter((r) => !drop.has(r.id)).concat(mem.filter(ok)));
        }
        const v = cur.value;
        if (ok(v)) rows.push({ id: v.id, at: v.at, keep: v.keep });
        else cur.delete();
        cur.continue();
      };
      c.onerror = () => res(rows);
    });
  });
}

/* Upload the clear crop for each id the hub wants, one at a time. */
async function sendHeldFull(ids) {
  if (!Array.isArray(ids) || !node) return;
  for (const id of ids) {
    const held = await fullGet(id);
    if (!held) continue;
    try {
      const r = await fetch('/api/sighting/fullres', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'Authorization': 'Bearer ' + node.token },
        body: JSON.stringify({ node_id: node.id, id: id, snap_b64: held.data }),
      }).then((x) => x.json());
      // Drop it whether accepted or refused: a refusal will not become an
      // acceptance on the next beat, and retrying forever is how a phone ends
      // up uploading the same rejected picture all day.
      await fullDel(id);
      if (r && r.ok) fullSent++;
    } catch (e) { /* offline; it will be asked again while we still hold it */ }
  }
}

/* Once a minute, watching or not: the verdict usually lands long after the pass. */
let fullSyncing = false;
async function syncHeld() {
  if (!(node && node.id && node.token) || fullSyncing) return;
  fullSyncing = true;
  try {
    const rows = await fullRows();
    if (!rows.length) return;
    // Kept ones first, then the newest: what is most likely to be asked for.
    rows.sort((a, b) => (b.keep - a.keep) || (b.at - a.at));
    const sent = rows.slice(0, 400);
    const r = await fetch('/api/node/parked', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.token },
      body: JSON.stringify({ node_id: node.id, ids: [], held: sent.map((x) => x.id) }),
    });
    if (!r.ok) return;
    const d = await r.json();
    const want = new Set(d.want_full || []);
    if (want.size) await sendHeldFull([...want]);
    // ⚠️ Delete only on an ANSWER. An older hub, or one whose lookup failed,
    // sends no `keep`, and a missing answer must never read as "drop it all".
    if (!Array.isArray(d.keep)) return;
    const keep = new Set(d.keep);
    const now = Date.now();
    await fullTx('readwrite', (s) => {
      for (const x of sent) {
        if (want.has(x.id)) continue;
        if (keep.has(x.id)) {
          if (!x.keep) {
            if (s) { const g = s.get(x.id);
                     g.onsuccess = () => { if (g.result) s.put({ ...g.result, keep: true }); }; }
            else if (fullMem.has(x.id)) fullMem.get(x.id).keep = true;
          }
        } else if (now - x.at > FULL_GRACE_MS) {
          if (s) s.delete(x.id);
          fullMem.delete(x.id);
        }
      }
    });
  } catch (e) { /* offline: nothing is deleted, it is asked again next minute */ }
  finally { fullSyncing = false; }
}
setTimeout(syncHeld, 4000);
setInterval(syncHeld, 60000);

let fullSent = 0;

/* 🚨 A PHONE CAMERA PICKS UP A FIX BY ITSELF.
 * 2026-10-13: volunteers run this page for days with the screen on, so a fix
 * reached a phone only when somebody happened to reload it - the clear-photo
 * fix shipped while both online phone cameras kept the old code. The beat now
 * carries the hub's current version; a phone that is behind reloads and, if it
 * was watching, starts watching again with no tap (camera permission is already
 * granted on this origin). Pages and /static/ are network-first in sw.js, so a
 * reload gets the new code.
 * ⚠️ ONCE PER VERSION per tab: if the reload still comes back old (offline, a
 * cache), it does not loop. Never while the "is this a cop?" card is open. */
function maybeUpdate(v) {
  if (!v || !APP_V || v <= APP_V) return;
  try {
    if (sessionStorage.getItem('sparrow.upd') === String(v)) return;
    const ask = $('#askNow');
    if (ask && !ask.classList.contains('hidden')) return;
    sessionStorage.setItem('sparrow.upd', String(v));
    if (running) sessionStorage.setItem('sparrow.resume', '1');
  } catch (e) { return; }      // no sessionStorage: no loop guard, so no reload
  location.reload();
}

async function submit(tr) {
  try {
    const r = await fetch('/api/sightings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.token },
      body: JSON.stringify({
        node_id: node.id, ts: Date.now() / 1000, source: 'phone_node',
        body: tr.cls, det_conf: tr.score,
        plate_text: '', plate_conf: 0, evidence: {}, snap_b64: tr.best }),
    }).then((x) => x.json());
    if (r.error) { net('busy', 'hub: ' + r.error); return; }
    sent++; $('#sSent').textContent = sent;
    net('on', 'sending');
    // The hub has just told us a human is being asked about this crop. Ask the
    // one who is standing here, while the vehicle may still be in sight.
    if (r.id && tr.fullBest) holdFull(r.id, tr.fullBest);
    if (r.parked && r.id) askNow(r.id, tr.best, r.vclass);
  } catch (e) { net('busy', 'hub unreachable'); }
}

/* 🚨 ASK ABOUT THE AIM EVERY TIME THE CAMERA STARTS.
 * A camera with heading 0 was never pointed at anything: the map guesses which
 * road it watches, and the guess is wrong often enough to matter. One
 * volunteer typed the answer into their camera's NAME - "Ross street
 * eastbound" - while the map drew them on Ashford Street for a day, because
 * nothing ever asked them.
 *
 * Checked on START rather than at registration, which is what makes it reach
 * the cameras that already exist: 41 of 52 spans were drawn from an unset
 * heading, and none of those owners will ever re-register. Every one of them
 * presses Watch.
 *
 * It does NOT block watching. Stopping an unaimed camera from contributing
 * would trade a wrong road for no sightings at all, and the road is the
 * smaller error. It nags once per session and goes away for good the moment
 * the aim is set.
 */
async function checkAim() {
  if (!(node && node.id && node.token)) return;
  try {
    const r = await fetch('/api/node/me?id=' + encodeURIComponent(node.id),
      { headers: { 'Authorization': 'Bearer ' + node.token } });
    if (!r.ok) return;
    const d = await r.json();
    // A carried camera has no watched road, so there is nothing to aim.
    if (d.kind === 'mobile') return;
    if (d.heading) return;                    // already aimed: never ask again
    showAimNag(d.road_name);
  } catch (e) { /* never let this interfere with watching */ }
}

function showAimNag(guessedRoad) {
  if (document.getElementById('aimNag')) return;
  const el = document.createElement('div');
  el.id = 'aimNag';
  Object.assign(el.style, {
    margin: '10px 0', padding: '12px 14px', borderRadius: '10px',
    border: '1px solid #6b4a17', background: '#2a1a0a', color: '#ffd9a3',
    font: '13px/1.6 system-ui, sans-serif' });
  const road = guessedRoad
    ? `The map currently shows you watching <b>${guessedRoad}</b>, which is a guess.`
    : `The map cannot tell which road you watch.`;
  el.innerHTML = `<b>Point this camera at the road</b><br>${road}
    Nobody has told it which way this camera faces, so it picked the nearest
    road it could find. It takes about ten seconds and the page tells you which
    road it landed on.`;
  const a = document.createElement('a');
  a.href = location.origin + '/aim#k=' + encodeURIComponent(node.id) + '.'
           + encodeURIComponent(node.token);
  a.textContent = 'Set the aim →';
  Object.assign(a.style, { display: 'inline-block', marginTop: '8px',
    color: '#ffb547', fontWeight: '700', textDecoration: 'none' });
  el.appendChild(a);
  const host = document.querySelector('#m-watch .box') || document.querySelector('main');
  if (host) host.insertBefore(el, host.firstChild);
}

/* ---- "is that one a cop?", asked while it is still in sight ------------
 * The desktop node has had this since the start (run_live.push_confirm); a
 * phone volunteer had nothing until they opened /rv hours later and judged a
 * still image of a car they no longer remember. The person holding the phone
 * saw the actual vehicle - markings, light bar, the lot - and that judgement
 * is strictly better than the one they can make later from a crop.
 *
 * Deliberately narrow:
 *   * only when the hub says the crop was PARKED for review. Prompting on
 *     civilian traffic would train people to dismiss without looking, and the
 *     next prompt is the one that mattered.
 *   * one at a time, newest wins. A queue of these on a phone propped in a
 *     window is just a wall to swipe through.
 *   * it never blocks the detector. Answering is optional and the crop is in
 *     /rv either way, so ignoring it costs nothing.
 *   * needs the camera's own reviewer token, which is 'own' scope - a
 *     volunteer can only ever rule on their own camera this way.
 */
var askTimer = null;
function askNow(sid, cropDataUrl, vclass) {
  if (!(node && node.review_token)) return;   // no token, nothing to post with
  var box = $('#askNow');
  if (!box) return;
  clearTimeout(askTimer);
  $('#askImg').src = cropDataUrl || '';
  $('#askWhy').textContent = vclass ? ('classifier says: ' + vclass) : '';
  box.dataset.sid = String(sid);
  box.classList.remove('hidden');
  // Not a modal and not permanent: if nobody is holding the phone it clears
  // itself rather than covering the preview until someone comes back.
  askTimer = setTimeout(function () { box.classList.add('hidden'); }, 45000);
}

async function answerNow(call) {
  var box = $('#askNow');
  var sid = box && box.dataset.sid;
  if (!sid) return;
  box.classList.add('hidden');
  clearTimeout(askTimer);
  try {
    await fetch('/api/rv/verdict', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.review_token },
      body: JSON.stringify({ id: +sid, verdict: call }),
    });
    say($('#watchMsg'), call === 'not' ? 'Marked as not a government vehicle.'
                                       : 'Confirmed — it will appear on the map.', true);
  } catch (e) { /* it stays in /rv; nothing is lost */ }
}

function reap(now) {
  tracks = tracks.filter((tr) => {
    if (now - tr.last < GONE_MS) return true;
    // One sighting per vehicle, sent when it LEAVES - not one per frame.
    if (tr.seen >= MIN_FRAMES && tr.best && !tr.sent) { tr.sent = true; submit(tr); }
    return false;
  });
}

async function loop() {
  if (!running) return;
  const v = $('#video');
  if (v.readyState < 2) return requestAnimationFrame(loop);
  letterbox(v, v.videoWidth, v.videoHeight);
  const px = pctx.getImageData(0, 0, SIZE, SIZE).data;
  const f = new Float32Array(3 * SIZE * SIZE);
  for (let i = 0, n = SIZE * SIZE; i < n; i++) {
    f[i] = px[i*4] / 255; f[n+i] = px[i*4+1] / 255; f[2*n+i] = px[i*4+2] / 255;
  }
  let hits = [];
  try {
    const out = await session.run({ images: new ort.Tensor('float32', f, [1,3,SIZE,SIZE]) });
    hits = decode(out[Object.keys(out)[0]]);
  } catch (e) {
    say($('#watchMsg'), 'Detector error: ' + e.message, false);
    running = false; return;
  }
  lastWork = Date.now();          // a real frame went through the detector
  const now = performance.now();
  if (lastT) fpsEMA = fpsEMA * 0.8 + (1000 / (now - lastT)) * 0.2;
  lastT = now;
  $('#sFps').textContent = fpsEMA.toFixed(1);

  for (const h of hits) {
    let tr = tracks.find((t) => iou(t.box, h.box) > 0.3);
    if (!tr) {
      tr = { id: nextId++, seen: 0, best: null, bestArea: 0, sent: false, cls: h.cls };
      tracks.push(tr); seen++; $('#sSeen').textContent = seen;
    }
    tr.box = h.box; tr.last = now; tr.seen++; tr.cls = h.cls; tr.score = h.score;
    // Keep the BIGGEST view, not the first: a car is smallest as it enters.
    const area = (h.box[2]-h.box[0]) * (h.box[3]-h.box[1]);
    if (area > tr.bestArea) {
      tr.bestArea = area;
      tr.best = cropOf(v, h.box);              // 200px: the only thing UPLOADED
      // ⚠️ The better copy never leaves the device unless the hub later says
      // this sighting was published. Same crop, same padding, just not thrown
      // away yet - see holdFull.
      tr.fullBest = cropOf(v, h.box, FULL_EDGE);
    }
  }
  reap(now);
  drawOverlay(hits, v.videoWidth, v.videoHeight);
  requestAnimationFrame(loop);
}

/* 🚨 WHERE SETUP DIES. 20 of the first 36 cameras registered and never sent a
 * single heartbeat, and nothing recorded why - a denied camera permission, a
 * 38 MB model download that gave up on mobile data, and a closed tab all
 * looked identical from the server. They need completely different fixes, so
 * the biggest number in the project was the least actionable.
 *
 * A stage name from a fixed list and a four-way platform bucket. No user
 * agent, no screen size, nothing that identifies a device - and it is
 * fire-and-forget: a camera must never treat a telemetry failure as a setup
 * failure, so nothing here is awaited and nothing here can throw. */
function platformBucket() {
  var u = navigator.userAgent || '';
  if (/iPhone|iPad|iPod/i.test(u)) return 'ios';
  if (/Android/i.test(u)) return 'android';
  if (/Mobi/i.test(u)) return 'other';
  return 'desktop';
}
function stage(name) {
  try {
    if (!(node && node.id && node.token)) return;
    fetch('/api/node/progress', {
      method: 'POST',
      // ⚠️ THE TOKEN GOES IN THE HEADER, exactly as sendBeat() sends it.
      // _token_ok() reads the Authorization header and nothing else; only
      // /api/enroll also accepts it in the body. Sending it in the body here
      // returned 401 for every call, which - because this is fire-and-forget -
      // would have recorded absolutely nothing while looking like it worked.
      headers: { 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + node.token },
      body: JSON.stringify({ node_id: node.id, stage: name,
                             platform: platformBucket() }),
      keepalive: true,        // survives the tab closing, which is the case
    }).catch(function () {});  // we most want to hear about
  } catch (e) { /* never let reporting break setting up */ }
}

async function start() {
  // A camera its owner took off the map: the hub refuses its posts ("node is
  // removed"), so watching would only fail over and over. Point them at the way back.
  if (node && node.removed) { go('key'); return say($('#rmMsg'), 'This camera is off the map. Put it back first to watch again.', false); }
  stage('starting');
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints(), audio: false });
  } catch (e) { return say($('#watchMsg'), camError(e), false); }
  stage('camera_granted');
  $('#video').srcObject = stream;
  listCameras();
  stage('preview');
  if (!session) {
    stage('model_loading');
    try { await loadModel(); }
    catch (e) { return say($('#watchMsg'), 'Could not load the detector: ' + e.message, false); }
  }
  stage('model_ready');
  try { wakeLock = await navigator.wakeLock.request('screen');
        $('#pWake').className = 'pill on'; $('#pWake').textContent = 'screen kept on'; }
  catch (e) { $('#pWake').className = 'pill warn'; $('#pWake').textContent = 'screen may sleep'; }

  running = true;
  $('#stage').classList.add('live');
  $('#btnStart').classList.add('hidden');
  $('#btnStop').classList.remove('hidden');
  net('on', 'watching');
  say($('#watchMsg'), 'Watching. Leave this window visible.', true);
  // A phone on a quiet street posts no sightings for minutes, but it is still
  // watching - so beat on its own, or the map shows it offline within the
  // 90-second window. A sighting also beats; this covers the gaps between.
  lastWork = Date.now();          // the stream is open; the loop starts next
  stage('detecting');
  checkAim();                     // asked every start, until it is answered
  sendBeat();
  beatTimer = setInterval(sendBeat, 30000);
  requestAnimationFrame(loop);
}

function stop() {
  running = false;
  lastWork = 0;                   // stopped watching: stop claiming to be online
  if (beatTimer) { clearInterval(beatTimer); beatTimer = null; }
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  const cv = $('#ov'); cv.getContext('2d').clearRect(0, 0, cv.width, cv.height);
  $('#stage').classList.remove('live');
  $('#btnStart').classList.remove('hidden');
  $('#btnStop').classList.add('hidden');
  $('#pWake').className = 'pill'; $('#pWake').textContent = 'screen lock';
  net('', 'idle');
  say($('#watchMsg'), 'Stopped.');
}
$('#btnStart').onclick = start;
// 'cop' / 'gov' / 'not' are the verdict names review_api.verdict already uses,
// so the phone answer and a /rv answer are literally the same call.
$('#askCop').onclick = function () { answerNow('cop'); };
$('#askGov').onclick = function () { answerNow('gov'); };
$('#askNot').onclick = function () { answerNow('not'); };
$('#btnStop').onclick = stop;

async function batteryWatch() {
  if (!navigator.getBattery) { $('#pBatt').textContent = 'mains'; return; }
  const b = await navigator.getBattery();
  const upd = () => {
    const pct = Math.round(b.level * 100);
    $('#pBatt').textContent = `battery ${pct}%${b.charging ? ' ⚡' : ''}`;
    $('#pBatt').className = 'pill' + (b.charging || pct > 20 ? ' on' : ' warn');
    // Stop rather than flatten someone's device. A node that killed the
    // machine it runs on does not get run twice.
    if (running && !b.charging && pct <= 20) {
      stop(); say($('#watchMsg'), 'Stopped at 20% battery. Plug in and start again.', false);
    }
  };
  b.addEventListener('levelchange', upd);
  b.addEventListener('chargingchange', upd);
  upd();
}

// A hidden window is a camera that sees nothing. Say so rather than letting
// someone believe their node is running while it is minimised.
document.addEventListener('visibilitychange', async () => {
  if (document.hidden && running) {
    say($('#watchMsg'), 'This window is hidden — the camera is not watching.', false);
  } else if (running) {
    say($('#watchMsg'), 'Watching.', true);
    if (!wakeLock) { try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {} }
  } else if (!running && mode === 'watch') {
    $('#stage').classList.remove('live');     // whatever is on screen is old
  }
});

/* Log-by-hand was REMOVED deliberately. It let a person type any plate,
 * tick the evidence signals themselves, and submit at plate_conf 0.95
 * attributed to a real camera at a real position. Since a confirmed public
 * sighting is what makes a plate searchable, that path could publish an
 * arbitrary plate about an arbitrary vehicle. The camera decides what it
 * sees; a person only reviews it afterwards. */

/* ---- 4 · key --------------------------------------------------------- */
async function loadQR() {
  if (!node) return;
  const r = await fetch('/api/key/qr', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    // The token goes in the BODY. A key in a query string is written to every
    // proxy log, kept in history, and leaked in the Referer of any link.
    body: JSON.stringify({ node_id: node.id, token: node.token, origin: location.origin }),
  });
  if (!r.ok) return say($('#kMsg'), 'Could not build the key.', false);
  $('#qr').src = URL.createObjectURL(await r.blob());
}

$('#kFile').onclick = () => {
  const url = `${location.origin}/app#k=${node.id}.${node.token}`;
  const html = `<!doctype html><meta charset="utf-8"><title>SparrowMap camera key</title>
<body style="background:#0a0d12;color:#e6ecf5;font:15px system-ui;padding:40px;max-width:520px;margin:0 auto">
<h2>SparrowMap camera key</h2>
<p>This file is the key to one camera. Opening it gives this device control.</p>
<p><a style="color:#4da3ff;font-size:18px" href="${url}">Open the camera &rarr;</a></p>
<p style="color:#8794a8;font-size:13px">Anyone who has this file has the camera.
Keep it like a house key. If it gets out, rotate the key and this file stops working.</p>
</body>`;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
  a.download = `sparrow-camera-key-${node.id}.html`;
  a.click();
  say($('#kMsg'), 'Saved. Copy it to a USB stick and keep it safe.', true);
};

$('#kPrint').onclick = () => window.print();

$('#kRotate').onclick = async () => {
  if (!confirm('Rotate the key?\n\nEvery copy of the current key stops working '
             + 'immediately, including any you printed or shared.')) return;
  const r = await fetch('/api/key/rotate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_id: node.id, token: node.token }),
  }).then((x) => x.json());
  if (r.error) return say($('#kMsg'), r.error, false);
  node.token = r.token;
  localStorage.setItem(KEY, JSON.stringify(node));
  await loadQR();
  say($('#kMsg'), 'Rotated. The old key is dead — save this new one.', true);
};

/* ---- take this camera off the map (and back) -------------------------
 * Bug report 015: an owner could not remove their own camera. The key is the
 * proof (Authorization header, as /api/node/whoami); the hub keeps a record,
 * so "Put it back" is the same key and one tap. */
function showRemoved(removed) {
  node.removed = !!removed;
  localStorage.setItem(KEY, JSON.stringify(node));
  $('#kRemove').classList.toggle('hidden', removed);
  $('#kRestore').classList.toggle('hidden', !removed);
  $('#rmNote').textContent = removed
    ? 'This camera is off the public map and is not sending. Its past sightings stay. Put it back any time with this key.'
    : 'It disappears from the public map and stops sending. Nothing is deleted: its past sightings of police vehicles stay, and you can put it back here with this key.';
}
async function nodeCall(path) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + node.token },
    body: JSON.stringify({ node_id: node.id }),
  });
  let d = {};
  try { d = await r.json(); } catch (e) { /* the status says enough */ }
  return { ok: r.ok, d };
}
async function removalState() {
  if (!(node && node.token)) return;
  try {
    const { ok, d } = await nodeCall('/api/node/whoami');
    if (ok) {
      showRemoved(d.status === 'removed');
      // Removed from another device: open where the way back is, not on a Watch that cannot send.
      if (d.status === 'removed' && mode === 'watch' && !running) go('key');
    }
  } catch (e) { /* offline: keep what we knew */ }
}
$('#kRemove').onclick = async () => {
  if (!confirm('Remove this camera from the map?\n\nIt disappears from the public map '
             + 'and stops sending. Nothing is deleted, and you can put it back '
             + 'from this page with the same key.')) return;
  if (running) stop();
  const { ok, d } = await nodeCall('/api/node/remove');
  if (!ok) return say($('#rmMsg'), d.error || 'Could not remove it. Try again.', false);
  showRemoved(true);
  say($('#rmMsg'), 'Removed. It leaves the public map within a minute.', true);
};
$('#kRestore').onclick = async () => {
  const { ok, d } = await nodeCall('/api/node/restore');
  if (!ok) return say($('#rmMsg'), d.error || 'Could not put it back. Try again.', false);
  showRemoved(false);
  say($('#rmMsg'), 'Put back. It returns to the map within a minute; open Watch to start sending again.', true);
};

/* ---- boot ------------------------------------------------------------ */
refreshChrome();
batteryWatch();
ensureReviewToken();
listCameras();
removalState();   // removed from another device? this one should know before it tries to watch
/* Offer the way back BEFORE offering a second camera. See #already-have in
   app.html for why this is the sentence that matters most on that screen. */
if (!(node && node.token)) {
  const back = document.getElementById('already-have');
  if (back) back.classList.remove('hidden');
}
go(node && node.token ? (node.removed ? 'key' : 'watch') : 'setup');
// Back from a self-update (maybeUpdate): carry on watching, as before the reload.
try {
  if (sessionStorage.getItem('sparrow.resume') === '1') {
    sessionStorage.removeItem('sparrow.resume');
    if (node && node.token) start();
  }
} catch (e) { /* no sessionStorage: nothing was saved, nothing to resume */ }

/* 🚨 ASK THE BROWSER TO KEEP THE KEY, BECAUSE LOSING IT IS THE #1 REPORTED BUG.
 *
 * "My camera got deleted" and "I am not signed in" are the same event, and
 * nothing was ever deleted: this file keeps the only credential in
 * localStorage, and localStorage is evictable. Safari clears script-writable
 * storage after 7 DAYS for a site that is not installed to the home screen, so
 * a volunteer who sets up a camera and comes back next week is logged out by
 * design - and the line above then drops them into SETUP, which enrols a
 * SECOND camera and orphans the first. Measured on the live box: 160 of 262
 * nodes have zero sightings and zero heartbeats, and one street is enrolled six
 * times.
 *
 * persist() marks the origin as one the browser must not evict. It is not
 * granted everywhere - Chrome weighs engagement and installation, Safari ties
 * it to being added to the home screen - so it is an improvement to the odds
 * and not a guarantee, which is why /signin exists as the way back regardless.
 * Requested only once a real key is held: asking a passer-by to be remembered
 * for ever is a permission prompt for nothing. */
if (node && node.token && navigator.storage && navigator.storage.persist) {
  navigator.storage.persisted().then((already) => {
    if (!already) navigator.storage.persist().catch(() => {});
  }).catch(() => {});
}

/* Register the service worker: it makes this page INSTALLABLE (a browser offers
   "Install" only for a page backed by a worker, over HTTPS) and lets a node run
   through a network blip. Only available in a secure context (HTTPS/localhost),
   so `serviceWorker in navigator` is false on a plain-http LAN box - the guard
   skips it there rather than erroring. */
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

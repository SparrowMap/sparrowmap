/* Landing-page behaviour, kept OUT of the HTML so the deployed Cloudflare Pages
   site can ship a strict Content-Security-Policy (script-src 'self') via
   landing/_headers. Cloudflare Pages cannot add a per-response nonce, and an
   inline <script> would force either 'unsafe-inline' (which defeats the point)
   or a brittle hash that silently breaks the page the next time a byte changes.
   External + 'self' is the robust choice. */


/* ---- call-to-action buttons --------------------------------------------
   Fill either value in and its button appears; leave it empty and the button
   is simply not rendered, so the page never ships a dead link. */
// No canonical public RavenMap host is configured yet. Keep these empty rather
// than sending visitors to the upstream SparrowMap deployment.
const APP_URL = "";
const MAP_URL = "";
const GITHUB_URL = "https://github.com/SparrowMap/sparrowmap";
const INSTAGRAM_URL = "https://instagram.com/sparrowmap";
const CONTACT_EMAIL = "sparrowmap@icloud.com";

(function () {
  const host = document.getElementById("actions");
  if (!host) return;
  const out = [];
  const link = (cls, href, text) => {
    const a = document.createElement("a");
    a.className = cls; a.href = href; a.rel = "noopener"; a.textContent = text;
    out.push(a);
  };
  if (APP_URL) link("btn", APP_URL, "Add a camera");
  if (MAP_URL) link("btn ghost", MAP_URL, "View the live map");
  if (GITHUB_URL) link("btn ghost", GITHUB_URL, "Read the upstream code");
  if (INSTAGRAM_URL) link("btn ghost", INSTAGRAM_URL, "Upstream Instagram");
  if (CONTACT_EMAIL) {
    const a = document.createElement("a");
    a.className = "btn ghost";
    a.href = "mailto:" + CONTACT_EMAIL + "?subject=I%20want%20to%20run%20a%20camera";
    a.textContent = "Ask about running a camera";
    out.push(a);
  }
  if (!out.length) {
    const p = document.createElement("p");
    p.style.cssText = "font-family:var(--mono);font-size:13px;color:var(--steel);margin:0";
    p.textContent = "Opening to volunteers soon.";
    out.push(p);
  }
  host.replaceChildren(...out);
})();

/* First-visit nudge to add a camera - people were not finding the button. Shown
   once per browser, dismissible, built with DOM calls (style-src allows inline
   styles here; there is still no inline <script>). */
(function () {
  try { if (localStorage.getItem("sparrow.introSeen")) return; } catch (e) { return; }
  const mk = (tag, text, css) => {
    const el = document.createElement(tag);
    if (text) el.textContent = text;
    if (css) el.style.cssText = css;
    return el;
  };
  const ov = mk("div", "", "position:fixed;inset:0;z-index:3000;display:flex;"
    + "align-items:center;justify-content:center;background:rgba(0,0,0,.6);padding:20px");
  const card = mk("div", "", "max-width:380px;width:100%;background:#0d1219;"
    + "border:1px solid #22303c;border-radius:16px;padding:26px;text-align:center;"
    + "color:#c7d2dc;font:15px/1.55 system-ui,sans-serif;box-shadow:0 24px 70px rgba(0,0,0,.6)");
  const h = mk("div", "Watch the watchers",
    "font-size:21px;font-weight:700;color:#fff;margin-bottom:10px");
  // ⚠️ "PRIVATE plates", NOT "plates" - kept in step with public/app.js. A
  // government plate is deliberately kept readable and searchable.
  const p = mk("div", "RavenMap runs on volunteer cameras. Point a spare phone "
    + "at a street and it maps the patrols that pass. Private plates are "
    + "destroyed on the device and never uploaded.", "color:#93a3b3;margin-bottom:20px");
  const add = mk("a", APP_URL ? "Add a camera" : "Configure a hub to add a camera", "display:block;padding:14px;border-radius:11px;"
    + "background:#3b82f6;color:#fff;font-weight:600;text-decoration:none;margin-bottom:10px");
  if (APP_URL) { add.href = APP_URL; add.rel = "noopener"; }
  else { add.removeAttribute("href"); add.style.opacity = ".65"; }

  /* 🚨 THE SAME THREE ROUTES AS THE MAP'S CARD, AND FOR THE SAME REASON.
   * This card and public/app.js's showIntro() are two copies of one decision -
   * the comment above already says they must be kept in step, and they had
   * drifted: the map's now offers Driving mode and Sign in, and this one still
   * offered a single "Add a camera".
   *
   * Sign in matters MORE here, not less. A volunteer whose phone lost its
   * camera key often starts at the front door rather than the map, and this
   * page is on a different ORIGIN - so it cannot read their key, cannot tell
   * whether they have one, and cannot quietly put them right. All it can do is
   * offer the way back and say that nothing was deleted. */
  const row = mk("div", "", "display:flex;gap:8px;margin-bottom:10px");
  const SEC = "flex:1;display:block;padding:12px 8px;border-radius:11px;"
    + "background:#131c27;border:1px solid #22303c;color:#c7d2dc;font-weight:600;"
    + "font-size:12.5px;text-decoration:none;text-align:center;cursor:pointer;"
    + "white-space:nowrap;min-width:0";
  const destinations = [
    ["🚗 Driving", "/drive"],
    ["📷 IP Camera", "/IPCamera"],
    ["🔑 Sign in", "/signin"],
  ];
  destinations.forEach(([text, path]) => {
    if (!MAP_URL) return;
    const link = mk("a", text, SEC);
    link.href = MAP_URL + path; link.rel = "noopener";
    row.append(link);
  });

  const backNote = mk("div",
    "Set up a camera before? Nothing is deleted — sign in with your key rather "
    + "than adding it again.",
    "color:#6f8296;font-size:12px;line-height:1.5;margin-bottom:14px");

  const skip = mk("button", "Just browsing", "display:block;width:100%;padding:12px;"
    + "border-radius:11px;background:transparent;border:1px solid #22303c;color:#7f93a6;"
    + "cursor:pointer;font:inherit");
  const done = () => { try { localStorage.setItem("sparrow.introSeen", "1"); } catch (e) {} ov.remove(); };
  skip.addEventListener("click", done);
  // Every way out marks it seen, or the card returns on the next visit and
  // reads as the site having forgotten what was just chosen.
  [add, ...row.children].forEach((el) => el.addEventListener("click", done));
  ov.addEventListener("click", (e) => { if (e.target === ov) done(); });
  card.append(h, p, add, row, backNote, skip);
  ov.appendChild(card);
  document.body.appendChild(ov);
})();

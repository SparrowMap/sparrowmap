/**
 * sparrowmap.com/api/stats  -  the public counter endpoint.
 *
 * Twelve lines of logic and one job: hold the last aggregate counts the home
 * hub pushed, and serve them to the website. It exists so the static site can
 * show live numbers WITHOUT the operator's home IP address ever appearing in
 * front of a visitor, and without anything at home accepting inbound
 * connections.
 *
 *   POST /api/stats        Authorization: Bearer <STATS_TOKEN>   (the hub)
 *   GET  /api/stats.json                                          (visitors)
 *
 * 🚨 THE WRITE PATH IS THE WHOLE ATTACK SURFACE, SO IT IS NARROW ON PURPOSE.
 * A forged POST would put a false number on a page whose entire argument is
 * that its numbers are true. Therefore:
 *   - the token is compared in CONSTANT TIME. A plain === leaks the token one
 *     byte at a time to anyone who can measure response latency, and this
 *     endpoint is on the public internet by definition.
 *   - every field must be a non-negative integer, and unknown fields are
 *     dropped. The stored object is rebuilt from an allowlist, so the endpoint
 *     cannot be turned into a place to host arbitrary text.
 *   - private_plates_stored MUST be 0. That figure is the site's strongest
 *     claim; if a push ever says otherwise the correct response is to reject
 *     it and keep serving the last honest value, not to publish it.
 *
 * Requires a KV namespace bound as STATS and a secret STATS_TOKEN.
 */

const FIELDS = ["vehicles_seen", "sightings", "confirmed_public",
                "private_seen", "private_plates_stored", "cameras"];

// Configure the public site origin explicitly in the worker environment.
// RAVEN_STATS_ORIGIN is preferred; the legacy name remains compatible.
function allowedOrigin(env, request) {
  const configured = env.RAVEN_STATS_ORIGIN || env.SPARROW_STATS_ORIGIN || "";
  const requestOrigin = request.headers.get("Origin") || "";
  return configured && requestOrigin === configured ? configured : "";
}

function safeEqual(a, b) {
  // Constant time in the length-equal case; length is not secret.
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/api/stats") {
      const auth = request.headers.get("Authorization") || "";
      const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
      if (!env.STATS_TOKEN || !safeEqual(token, env.STATS_TOKEN)) {
        return new Response("no", {status: 401});
      }
      let body;
      try { body = await request.json(); }
      catch { return new Response("bad json", {status: 400}); }

      // A billion vehicles past one street is already absurd; cap well below
      // that so a valid-token but buggy/hostile push cannot store a number that
      // renders as garbage on the page. Counts this large are a broken hub, not
      // a reading.
      const MAX_COUNT = 1e9;
      const clean = {};
      for (const k of FIELDS) {
        const v = body[k];
        if (!Number.isInteger(v) || v < 0 || v > MAX_COUNT) {
          return new Response(`bad field ${k}`, {status: 400});
        }
        clean[k] = v;
      }
      // The promise, enforced at the edge as well as at the source.
      if (clean.private_plates_stored !== 0) {
        return new Response("refused: site claims 0 private plates", {status: 409});
      }
      // An all-zero push is a broken hub, not a reading. Rejecting it here means
      // the endpoint keeps serving the last honest value instead of storing a
      // row of zeros that the page would render as "nothing has ever happened".
      if (clean.vehicles_seen === 0 || clean.sightings === 0) {
        return new Response("refused: zero traffic is a failure, not a count",
                            {status: 409});
      }
      // `as_of` is the only stored string. The page writes it via textContent
      // (safe), but defence in depth: strip it to a date/time charset so it can
      // never carry markup or control characters to any future consumer that is
      // less careful. Whitespace-only after stripping => drop it.
      if (typeof body.as_of === "string") {
        const cleaned = body.as_of.replace(/[^0-9A-Za-z ,:.\-]/g, "").slice(0, 40).trim();
        if (cleaned) clean.as_of = cleaned;
      }
      await env.STATS.put("current", JSON.stringify(clean));
      return new Response("ok");
    }

    if (request.method === "GET" &&
        (url.pathname === "/api/stats.json" || url.pathname === "/api/stats")) {
      const cur = await env.STATS.get("current");
      // 404 rather than an empty object: the page treats a failed fetch as
      // "keep the honest snapshot in the markup", and an empty payload would
      // instead risk rendering zeros beside sentences that claim otherwise.
      if (!cur) return new Response("no data yet", {status: 404});
      const corsOrigin = allowedOrigin(env, request);
      return new Response(cur, {
        headers: {
          "Content-Type": "application/json",
          // Short cache: the figures move slowly and a visitor spike should not
          // become a KV read per visitor.
          "Cache-Control": "public, max-age=60",
          ...(corsOrigin ? {"Access-Control-Allow-Origin": corsOrigin} : {}),
        },
      });
    }

    return new Response("not found", {status: 404});
  },
};

/* The transparency panel: live policy and counters.
 *
 * The decisions log was deliberately removed - see transparency.html.
 *
 * Extracted from transparency.html so the standalone page and the unified
 * shell on the map render it from ONE implementation. This codebase has been
 * bitten repeatedly by the other arrangement - is_operator_addr and the
 * government-vehicle call both had to be collapsed into single functions after
 * a rule was fixed in one copy and not the other.
 *
 * Exposed as window.sparrowTransparency() so the shell can call it again after
 * injecting the markup, because innerHTML does not run scripts.
 */
window.sparrowTransparency = async function () {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const LABELS = {
    public_tiers:            ['Vehicle classes published with a readable plate', (v) => v.join(', ')],
    civilian_retention_days: ['Private sightings deleted after', (v) => `${v} days`],
    public_retention_days:   ['Public sightings deleted after', (v) => v ? `${v} days` : 'never (public record)'],
    pepper_rotation_days:    ['Hash key rotated every', (v) => `${v} days`],
    /* 🚨 THIS LINE BECAME UNTRUE THE DAY PUBLIC TRAFFIC CAMERAS WERE ADDED.
     * It said, flatly, that camera positions are published only to within the
     * jitter. That is still exactly right for every VOLUNTEER camera - the
     * whole reason the jitter exists is that a volunteer's camera position
     * describes their house - and it is now wrong as a blanket statement,
     * because government traffic cameras publish their own exact coordinates
     * and SparrowMap passes them straight through.
     *
     * On the one page whose entire purpose is that its claims can be checked,
     * a sentence that is 95% true is worse than a longer one that is true. */
    node_position_jitter_m:  ['Volunteer camera positions published to within',
                              (v) => `${v} m`],
    min_plate_confidence:    ['Minimum plate confidence to record', (v) => v],
    public_threshold:        ['Confidence needed to publish a plate', (v) => v],
    private_plate_lookup:    ['Can anyone look up a private plate?', (v) => v ? 'YES' : 'no'],
    stores_video:            ['Does any video reach this server?', (v) => v ? 'YES' : 'no'],
    stores_full_frames:      ['Are full camera frames stored?', (v) => v ? 'YES' : 'no (vehicle crops only)'],
    /* 🚨 THE ROUTING PROMISE, ON THE PAGE WHOSE JOB IS THAT CLAIMS CAN BE
     * CHECKED. Driving mode computes turn-by-turn directions, which means a
     * destination reaches this server - the most revealing thing a person can
     * hand a map, because it is where they are going before they have gone.
     * Two facts decide whether that is acceptable, so both are published as
     * values rather than described in prose: nothing about the journey is
     * written down, and no third party is involved. They can be diffed against
     * deploy/valhalla.service (which starts the engine with its own logging
     * off, on loopback) and the Caddyfile (access log `output discard`). */
    route_logging:           ['Is your destination logged when you navigate?', (v) => v ? 'YES' : 'no'],
    route_third_party:       ['Does your destination reach anyone else?', (v) => v ? 'YES' : 'no'],
    routing_engine:          ['Directions are computed by', (v) => v],
  };

  // body
    // The decisions log is gone (see transparency.html). /api/audit is no
    // longer fetched at all: it 404s on a mirror, and a request that can only
    // fail is not worth making on every page view.
    const [p, s] = await Promise.all([
      fetch('/api/policy').then(r => r.json()),
      fetch('/api/stats').then(r => r.json()),
    ]);

    $('#cards').innerHTML = [
      [s.nodes_online + ' / ' + s.nodes_active, 'cameras online'],
      [s.sightings_24h.toLocaleString(), 'sightings, 24h'],
      [s.public_24h.toLocaleString(), 'public sightings, 24h'],
      [(s.sightings_24h - s.public_24h).toLocaleString(), 'private passes, no plate kept'],
      [s.vehicles_24h.toLocaleString(), 'distinct vehicles'],
    ].map(([b, t]) => `<div class="card"><b>${b}</b><span>${t}</span></div>`).join('');

    /* Sources, stated plainly. Somebody reading this page is deciding whether
     * to believe the map, and "where do these sightings come from" is the
     * first thing they need - especially now that not all of them come from
     * volunteers. Counted live rather than asserted, so it cannot drift. */
    try {
      const nodes = await fetch('/api/nodes').then(r => r.json());
      const pub = nodes.filter(n => n.kind === 'public_cam');
      const note = $('#sources');
      if (note) {
        note.innerHTML = pub.length
          ? `<b>${pub.length}</b> of the cameras feeding this map are <b>public `
            + `government traffic cameras</b>, read from feeds their transport `
            + `authority publishes. They are marked as such on the map and in `
            + `the API (<code>kind=public_cam</code>), and their exact position `
            + `is shown because the authority publishes it. Every other camera `
            + `is a volunteer's, and its position is never published.`
          : `Every camera feeding this map is a volunteer's, and no camera `
            + `position is published.`;
      }

      /* 🚨 ATTRIBUTION IS A LICENCE CONDITION, NOT A COURTESY, AND IT WAS
       * MISSING. Fintraffic and Iowa DOT both publish under CC BY 4.0 and
       * Ontario under the Open Government Licence - every one of which
       * requires naming the source. SparrowMap has been republishing crops
       * from all three with no credit anywhere on the site.
       *
       * That is worse here than it would be elsewhere. This project's entire
       * argument is that you can check what it says about itself; being
       * casual with somebody else's licence while demanding accountability
       * from police departments is not a position that survives contact with
       * anyone who looks.
       *
       * COUNTED FROM THE NODES, NOT ASSERTED. The source tag is in each node's
       * name as "[fi:C0150301]", so a network that stops feeding the map stops
       * being credited, and one that starts cannot be forgotten. */
      const credit = $('#camsources');
      if (credit) {
        /* Every source the poller can run, so none is ever shown as a bare
         * code. A licence is NAMED only where it was checked and found (the
         * open-data ones); everything else says 'public feed' rather than
         * claiming a licence nobody verified. [name, site, licence, licence URL] */
        const SRC = {
          fi:  ['Fintraffic', 'https://www.fintraffic.fi/en', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0/'],
          ia:  ['Iowa DOT', 'https://data.iowadot.gov/', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0/'],
          atx: ['City of Austin', 'https://data.austintexas.gov/', 'public domain'],
          on:  ['Ontario 511 (MTO)', 'https://511on.ca/', 'Open Government Licence – Ontario', 'https://www.ontario.ca/page/open-government-licence-ontario'],
          nyc: ['NYC DOT', 'https://webcams.nyctmc.org/', 'public feed'],
          oh:  ['Ohio DOT (OHGO)', 'https://www.ohgo.com/', 'public feed'],
          ny:  ['511NY (NYSDOT)', 'https://511ny.org/', 'public feed'],
          nm:  ['New Mexico DOT (NMRoads)', 'https://nmroads.com/', 'public feed'],
          mo:  ['Missouri DOT', 'https://traveler.modot.org/', 'public feed'],
          mi:  ['Michigan DOT (Mi Drive)', 'https://mdotjboss.state.mi.us/MiDrive/', 'public feed'],
          in:  ['Indiana DOT (TrafficWise)', 'https://pws.trafficwise.org/', 'public feed'],
          al:  ['ALDOT (ALGO Traffic)', 'https://algotraffic.com/', 'public feed'],
          aldot: ['Alabama DOT', 'https://www.dot.state.al.us/', 'public feed'],
          ne_511: ['New England 511 (VT / NH / ME)', 'https://www.newengland511.org/', 'public feed'],
          nc:  ['NCDOT (DriveNC)', 'https://www.drivenc.gov/', 'public feed'],
          il:  ['Travel Midwest (Illinois)', 'https://www.travelmidwest.com/', 'public feed'],
          sd:  ['South Dakota DOT', 'https://www.sd511.org/', 'public feed'],
          az:  ['Arizona DOT (AZ511)', 'https://www.az511.gov/', 'public feed'],
          ut:  ['Utah DOT', 'https://www.udottraffic.utah.gov/', 'public feed'],
          id:  ['Idaho 511 (ITD)', 'https://511.idaho.gov/', 'public feed'],
          tx:  ['Texas DOT', 'https://its.txdot.gov/', 'public feed'],
          ne:  ['Nebraska DOT', 'https://511.nebraska.gov/', 'public feed'],
          ks:  ['Kansas DOT (KanDrive)', 'https://www.kandrive.gov/', 'public feed'],
          mn:  ['Minnesota DOT', 'https://511mn.org/', 'public feed'],
          co:  ['Colorado DOT', 'https://www.cotrip.org/', 'public feed'],
          kytc: ['Kentucky Transportation Cabinet', 'https://goky.ky.gov/', 'public feed'],
          sea: ['Seattle DOT', 'https://web.seattle.gov/travelers/', 'public feed'],
          wsd: ['WSDOT', 'https://wsdot.com/travel/real-time/', 'public feed'],
          or:  ['WSDOT travel information', 'https://wsdot.com/travel/real-time/', 'public feed'],
          kc:  ['King County, WA', 'https://kingcounty.gov/', 'public feed'],
          kirk: ['City of Kirkland, WA', 'https://www.kirklandwa.gov/', 'public feed'],
          tor: ['City of Toronto', 'https://www.toronto.ca/', 'public feed'],
          nl:  ['Newfoundland and Labrador', 'https://www.gov.nl.ca/', 'public feed'],
          fl:  ['FL511 (Florida DOT)', 'https://fl511.com/', 'public feed'],
          ga:  ['Georgia DOT', 'https://511ga.org/', 'public feed'],
          ca:  ['Caltrans', 'https://cwwp2.dot.ca.gov/', 'public feed'],
          es_mad: ['Ayuntamiento de Madrid', 'https://datos.madrid.es/', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0/'],
          es_dgt: ['Dirección General de Tráfico (DGT)', 'https://nap.dgt.es/', 'CC BY', 'https://nap.dgt.es/en/dataset/camaras-dgt-datex2-v3-7'],
          no:  ['Statens vegvesen', 'https://www.vegvesen.no/trafikk/', 'NLOD 2.0', 'https://data.norge.no/nlod/en/2.0'],
          ee:  ['Transpordiamet (Tark Tee)', 'https://tarktee.ee/', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0/'],
          fr_lyon: ['Métropole de Lyon', 'https://data.grandlyon.com/', 'Licence Ouverte 2.0', 'https://www.etalab.gouv.fr/licence-ouverte-open-licence/'],
          sg:  ['Traffic Images from data.gov.sg (LTA)', 'https://data.gov.sg/', 'Singapore Open Data Licence 1.0', 'https://data.gov.sg/open-data-licence'],
          mnt: ['Manatee County, FL', 'https://www.mymanatee.org/', 'public feed'],
          kcs: ['KC Scout (MoDOT / KDOT)', 'https://www.kcscout.net/', 'public feed'],
        };
        const seen = {};
        pub.forEach((n) => {
          // ⚠️ [a-z0-9_]: source ids like ne_511 and es_mad have digits and
          // underscores, and [a-z]+ silently credited none of them.
          const m = /\[([a-z0-9_]+):[^\]]*\]$/.exec(n.name || '');
          if (m) seen[m[1]] = (seen[m[1]] || 0) + 1;
        });
        const rows = Object.entries(seen)
          .sort((a, b) => b[1] - a[1])
          .map(([k, count]) => {
            const [name, url, lic, licUrl] = SRC[k] || [k, '', 'see the source'];
            const licHtml = licUrl
              ? `<a href="${esc(licUrl)}" rel="noopener noreferrer" target="_blank">${esc(lic)}</a>`
              : esc(lic);
            return `<li>${count.toLocaleString()} from <a href="${esc(url)}" `
                 + `rel="noopener noreferrer" target="_blank">${esc(name)}</a> `
                 + `— ${licHtml}</li>`;
          });
        credit.innerHTML = rows.length
          ? `<p>Those images come from these networks, used under their own `
            + `terms:</p><ul>${rows.join('')}</ul>`
          : '';
      }
    } catch (e) { /* the table below still stands on its own */ }

    $('#policy').innerHTML = '<tr><th>Setting</th><th>Value</th></tr>' +
      Object.entries(LABELS).map(([k, [lab, fmt]]) => {
        if (p[k] === undefined) return '';
        const val = fmt(p[k]);
        const cls = (k === 'private_plate_lookup' || k === 'stores_video' || k === 'stores_full_frames'
                     || k === 'route_logging' || k === 'route_third_party')
          ? (String(val).startsWith('no') ? 'yes' : 'no') : '';
        return `<tr><td class="n">${esc(lab)}</td><td class="v ${cls}">${esc(val)}</td></tr>`;
      }).join('');

};

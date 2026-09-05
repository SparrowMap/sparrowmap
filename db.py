"""SparrowMap - storage.

SQLite, because a citizen network should be runnable by one person on a laptop
in a spare room, and because the whole database being a single file makes
"delete everything" an operation anybody can verify.

Note what the schema does NOT have: a column for readable civilian plate text.
That is not an oversight the ingest path fills in later. The column does not
exist for private-tier rows because a field that cannot be populated cannot be
leaked.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Optional

from core import CONFIG, DB_PATH, now

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

-- A volunteer camera.
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    pubkey        TEXT,               -- ed25519, base64. Identity of the node.
    token         TEXT,               -- bearer secret for browser nodes that
                                      -- cannot hold an ed25519 key. Weaker than
                                      -- a signature; only ever issued to
                                      -- kind='phone'.
    lat           REAL, lon REAL,     -- true position, operator-only
    pub_lat       REAL, pub_lon REAL, -- jittered position shown publicly
    heading       REAL DEFAULT 0,     -- compass bearing the lens faces
    fov           REAL DEFAULT 60,    -- horizontal field of view, degrees
    reach_m       REAL DEFAULT 45,    -- usable plate-read distance, metres
    kind          TEXT DEFAULT 'fixed',   -- fixed | mobile | phone
    status        TEXT DEFAULT 'active',  -- active | paused | revoked
    contact       TEXT,               -- operator-only, never served publicly
    created       REAL,
    last_seen     REAL,               -- last SIGHTING. Traffic, not liveness.
    last_beat     REAL,               -- last heartbeat. This is what 'online'
                                      -- means: the node said so. Deriving it
                                      -- from last_seen made a camera watching
                                      -- an empty street look switched off.
    -- The stretch of real road this camera covers, published accurately
    -- because it is public information, unlike the camera's own position.
    -- See road.py for why these two are separated.
    span_lat1     REAL, span_lon1 REAL,
    span_lat2     REAL, span_lon2 REAL,
    road_name     TEXT,
    span_source   TEXT,               -- osm | aim | none
    sightings     INTEGER DEFAULT 0
);

-- One vehicle, seen once, by one camera.
CREATE TABLE IF NOT EXISTS sightings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       TEXT NOT NULL,
    ts            REAL NOT NULL,
    lat           REAL, lon REAL,
    tier          TEXT NOT NULL,      -- public | private
    plate_hash    TEXT,               -- always present when a plate was read
    plate_text    TEXT,               -- populated ONLY when tier = 'public'
    plate_state   TEXT,
    plate_conf    REAL,
    vclass        TEXT,               -- police | gov | fleet | civilian | unknown
    vclass_conf   REAL,
    vclass_why    TEXT,               -- human-readable evidence for the class
    color         TEXT, body TEXT, make TEXT, model TEXT,
    heading       REAL,               -- direction of travel
    speed_mph     REAL,
    snap          TEXT,               -- filename under data/snaps
    source        TEXT,               -- synthetic | camera | phone
    sig_ok        INTEGER DEFAULT 0,  -- did the node's signature verify
    confirmed_by  INTEGER DEFAULT 0   -- humans who confirmed the vehicle class
);
CREATE INDEX IF NOT EXISTS ix_sight_ts     ON sightings(ts DESC);
CREATE INDEX IF NOT EXISTS ix_sight_hash   ON sightings(plate_hash, ts DESC);
CREATE INDEX IF NOT EXISTS ix_sight_class  ON sightings(vclass, ts DESC);
CREATE INDEX IF NOT EXISTS ix_sight_plate  ON sightings(plate_text, ts DESC);
CREATE INDEX IF NOT EXISTS ix_sight_node   ON sightings(node_id, ts DESC);

-- Every read of the public tier. We are building a surveillance network; the
-- least we can do is be the first thing it watches.
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL, action TEXT, target TEXT, actor TEXT, ip TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts DESC);

-- Out-of-band proof that a person owns a plate. Unused until a real
-- verification channel exists. See privacy.lookup_private_plate.
CREATE TABLE IF NOT EXISTS claims (
    plate_hash TEXT PRIMARY KEY,
    token      TEXT, created REAL, verified_by TEXT, note TEXT
);

-- Free-form community annotation on a public-tier vehicle: "unmarked unit",
-- "this is the county sheriff's K9 truck". Kept separate from detections so a
-- claim can never be mistaken for a measurement.
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_hash TEXT, ts REAL, body TEXT, author TEXT, votes INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_notes_hash ON notes(plate_hash);

-- A member of the public flagging a published sighting as wrong: not a
-- government vehicle, wrong description, and so on. A flag is a SUGGESTION, not
-- an edit. It surfaces the sighting in the operator's review queue, where a
-- human makes the actual call. That keeps the map correctable by anyone without
-- letting anyone quietly take a real patrol car off it.
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sighting_id INTEGER, ts REAL, reason TEXT, note TEXT, ip TEXT,
    resolved    REAL
);
CREATE INDEX IF NOT EXISTS ix_reports_sid ON reports(sighting_id);

-- Reviewer access, separate from the single operator secret. Each trusted
-- reviewer gets their own token (stored only as a hash), which can be scoped to
-- the shared pool or to specific cameras they own, and revoked without touching
-- anyone else's. Every verdict they cast is written to `audit` with their
-- label, so trust is the default and abuse is recoverable rather than assumed.
CREATE TABLE IF NOT EXISTS review_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT UNIQUE,
    label        TEXT,
    scope        TEXT,          -- 'pool' = all pending, 'own' = only `nodes`
    nodes        TEXT,          -- comma-separated node_ids for scope='own'
    created_at   REAL,
    created_by   TEXT,
    revoked_at   REAL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS ix_rvtok_hash ON review_tokens(token_hash);

-- Live, ephemeral "a patrol is here right now" reports from drivers in driving
-- mode. Deliberately SEPARATE from sightings: these are crowd signals, not the
-- verified record - unphotographed, unreviewed, and they EXPIRE. A driver's own
-- route is never stored; only the point they tapped, which is about the patrol,
-- not them. Waze-for-patrols, and just as disposable.
CREATE TABLE IF NOT EXISTS driver_reports (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL, lat REAL, lon REAL,
    kind     TEXT,                   -- 'police' for now; room for more later
    confirms INTEGER DEFAULT 1,
    denies   INTEGER DEFAULT 0,
    expires  REAL
);
CREATE INDEX IF NOT EXISTS ix_driver_reports_exp ON driver_reports(expires);

-- 🚨 SIGNAL PARTNERS: OUTSIDE SENSORS THAT REPORT WHAT A VEHICLE BROADCASTS.
--
-- A partner runs their own sensors (sub-GHz, LoRaWAN, whatever) and tells us
-- "at this time and place I heard signature X". They do NOT tell us what the
-- vehicle was, and they cannot write to `sightings` at all. Correlating an
-- observation with a published government vehicle is OUR job, on OUR data,
-- and every result of it is INFERRED - see db.tag_sighting, which refuses to
-- link anything that is not already public tier.
--
-- Why a separate token table rather than reusing review_tokens: these grant a
-- completely different power (write an observation) and must be revocable on
-- their own. Mixing them would mean revoking a partner also revoked a
-- reviewer, or worse, that one leaked credential did both.
CREATE TABLE IF NOT EXISTS signal_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT UNIQUE,
    label        TEXT,           -- who this belongs to, in words
    created_at   REAL,
    created_by   TEXT,
    revoked_at   REAL,
    last_used_at REAL,
    posted       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sigtok_hash ON signal_tokens(token_hash);

-- One observation. Deliberately dumb: a time, a place, a band, an opaque
-- signature string. No vehicle id, no plate, no claim about who or what.
CREATE TABLE IF NOT EXISTS signal_obs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    partner    INTEGER,          -- signal_tokens.id
    ts         REAL,             -- when the partner heard it
    lat        REAL, lon REAL,
    band       TEXT,             -- 'sub-ghz', 'lorawan', 'gnss', ...
    signature  TEXT,             -- opaque; whatever fingerprint they compute
    conf       REAL,
    note       TEXT,
    received   REAL              -- when WE got it
);
CREATE INDEX IF NOT EXISTS ix_sigobs_ts ON signal_obs(ts);
CREATE INDEX IF NOT EXISTS ix_sigobs_sig ON signal_obs(signature);
"""


# Columns added after the first deployment. CREATE TABLE IF NOT EXISTS is a
# no-op on an existing table, so a new column in SCHEMA above never reaches a
# database that already exists - which is a silent failure of exactly the kind
# this project keeps catching itself in. Adding them here means the schema and
# a live database cannot drift apart.
MIGRATIONS = [
    ("nodes", "last_beat", "REAL"),
    # The town this camera is in, for the zoomed-out map.
    #
    # 🚨 A TOWN NAME IS A DELIBERATELY COARSE THING TO PUBLISH, AND THAT IS THE
    # POINT. Zoomed out to a whole state, thirty 80 m road bands are sub-pixel
    # smears that read as markers - the exact "a thing is at this spot"
    # impression the corridor shape exists to avoid. "Brighton, 3 cameras" says
    # what is actually known at that zoom and nothing more.
    #
    # It is also LESS revealing than what is already published: the watched span
    # is a specific stretch of a specific street, and the town containing it is
    # strictly coarser. This adds no exposure.
    #
    # ⚠️ Resolved from the JITTERED position, never the true one (see
    # tools/backfill_places.py). A town lookup would survive the jitter anyway,
    # but the rule that a camera's real coordinates are not sent to a third
    # party has no exceptions worth making.
    ("nodes", "place", "TEXT"),
    # 🚨 WHO put this on the public map: 'human' or 'auto'.
    #
    # `reviewed='confirmed'` was being read as "a person looked at this", and it
    # is not: promote_sighting stamps it, and promote_sighting is called both by
    # a reviewer in /rv AND by the contributor auto-publish gate. The flag
    # records that a DECISION happened, never who made it - so a count of
    # human-reviewed publications was impossible to compute, and the only way
    # to tell them apart was parsing the reason string.
    #
    # A transparency project has to be able to prove "nothing reaches the public
    # tier without a person", not merely assert it. That needs a column.
    ("sightings", "decided_by", "TEXT"),
    # How many times this camera has said "still watching". last_beat answers
    # "is it up now"; this answers "how much watching has this network done",
    # which is the number that grows and is worth showing. Heartbeats were not
    # always enabled, so a node with none is not evidence it never ran - which
    # is exactly why this is a TOTAL and never a per-node quality signal.
    ("nodes", "beats", "INTEGER"),
    # Operator review of a PUBLISHED claim. NULL = nobody has looked.
    # 'confirmed' / 'retracted' - see hub /review. A public sighting is an
    # assertion about a vehicle, so there has to be a way to take one back,
    # and the taking-back is the most valuable training label in the project:
    # a human judgement on the camera's own view of a real published claim.
    ("sightings", "reviewed", "TEXT"),
    ("sightings", "reviewed_at", "REAL"),
    # Which banked training crop came from this sighting, so a correction on
    # the map writes a label into the dataset instead of dying as a one-off fix.
    ("sightings", "bank_ref", "TEXT"),
    # How many separate detections were folded into this one pass. A vehicle
    # crossing the frame can break into several tracker tracks, and each used
    # to post its own sighting - one patrol car, three dots on the map. See
    # merge_window_row.
    ("sightings", "detections", "INTEGER"),
    ("nodes", "span_lat1", "REAL"),
    ("nodes", "span_lon1", "REAL"),
    ("nodes", "span_lat2", "REAL"),
    ("nodes", "span_lon2", "REAL"),
    ("nodes", "road_name", "TEXT"),
    ("nodes", "span_source", "TEXT"),
    # 🚨 HOW FAR SETUP GOT. 20 of the first 36 cameras registered and never
    # sent a single heartbeat, and "never started" was one undifferentiated
    # bucket - it could equally be a denied camera permission, a 38 MB model
    # download that timed out on mobile data, or someone who closed the tab.
    # Those need completely different fixes and the data could not tell them
    # apart, so the largest number in the project was unactionable.
    #
    # Deliberately three columns and no more. A stage name from a fixed list,
    # when it happened, and a four-way platform bucket. No user agent, no
    # screen size, no IP, nothing that identifies a device or a person - this
    # project's whole argument is that it collects the minimum, and setup
    # telemetry is exactly where that gets quietly abandoned.
    ("nodes", "setup_stage", "TEXT"),
    ("nodes", "setup_stage_ts", "REAL"),
    ("nodes", "setup_platform", "TEXT"),
    # The filename of a photograph that has been pulled off the map and moved
    # into core.HELD, pending a human decision. `snap` is NULL while this is
    # set, so nothing serves the picture, and this remembers what to crop or
    # restore from. Both being set at once would mean the file is in two places
    # and one of them is a lie, so hold_photo/fix_photo always swap the pair.
    ("sightings", "snap_held", "TEXT"),
    # When it was held, so the fix queue can show the oldest first - a held
    # photo is a sighting with its evidence missing, which is a debt.
    ("sightings", "held_at", "REAL"),
    # 🚨 DOES THIS CAMERA'S OWNER AGREE TO PUBLISH THE STRETCH OF ROAD IT
    # WATCHES? NULL/0 = no, and that is the deliberate default.
    #
    # The span was published for every camera unconditionally, on the argument
    # that "people are entitled to know where they are recorded" - which is
    # true, and which the owner was never asked about. An 80 m span on a NAMED
    # road is coarse in a town and close to an address in the country: on a
    # rural stretch where one house overlooks the road, the span plus the road
    # name is that house. The person who accepted that exposure is the one
    # volunteering the camera, and they were not consulted.
    #
    # Opt-IN rather than opt-out, because a default that publishes and waits to
    # be corrected has already published. See /api/node/span.
    ("nodes", "publish_span", "INTEGER"),
    # 🚨 THE NODE THIS ONE TURNED INTO WHEN THE CAMERA MOVED.
    #
    # A camera that moves more than MOVED_THRESHOLD_M is deliberately a NEW
    # node (see nodes.enroll): moving a device must not drag its history and
    # its watched road onto a street it never saw. That is right, and it leaves
    # the old row behind as a real record of a real period.
    #
    # What it does NOT leave behind is any way to tell that the old row is no
    # longer a camera. enroll() computes exactly this fact, hands it back as
    # `moved_from`, and nothing stored it - so the retired node kept beating
    # for as long as the device took to adopt its new id, and counted as a
    # CAMERA ONLINE the whole time. One physical camera, three entries, three
    # of them "online". Same shape as the ratio bug in the comment below: a
    # number that counts rows nobody would call a camera.
    #
    # Deliberately NOT a delete and NOT a merge. The row is history, the
    # registered total is a true count of registrations, and the sightings stay
    # attached to the node that actually took them.
    ("nodes", "superseded_by", "TEXT"),
    # 🚨 "THESE SIGHTINGS MAY BE THE SAME VEHICLE." A GUESS, LABELLED AS ONE.
    #
    # This is the most dangerous column in the schema, so read all four notes
    # before touching anything that writes it.
    #
    # 1. IT IS THE THING THIS PROJECT REFUSES TO DO, POINTED THE OTHER WAY.
    #    Linking one vehicle across cameras over time IS tracking - it is the
    #    entire capability the architecture exists to not have for the public.
    #    It is defensible here for exactly one class of vehicle: a marked
    #    patrol car on a public road, paid for publicly, whose plate this
    #    system already keeps ON PURPOSE while destroying everyone else's.
    #    Applied to a civilian it would make every promise on /transparency
    #    untrue in one commit. See tag_sighting(), which refuses.
    #
    # 2. IT IS INFERRED AND MUST READ AS INFERRED. Never "unit 4021", always
    #    "possibly the same vehicle as N other sightings, because <reason>".
    #    tag_why carries the reason in words, the way vclass_why already does,
    #    because a link nobody can check is just an assertion with extra steps.
    #
    # 3. IT IS DERIVED, NEVER AUTHORITATIVE. tag_rev records WHICH rule or
    #    model drew the link, so a better one can re-derive every tag and you
    #    can still tell old conclusions from new. Nothing downstream may treat
    #    a tag as a fact about the world - it is this version's opinion, and
    #    the whole point of the column is that the opinion will improve.
    #
    # 4. IT MUST BE ABLE TO BE WRONG WITHOUT COSTING ANYTHING. A tag is not a
    #    key, nothing joins on it, and deleting the column loses no sighting.
    ("sightings", "vehicle_tag", "TEXT"),
    ("sightings", "tag_conf", "REAL"),
    ("sightings", "tag_why", "TEXT"),
    ("sightings", "tag_rev", "TEXT"),
    # 🚨 THE GOOD PICTURE, ONCE THE CAMERA HAS SENT IT. Separate from `snap` so
    # the record still says which crop was the plate-illegible one, and so a
    # camera is never asked twice for a picture it has already handed over.
    ("sightings", "snap_full", "TEXT"),
]

# The setup funnel, in order. Progress only ever moves FORWARD through this
# list: a reload that reports an earlier stage must not erase how far someone
# actually got, or the numbers describe the last attempt rather than the best.
SETUP_STAGES = ("registered", "starting", "camera_granted", "preview",
                "model_loading", "model_ready", "detecting", "posted")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


# Set once, by whichever thread connects first. See the note inside connect().
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def connect() -> sqlite3.Connection:
    """One connection per thread. sqlite objects are not thread-portable.

    🚨 WHOEVER CREATES THE THREAD MUST CALL close_thread() WHEN IT ENDS.
    See the note there - not doing so took the public site down repeatedly.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        # 🚨 THE SCHEMA IS A PROPERTY OF THE DATABASE, NOT OF THE CONNECTION,
        # SO RUNNING IT PER THREAD IS BOTH POINTLESS AND EXPENSIVE.
        #
        # ThreadingHTTPServer makes a NEW THREAD PER CONNECTION, so every
        # visitor was running executescript(SCHEMA) and _migrate() - and both
        # take a SQLite WRITE lock. Uncontended that costs 0.002s and looks
        # free. Under this box's ingest (~87,000 camera posts per scraper
        # cycle) the write lock is busy, so a new thread could sit in connect()
        # for a very long time: measured 2026-08-19, an /api/nodes rebuild held
        # for 165 SECONDS while the rebuild itself takes 0.15s.
        #
        # CREATE TABLE IF NOT EXISTS is idempotent, so doing it once per
        # process is exactly as correct and removes a write-lock acquisition
        # from every single connection. The lock below makes the first caller
        # finish before any other thread reads - once, at startup, not forever.
        global _SCHEMA_READY
        if not _SCHEMA_READY:
            with _SCHEMA_LOCK:
                if not _SCHEMA_READY:
                    conn.executescript(SCHEMA)
                    _migrate(conn)
                    _SCHEMA_READY = True
        _local.conn = conn
    return conn


def close_thread() -> None:
    """Release this thread's connection. Safe to call when there isn't one.

    🚨 THIS IS WHY THE SITE KEPT GOING DOWN, AND IT LOOKED LIKE A DEAD DATABASE.

    `connect()` caches one sqlite connection per thread, which is correct -
    sqlite objects are not thread-portable. ThreadingHTTPServer creates a NEW
    THREAD PER CONNECTION, so every visitor minted a connection, and nothing
    ever closed them: when the thread ended the Connection was not collected
    promptly, so its two file descriptors (the db and its -wal) stayed open.

    They accumulate until the process hits its open-file limit - 1024 by
    default - and from that moment EVERY route fails, permanently, with

        sqlite3.OperationalError: unable to open database file

    which reads like the database is missing or unreadable. It is neither. The
    file is fine; the process simply cannot open anything else. Measured on the
    live box mid-outage: 10 live threads, 1011 descriptors on sparrow.db and
    sparrow.db-wal, disk 25% full, permissions correct.

    📌 It took about 45 minutes of ordinary public traffic to reach the limit,
    and 105,768 of those errors had been logged in four days. It stayed hidden
    because a restart resets the count and the box was being restarted often -
    every deploy silently "fixed" it for another 45 minutes. The first time it
    went two hours without a restart, the site was simply down.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        _local.conn = None
        try:
            conn.close()
        except Exception:
            pass          # already closed, or closing from the wrong thread


def init() -> None:
    connect()


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

FIELDS = ("node_id", "ts", "lat", "lon", "tier", "plate_hash", "plate_text",
          "plate_state", "plate_conf", "vclass", "vclass_conf", "vclass_why",
          "color", "body", "make", "model", "heading", "speed_mph", "snap",
          "source", "sig_ok",
          # Which banked training crop this sighting came from. Stored HERE
          # rather than discovered by scanning the bank: the review queue used
          # to search every sidecar once per sighting, which was over 300,000
          # file reads on a single page load and got worse every hour the node
          # ran. A join key belongs in the table that needs to join.
          "bank_ref",
          # Who decided, at INSERT time. Only ever set by the operator-confirm
          # path, which is the one route where a person has already ruled before
          # the row exists. Everything else leaves these NULL and earns them
          # through promote_sighting/review_sighting. Without them here, an
          # operator-confirmed row reached the public tier with reviewed IS NULL
          # - indistinguishable from something published with nobody looking,
          # which is the one claim this project makes about itself.
          "reviewed", "decided_by")


def insert_sighting(rec: dict) -> int:
    """Write one detection.

    The tier gate lives here, at the single choke point every path must pass
    through, rather than being re-decided by each caller. If the tier is not
    'public', plate_text and plate_state are dropped on the floor before the
    INSERT is even built.
    """
    rec = dict(rec)
    if rec.get("tier") != "public":
        rec["plate_text"] = None
        rec["plate_state"] = None

    # The other high-traffic write. Same reasoning as upsert_node: a sighting
    # carries a lat/lon that several code paths compute, and snap_point returns
    # a TUPLE - exactly the shape that broke node writes.
    guard_bindable({f: rec.get(f) for f in FIELDS}, "add_sighting")
    conn = connect()
    cols = ",".join(FIELDS)
    marks = ",".join("?" for _ in FIELDS)
    cur = conn.execute(
        f"INSERT INTO sightings ({cols}) VALUES ({marks})",
        tuple(rec.get(f) for f in FIELDS))
    conn.execute(
        "UPDATE nodes SET last_seen = ?, sightings = sightings + 1 WHERE id = ?",
        (rec.get("ts", now()), rec.get("node_id")))
    conn.commit()
    return int(cur.lastrowid)


# 🚨 NOTHING THAT IS NOT A SCALAR GOES NEAR THE DATABASE.
#
# HIS INSTRUCTION, after a road-name cache bug wrote a tuple into a node's
# road_name and every camera signup started returning "internal error".
#
# What made that expensive was not the bug, it was the DIAGNOSIS. sqlite3 says
#     Error binding parameter 21: type 'tuple' is not supported
# which names a POSITION in a generated SQL statement, not a field. Finding out
# that 21 meant `road_name` took counting placeholders by hand in a 22-column
# insert, in a live outage, while signups were failing.
#
# This costs one dict walk per write and turns that into
#     upsert_node: road_name is tuple, expected str/int/float/bytes/None
# before the statement is prepared. It cannot fix a bad value - only the caller
# knows what was meant - but a write that is going to fail should fail by
# NAME, immediately, and never leave a half-formed row behind it.
_BINDABLE = (str, int, float, bytes, bool, type(None))


def guard_bindable(mapping: dict, where: str) -> dict:
    """Every value must be something SQLite can bind. Raises TypeError if not.

    Returned so it can be used inline: conn.execute(sql, guard_bindable(d, "x"))
    """
    bad = [(k, type(v).__name__) for k, v in mapping.items()
           if not isinstance(v, _BINDABLE)]
    if bad:
        detail = ", ".join(f"{k} is {t}" for k, t in sorted(bad))
        raise TypeError(
            f"{where}: {detail}; expected str/int/float/bytes/None. "
            f"Nothing was written.")
    return mapping


def upsert_node(n: dict) -> None:
    guard_bindable(n, "upsert_node")
    conn = connect()
    conn.execute("""
        INSERT INTO nodes (id, name, pubkey, token, lat, lon, pub_lat, pub_lon,
                           heading, fov, reach_m, kind, status, contact,
                           created, last_seen,
                           span_lat1, span_lon1, span_lat2, span_lon2,
                           road_name, span_source)
        VALUES (:id,:name,:pubkey,:token,:lat,:lon,:pub_lat,:pub_lon,:heading,
                :fov,:reach_m,:kind,:status,:contact,:created,:last_seen,
                :span_lat1,:span_lon1,:span_lat2,:span_lon2,
                :road_name,:span_source)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            -- 🚨 A REGISTERED KEY IS NEVER CLEARED BY A RE-ENROL.
            -- This was `pubkey=excluded.pubkey`, so any re-save that omitted
            -- the key wiped it - and _ingest only demands a signature from a
            -- node that HAS a pubkey. So the one call that is supposed to
            -- strengthen a camera's identity could silently downgrade it to
            -- unsigned, and nothing would look wrong: sightings keep arriving,
            -- sig_ok just quietly goes back to 0. Nudging a heading from /aim
            -- would have been enough.
            -- A camera can still ROTATE its key by sending a new one.
            pubkey=COALESCE(excluded.pubkey, nodes.pubkey),
            token=excluded.token,
            lat=excluded.lat, lon=excluded.lon,
            pub_lat=excluded.pub_lat, pub_lon=excluded.pub_lon,
            heading=excluded.heading, fov=excluded.fov, reach_m=excluded.reach_m,
            kind=excluded.kind, status=excluded.status, contact=excluded.contact,
            span_lat1=excluded.span_lat1, span_lon1=excluded.span_lon1,
            span_lat2=excluded.span_lat2, span_lon2=excluded.span_lon2,
            road_name=excluded.road_name, span_source=excluded.span_source
    """, {**{"pubkey": None, "token": None, "contact": None, "created": now(),
             "last_seen": None, "heading": 0, "fov": 60, "reach_m": 45,
             "kind": "fixed", "status": "active",
             "span_lat1": None, "span_lon1": None, "span_lat2": None,
             "span_lon2": None, "road_name": None, "span_source": None}, **n})
    conn.commit()


def set_setup_stage(node_id: str, stage: str,
                    platform: Optional[str] = None) -> bool:
    """Record how far this camera's setup got. Forward-only.

    Returns True if it advanced. A page reload replays earlier stages, and
    accepting those would rewrite a camera that reached 'detecting' back down
    to 'starting' - so the furthest point reached is kept, not the latest one
    reported.
    """
    if stage not in SETUP_STAGES:
        return False
    conn = connect()
    row = conn.execute("SELECT setup_stage FROM nodes WHERE id = ?",
                       (node_id,)).fetchone()
    if row is None:
        return False
    have = row["setup_stage"]
    if have in SETUP_STAGES and SETUP_STAGES.index(have) >= SETUP_STAGES.index(stage):
        return False
    conn.execute("UPDATE nodes SET setup_stage = ?, setup_stage_ts = ?, "
                 "setup_platform = COALESCE(?, setup_platform) WHERE id = ?",
                 (stage, now(), platform, node_id))
    conn.commit()
    return True


def heartbeat(node_id: str, ts: Optional[float] = None) -> None:
    """A node reporting that it is awake and watching.

    Separate from last_seen on purpose. 'A car drove past recently' and 'the
    camera is running' are different questions, and answering the second with
    the first marks every quiet street offline.
    """
    conn = connect()
    conn.execute("UPDATE nodes SET last_beat = ?, "
                 "beats = COALESCE(beats, 0) + 1 WHERE id = ?",
                 (ts or now(), node_id))
    conn.commit()


# The rule/model that drew the current generation of links. BUMP IT whenever
# the logic changes, so a tag can always be traced to what believed it - and so
# a re-tag can be told apart from the thing it replaced.
TAG_REV = "r1-plate"

# Vehicle classes a link may be drawn for. NOT a list of "interesting"
# vehicles - a list of vehicles whose identity this project already publishes
# on purpose. Everything else is destroyed at the edge and must stay that way.
TAGGABLE = ("police", "gov")


class NotTaggable(Exception):
    """Refused: linking this sighting would be tracking a private vehicle."""


def tag_sighting(sighting_id: int, tag: str, conf: float, why: str,
                 rev: str = TAG_REV) -> None:
    """Record that a sighting MAY be the same vehicle as others.

    🚨 THE REFUSAL LIVES HERE, NOT IN THE CALLER.
    Every other guard in this file has eventually been reached by some path
    that forgot it. This one is checked against the stored row at the moment of
    writing - not against what the caller believes about it - so a future
    batch job, backfill or console session cannot link a civilian by being
    unaware that it must not.

    Refuses unless the row is BOTH public tier AND a class this project
    already publishes the identity of. A private sighting has no plate text by
    design; linking one by appearance would be building the tracker this map
    exists to argue against.
    """
    if not why or not why.strip():
        # Same rule as vclass_why: a claim with no stated evidence is not a
        # claim, it is an assertion, and nobody downstream can check it.
        raise ValueError("a link needs a reason in words")
    conn = connect()
    row = conn.execute("SELECT tier, vclass FROM sightings WHERE id = ?",
                       (sighting_id,)).fetchone()
    if not row:
        raise NotTaggable(f"no sighting {sighting_id}")
    if row["tier"] != "public" or (row["vclass"] or "") not in TAGGABLE:
        raise NotTaggable(
            f"sighting {sighting_id} is tier={row['tier']} "
            f"vclass={row['vclass']}: linking it would be tracking a private "
            f"vehicle, which is the thing this project refuses to do")
    conn.execute("UPDATE sightings SET vehicle_tag = ?, tag_conf = ?, "
                 "tag_why = ?, tag_rev = ? WHERE id = ?",
                 (tag, float(conf), why.strip(), rev, sighting_id))
    conn.commit()


def clear_tags(rev: Optional[str] = None) -> int:
    """Forget every link, or every link drawn by one revision.

    ⚠️ EXISTS SO THE LINKS CAN BE WRONG. A better model should be able to throw
    away everything the previous one believed rather than leave two
    generations of guess sharing one column and no way to tell them apart.
    """
    conn = connect()
    if rev:
        cur = conn.execute("UPDATE sightings SET vehicle_tag = NULL, "
                           "tag_conf = NULL, tag_why = NULL, tag_rev = NULL "
                           "WHERE tag_rev = ?", (rev,))
    else:
        cur = conn.execute("UPDATE sightings SET vehicle_tag = NULL, "
                           "tag_conf = NULL, tag_why = NULL, tag_rev = NULL "
                           "WHERE vehicle_tag IS NOT NULL")
    conn.commit()
    return cur.rowcount


# How coarse a pending sighting's position is published. 0.05 degrees is about
# 5 km of latitude - a town, not a street.
PENDING_CELL = 0.05

# How long a possible patrol car is worth flagging as "someone should look".
# Beyond this it is not news, it is a backlog, and a map is the wrong place to
# report a backlog.
PENDING_WINDOW_S = 6 * 3600


def pending_areas(now_ts: Optional[float] = None) -> list[dict]:
    """Rough areas where the classifier thinks it saw a patrol car, unreviewed.

    🚨 COARSE ON PURPOSE, AND COARSENED HERE RATHER THAN IN THE BROWSER.
    These rows have NOT been reviewed, so the claim behind each one is a
    machine's opinion that nobody has checked. One in twenty of those is not a
    patrol car - and publishing a precise position for an unconfirmed claim
    would put a specific civilian vehicle on a public map on a guess.
    Rounding in the client would be decoration; the exact position would still
    have been sent. So the cell is all that ever leaves this function.

    ⚠️ NO id, NO photograph, NO plate, NO node. A count and a cell centre is
    the whole answer - enough to say "something here wants a human", which is
    the entire purpose, and not enough to identify anybody.
    """
    # 🚨 THE PENDING POOL IS A DIRECTORY, NOT A COLUMN, AND THE FIRST VERSION
    # OF THIS QUERIED THE COLUMN.
    #
    # `/rv` reads mirror.REVIEW - one JSON file per candidate, keyed by
    # sighting id - and a crop sits there until a human presses Cop or Not a
    # cop. `sightings.reviewed` is what happens AFTERWARDS. So a query for
    # `reviewed IS NULL` returns nothing whether the queue is empty or has
    # sixty items in it, and this layer would have read zero for ever while
    # the reviewer stared at a full queue.
    #
    # Caught only because he sent screenshots showing "6 left" while the
    # endpoint said none. Two numbers about the same thing disagreeing is the
    # only reason it surfaced - the same way every silent drop in this project
    # has surfaced.
    import mirror                        # local: hub imports this lazily too
    now_ts = now_ts or now()
    cells: dict = {}
    if not mirror.REVIEW.exists():
        return []
    conn = connect()
    for jp in mirror.REVIEW.glob("*.json"):
        try:
            sid = int(jp.stem)
        except ValueError:
            continue
        r = conn.execute("SELECT lat, lon, ts FROM sightings WHERE id = ?",
                         (sid,)).fetchone()
        if not r or r["lat"] is None or r["lon"] is None:
            continue
        if (r["ts"] or 0) < now_ts - PENDING_WINDOW_S:
            continue
        key = (round(r["lat"] / PENDING_CELL), round(r["lon"] / PENDING_CELL))
        cells[key] = cells.get(key, 0) + 1
    return [{"lat": round(k[0] * PENDING_CELL, 3),
             "lon": round(k[1] * PENDING_CELL, 3), "n": v}
            for k, v in sorted(cells.items(), key=lambda kv: -kv[1])]


def tagged_group(tag: str, limit: int = 200) -> list[dict]:
    """Every sighting currently believed to be this vehicle, oldest first.

    ⚠️ Ordered by time on purpose: what a reader wants from a link is the
    SEQUENCE - where it was, then where it was next - and a list that arrives
    in an arbitrary order invites the reader to invent one.
    """
    return [dict(r) for r in connect().execute(
        "SELECT * FROM sightings WHERE vehicle_tag = ? AND tier = 'public' "
        "ORDER BY ts ASC LIMIT ?", (tag, limit)).fetchall()]


def heartbeat_many(node_ids: list, ts: Optional[float] = None) -> int:
    """Many nodes reporting at once, in ONE transaction.

    🚨 EXISTS BECAUSE ONE PROCESS NOW SPEAKS FOR THOUSANDS OF CAMERAS. The
    public-traffic-camera poller watches ~4,400 of them; beaten one at a time
    that is 4,400 requests and 4,400 commits every cycle against a two-core box
    whose descriptor ceiling has taken the site down once already.

    ⚠️ Callers MUST authenticate every id first. This function trusts its list
    completely - it is the storage half only, and the checking half is in the
    route.
    """
    if not node_ids:
        return 0
    t = ts or now()
    conn = connect()
    conn.executemany("UPDATE nodes SET last_beat = ?, "
                     "beats = COALESCE(beats, 0) + 1 WHERE id = ?",
                     [(t, nid) for nid in node_ids])
    conn.commit()
    return len(node_ids)


def audit(action: str, target: str = "", actor: str = "anon", ip: str = "") -> None:
    conn = connect()
    conn.execute("INSERT INTO audit (ts, action, target, actor, ip) VALUES (?,?,?,?,?)",
                 (now(), action, target, actor, ip))
    conn.commit()


def recent_audit(limit: int = 200) -> list[dict]:
    """Most recent audit rows, newest first. Redaction stays the caller's job."""
    rows = connect().execute(
        "SELECT ts, action, target, ip FROM audit ORDER BY ts DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def health_check() -> None:
    """Cheapest possible proof the database is reachable. Raises on failure."""
    connect().execute("SELECT 1").fetchone()


# --------------------------------------------------------------------------
# Reads.  Every one of these returns raw rows; redaction happens in the hub so
# it is applied uniformly and is visible in one place during review.
# --------------------------------------------------------------------------

def search_plate(text: str, limit: int = 200) -> list[dict]:
    """Every CONFIRMED public sighting of a plate.

    🚨 THE FILTER IS IN THE QUERY, NOT ONLY IN THE REDACTOR.
    privacy.redact already withholds the text from unconfirmed rows, but a
    search that scanned every row and then hid the results would still ANSWER
    the question - "no matches" and "matches you may not see" are
    distinguishable by anyone paying attention, and that is a lookup oracle for
    private plates. So private and unconfirmed rows are never scanned.

    Matching is prefix-or-exact on a normalised string, deliberately not
    fuzzy: a near-miss search that returns somebody else's vehicle is worse
    than no result.
    """
    q = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if len(q) < 3:
        return []
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM sightings WHERE tier='public' AND reviewed='confirmed' "
        "AND plate_text IS NOT NULL AND plate_text != '' "
        "AND REPLACE(REPLACE(UPPER(plate_text),' ',''),'-','') LIKE ? "
        "ORDER BY ts DESC LIMIT ?", (q + "%", int(limit))).fetchall()
    return [dict(r) for r in rows]


#: A public sighting with no photograph is withheld from the public feed.
#:
#: 🚨 NO PICTURE, NO PUBLICATION. A public row is an ASSERTION that a specific
#: government vehicle passed a specific place, and the photograph is the whole
#: of the evidence for it. Without one there is nothing a reader can check and
#: nothing a reporter can dispute - it is the project asking to be believed,
#: which is the opposite of the argument it makes for existing.
#:
#: It withholds rather than deletes. The vehicle really did pass, so erasing
#: the row would replace an unevidenced claim with a false absence, and the
#: rule reverses itself the moment a photo is attached: crop a held photo back
#: on and the sighting returns by itself, with no state to unwind. Same reason
#: the span consent check lives at the point of publication.
_HAS_PHOTO = "snap IS NOT NULL AND snap <> ''"
#: A public row must carry a photo; a private one is live traffic, not a claim,
#: and never had one to show. So the rule binds the public rows inside a mixed
#: feed rather than the whole query.
_PUBLISHABLE = f"(tier <> 'public' OR ({_HAS_PHOTO}))"


def recent_sightings(since: float = 0, limit: int = 500,
                     vclass: Optional[str] = None,
                     bbox: Optional[tuple] = None) -> list[dict]:
    sql = "SELECT * FROM sightings WHERE ts > ?"
    args: list[Any] = [since]
    if vclass and vclass != "all":
        if vclass == "public":
            sql += f" AND tier = 'public' AND {_HAS_PHOTO}"
        else:
            sql += " AND vclass = ? AND " + _PUBLISHABLE
            args.append(vclass)
    else:
        sql += " AND " + _PUBLISHABLE
    if bbox:
        sql += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        args += [bbox[0], bbox[2], bbox[1], bbox[3]]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(min(int(limit), 2000))
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def track_for(plate_hash: str, limit: int = 500) -> list[dict]:
    """All sightings sharing a hash, oldest first. This is a vehicle's trail."""
    return [dict(r) for r in connect().execute(
        "SELECT * FROM sightings WHERE plate_hash = ? ORDER BY ts ASC LIMIT ?",
        (plate_hash, min(int(limit), 2000))).fetchall()]


def sighting(sid: int) -> Optional[dict]:
    r = connect().execute("SELECT * FROM sightings WHERE id = ?", (sid,)).fetchone()
    return dict(r) if r else None


# How long after a sighting the camera may still be asked for the full picture.
# Long enough to survive a review that waits for a human, short enough that a
# phone is not holding pictures around indefinitely.
FULLRES_WINDOW_S = 3600.0


def wants_fullres(node_id: str, limit: int = 5) -> list[int]:
    """Published sightings from this camera that are still only the small crop.

    🚨 THIS EXISTS BECAUSE THE ONLY DEVICE THAT EVER HAD THE FULL PICTURE IS THE
    ONE THAT TOOK IT. Everything uploaded is capped at 200px so a private plate
    cannot survive the trip. That is the right default and it must not change.
    But when the head later decides a vehicle IS a government one, it belongs in
    the public tier where the plate is legible ON PURPOSE - and by then the only
    copy the server has is the deliberately ruined one.

    Confirming on /rv cannot fix it either: a reviewer is looking at somebody
    else's sighting and never had the original. So the camera has to be ASKED,
    and the heartbeat it already sends is the place to ask.

    ⚠️ PUBLISHED ONLY. Never ask for a better picture of a private-tier vehicle;
    that is the whole guarantee. `tier = 'public'` is the state machine's own
    answer, not the label somebody typed.
    """
    cur = connect().execute(
        """SELECT id FROM sightings
            WHERE node_id = ? AND tier = 'public' AND ts > ?
              AND snap IS NOT NULL AND snap != ''
              AND (snap_full IS NULL OR snap_full = '')
            ORDER BY ts DESC LIMIT ?""",
        (node_id, now() - FULLRES_WINDOW_S, int(limit)))
    return [int(r["id"]) for r in cur.fetchall()]


def mark_fullres(sid: int, name: str) -> None:
    """Remember that the good picture arrived, so it is never asked for twice."""
    c = connect()
    c.execute("UPDATE sightings SET snap_full = ? WHERE id = ?", (name, int(sid)))
    c.commit()


# ---------------------------------------------------------------------------
# Signal partners
# ---------------------------------------------------------------------------
# 🚨 EVERY ONE OF THESE IS TOKEN-GATED AND INDIVIDUALLY REVOCABLE. His reason,
# and it is the right one: "we could gate the endpoint for them sending data
# with tokens so we can cut off bad actors directly. that way we don't have to
# blame shin for anyone using his system."
#
# One partner, one token. If somebody downstream of them abuses it, that token
# dies and nobody else notices.

SIGNAL_MAX_BATCH = 500          # per request
SIGNAL_MAX_AGE_S = 7 * 86400    # refuse observations older than a week


def add_signal_token(token_hash: str, label: str,
                     created_by: str = "operator") -> int:
    c = connect()
    cur = c.execute(
        "INSERT INTO signal_tokens (token_hash, label, created_at, created_by) "
        "VALUES (?,?,?,?)", (token_hash, label, now(), created_by))
    c.commit()
    return int(cur.lastrowid)


def signal_token(token_hash: str) -> Optional[dict]:
    """The live partner behind this token, or None. Revoked reads as None."""
    r = connect().execute(
        "SELECT * FROM signal_tokens WHERE token_hash = ? AND revoked_at IS NULL",
        (token_hash,)).fetchone()
    return dict(r) if r else None


def list_signal_tokens(include_revoked: bool = True) -> list[dict]:
    sql = "SELECT * FROM signal_tokens"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    return [dict(r) for r in connect().execute(sql + " ORDER BY id").fetchall()]


def revoke_signal_token(tid: int) -> bool:
    c = connect()
    cur = c.execute(
        "UPDATE signal_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now(), int(tid)))
    c.commit()
    return cur.rowcount > 0


def add_signal_obs(partner: int, rows: list[dict]) -> int:
    """Store observations. Returns how many were kept.

    ⚠️ THIS NEVER TOUCHES `sightings`. A partner reports what they heard; what
    it MEANS is decided here, later, against our own published rows only.
    """
    c = connect()
    t = now()
    kept = 0
    for r in rows[:SIGNAL_MAX_BATCH]:
        try:
            ts = float(r.get("ts") or 0)
            lat = float(r.get("lat"))
            lon = float(r.get("lon"))
        except (TypeError, ValueError):
            continue
        sig = str(r.get("signature") or "").strip()[:200]
        if not sig or not ts:
            continue
        # ⚠️ A stale or future timestamp cannot be correlated with anything and
        # is the shape a replayed or fabricated batch takes.
        if ts < t - SIGNAL_MAX_AGE_S or ts > t + 300:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        c.execute(
            "INSERT INTO signal_obs (partner, ts, lat, lon, band, signature, "
            "conf, note, received) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(partner), ts, lat, lon,
             str(r.get("band") or "")[:32], sig,
             float(r.get("conf") or 0), str(r.get("note") or "")[:300], t))
        kept += 1
    c.execute("UPDATE signal_tokens SET last_used_at = ?, posted = posted + ? "
              "WHERE id = ?", (t, kept, int(partner)))
    c.commit()
    return kept


def signal_matches(signature: str, window_s: float = 120.0,
                   radius_deg: float = 0.003, limit: int = 50) -> list[dict]:
    """Published government sightings near this signature's observations.

    🚨 `tier = 'public'` IS NOT OPTIONAL AND IS NOT A FILTER ADDED BY THE
    CALLER. Correlating a signature against a PRIVATE row would build exactly
    the thing this project argues against: a way to follow an ordinary person
    by something their car emits. The predicate lives in the SQL so no caller
    can forget it.

    Everything this returns is a COINCIDENCE IN TIME AND PLACE, not proof.
    """
    rows = connect().execute(
        """SELECT o.id AS obs_id, o.ts AS obs_ts, o.band, o.signature,
                  s.id AS sighting_id, s.ts AS sighting_ts,
                  s.vclass, s.lat, s.lon
             FROM signal_obs o
             JOIN sightings s
               ON s.tier = 'public'
              AND ABS(s.ts - o.ts) <= ?
              AND ABS(s.lat - o.lat) <= ?
              AND ABS(s.lon - o.lon) <= ?
            WHERE o.signature = ?
            ORDER BY ABS(s.ts - o.ts)
            LIMIT ?""",
        (window_s, radius_deg, radius_deg, signature, int(limit))).fetchall()
    return [dict(r) for r in rows]


# 🚨 THE NODE LIST IS BUILT ONCE PER WINDOW, NOT ONCE PER READER.
#
# On 2026-08-18 the map wedged twice within ten minutes of a restart. Every
# stuck thread had the same stack: db.nodes(), 100+ of them at once, each
# materialising all 13,637 camera rows. Uncontended that query answers in
# 0.19s; a hundred concurrent copies of it under one GIL took SIX MINUTES
# (/api/places measured at 366.8s, /api/nodes at 363.4s), and requests holding
# admission permits that long drained every pool until readers got 503s.
#
# Two routes call this on the hot path - /api/nodes and /api/places - and the
# HTTP micro-cache above them only collapses identical URLs, so it could not
# help /api/nodes?public_cams=0 and /api/nodes at the same moment. The right
# place to collapse the work is the work itself.
#
# ⚠️ THE TTL IS A STALENESS BUDGET FOR last_beat, NOT A GUESS. A row's only
# fast-moving field is its heartbeat, and a camera beats every 30s against a
# 90s online window, so 5s of lag cannot change whether a camera reads as
# online. Adding or moving a camera appears within the same 5s.
_NODES_CACHE: dict = {}
_NODES_LOCK = threading.Lock()
_NODES_TTL_S = 5.0


def _build_nodes(active_only: bool, include_superseded: bool, key) -> list[dict]:
    """Read the node list and publish it to the cache. Safe to run concurrently.

    Two threads doing this at once produce identical results and the second
    simply overwrites the first, so it needs no lock of its own. That is the
    whole reason a cold-cache reader can be told to just do the work instead of
    waiting or being refused.
    """
    sql = "SELECT * FROM nodes"
    where = []
    if active_only:
        where.append("status = 'active'")
    if not include_superseded:
        where.append("(superseded_by IS NULL OR superseded_by = '')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = [dict(r) for r in connect().execute(sql + " ORDER BY name").fetchall()]
    _NODES_CACHE[key] = (now(), rows)
    return [d.copy() for d in rows]


def nodes(active_only: bool = True, include_superseded: bool = False) -> list[dict]:
    """Cameras. By default, the ones that ARE a camera right now.

    A superseded row is a real record of a real period, and it is not a camera
    any more - it is where a camera used to be. Listing it puts a second entry
    on the map for one device and makes the same claim twice. Excluded here for
    the same reason it is excluded from nodes_online, and by the same test, so
    the list and the count cannot disagree.
    """
    key = (bool(active_only), bool(include_superseded))
    hit = _NODES_CACHE.get(key)
    if hit and now() - hit[0] < _NODES_TTL_S:
        return [d.copy() for d in hit[1]]

    # 🚨 A READER MUST NEVER WAIT FOR A REBUILD. THIS LOCK USED TO BE `with`,
    # AND THAT IS WHAT WEDGED THE MAP.
    #
    # The first version held this lock across the rebuild - including across
    # connect(). On a fresh thread connect() runs executescript(SCHEMA) and
    # _migrate(), which take a SQLite WRITE lock, and ThreadingHTTPServer makes
    # a new thread per connection. Behind ingest writing ~58,000 rows a cycle
    # that write lock is contended, so the holder stalled, and every other
    # reader queued behind it.
    #
    # Measured 2026-08-19 during a live wedge: 50 threads with their top frame
    # on this exact line, /api/nodes held 193s, heavy_free 0/48, while a
    # rebuild in isolation costs 0.15s and connect() setup costs 0.002s. The
    # work was never expensive; the convoy was. A lock that serialises a cheap
    # operation behind a slow one is worse than no lock at all.
    #
    # So: try for the lock, and if somebody else is already rebuilding, serve
    # the stale copy immediately instead of joining a queue. Node data changes
    # when a camera is enrolled - a few extra seconds of staleness cannot
    # matter, and the alternative is measurably a 193-second stall.
    got = _NODES_LOCK.acquire(blocking=False)
    if not got:
        if hit:
            return [d.copy() for d in hit[1]]      # stale, and that is fine
        # ⚠️ NOTHING CACHED AT ALL - the first requests after a restart, when
        # there is no stale copy to fall back on. The first version raised
        # TimeoutError here after a bounded wait, and that shipped a REGRESSION:
        # measured immediately after deploying it, five consecutive /api/nodes
        # requests returned 500 while the cache was cold.
        #
        # Refusing was never the right answer. A rebuild costs 0.15s, so the
        # worst case of just doing the work is a handful of duplicate builds
        # during the first second of a restart - which is cheaper than one 500
        # served to a real visitor. Build it, publish it, and let whoever holds
        # the lock publish theirs too; last writer wins and both are correct.
        if not _NODES_LOCK.acquire(timeout=2.0):
            hit = _NODES_CACHE.get(key)
            if hit:
                return [d.copy() for d in hit[1]]
            return _build_nodes(active_only, include_superseded, key)
    try:
        hit = _NODES_CACHE.get(key)
        if hit and now() - hit[0] < _NODES_TTL_S:
            return [d.copy() for d in hit[1]]
        sql = "SELECT * FROM nodes"
        where = []
        if active_only:
            where.append("status = 'active'")
        if not include_superseded:
            where.append("(superseded_by IS NULL OR superseded_by = '')")
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [dict(r)
                for r in connect().execute(sql + " ORDER BY name").fetchall()]
        _NODES_CACHE[key] = (now(), rows)
    finally:
        _NODES_LOCK.release()
    # Callers get their OWN dicts, so anything that edits a row in a loop keeps
    # working exactly as before and cannot corrupt the shared copy.
    return [d.copy() for d in rows]


def node(nid: str) -> Optional[dict]:
    r = connect().execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
    return dict(r) if r else None


def set_node_pubkey(node_id: str, pubkey: str) -> None:
    """Record a node's Ed25519 public key. Caller is responsible for auditing."""
    c = connect()
    c.execute("UPDATE nodes SET pubkey=? WHERE id=?", (pubkey, node_id))
    c.commit()


def set_node_token(node_id: str, token: str) -> None:
    """Replace a node's shared token (key rotation). No audit event by design."""
    c = connect()
    c.execute("UPDATE nodes SET token=? WHERE id=?", (token, node_id))
    c.commit()


# A node is online if it has beaten within this window. Two missed beats at the
# 30 s cadence, so one dropped request does not blink a camera offline.
ONLINE_WINDOW_S = 90.0

# ...or if it has POSTED within this one. Deliberately much wider: a heartbeat
# is a clock and its absence is meaningful within seconds, while a sighting is
# an event - a phone watching a quiet road produces nothing for minutes and is
# no less switched on. Five minutes is long enough to cover a red light and
# short enough that a driver who has arrived home drops off the count.
POSTING_WINDOW_S = 300.0

# 🚨 A PUBLIC TRAFFIC CAMERA CANNOT SATISFY A 90 SECOND WINDOW, AND IT IS NOT
# BROKEN FOR THAT.
#
# The 90s above is built around a phone beating every 30 seconds - a cadence the
# DEVICE chooses. A public camera does not beat for itself: one poller sweeps
# thousands of them on a schedule we set, currently a ~150s sweep inside a 300s
# interval, so each camera is heard from exactly once per cycle. Judged by the
# phone's window, the entire fleet would flap - measured going 4,484 online,
# then ~700 a minute later - and the front page would swing by thousands while
# nothing whatsoever changed.
#
# So the window follows the CADENCE, not the kind of hardware: two poll cycles
# plus slack, the same "one dropped request must not blink it offline" rule the
# 90s was derived from. If --interval on the poller changes, this changes.
#
# ⚠️ IT IS DELIBERATELY NOT A BLANKET WIDENING. A volunteer's camera keeps the
# tight window, because there the absence of a beat is meaningful within
# seconds and that is the whole value of the signal.
# 🚨 SIZED FOR THE SLOWEST POLLER, AND THERE IS MORE THAN ONE NOW. Helsinki
# sweeps every 300s and Ashburn every 600s, so two cycles plus slack is 1260 -
# not the 660 that was right when Helsinki was the only box.
#
# 🚨 THIS EXACT COUPLING BROKE THE MAP ON 2026-08-16. During the spike the
# intervals were raised to 900s and 1800s to shed ingest, and this constant was
# left alone. Every camera then beat LESS OFTEN than the window that declares it
# online, so 9,900 working cameras reported as dead - a fleet-wide outage that
# was purely an arithmetic disagreement between two files, while every camera
# was fetching images perfectly the whole time.
#
# ⚠️ IF YOU CHANGE --interval ON EITHER POLLER, CHANGE THIS IN THE SAME EDIT.
# The rule is 2 × (longest interval) + 60.
PUBLIC_CAM_WINDOW_S = 1260.0


def beat_window(kind: str) -> float:
    """How stale a heartbeat may be before that KIND of node reads offline.

    One definition, because three places asked the same question and would
    otherwise answer it differently - /api/stats, /api/nodes and the town
    badges each had their own copy of `now() - ONLINE_WINDOW_S`, so a fleet
    fixed in one would still flap in the other two.
    """
    return PUBLIC_CAM_WINDOW_S if kind == "public_cam" else ONLINE_WINDOW_S



def stats() -> dict:
    c = connect()
    one = lambda q, a=(): c.execute(q, a).fetchone()[0]
    day = now() - 86400

    # ⚠️ COUNT(DISTINCT plate_hash) COUNTED THE EMPTY STRING AS A VEHICLE.
    #
    # privacy.plate_hash returns '' for a pass with no readable plate, and ''
    # is a value, not a NULL - so 31 plateless sightings counted as exactly one
    # distinct vehicle and the map reported "1 vehicle" while identifying none.
    # The insert path now stores NULL, and this guard keeps the figure honest
    # for rows written before that fix.
    #
    # The headline figure is also PUBLIC-TIER ONLY. A private vehicle is a
    # thing the system deliberately cannot identify or count across cameras -
    # reporting a distinct count of them implies a tracking ability that the
    # whole architecture exists to not have.
    # ⚠️ AND WHEN NOTHING CAN BE COUNTED, SAY SO RATHER THAN SAYING ZERO.
    #
    # This counts DISTINCT IDENTIFIED vehicles, which needs a plate. At
    # the reference camera the effective plate width is ~22px against the 60 needed,
    # so no plate is ever read and the figure is structurally zero - displayed
    # next to "4 public sightings", which reads as a contradiction or a bug.
    #
    # It is neither. It is the difference between "we counted none" and "we
    # cannot count", and reporting the second as the first is the mistake his
    # standing rule names: a gap in the instrument is not evidence of absence.
    # `vehicles_countable` lets the UI tell the truth instead.
    identified = """SELECT COUNT(DISTINCT plate_hash) FROM sightings
                    WHERE ts > ? AND tier='public'
                      AND plate_hash IS NOT NULL AND plate_hash != ''"""
    countable = """SELECT COUNT(*) FROM sightings
                   WHERE ts > ? AND tier='public'
                     AND plate_hash IS NOT NULL AND plate_hash != ''"""
    return {
        "nodes_active":   one("SELECT COUNT(*) FROM nodes WHERE status='active'"),
        # Cameras that have actually contributed something, ever. Kept beside
        # nodes_active rather than replacing it: "29 enrolled" is true and
        # encouraging, and this is the harder number that keeps it honest.
        "nodes_ever_produced": one(
            "SELECT COUNT(DISTINCT node_id) FROM sightings WHERE node_id != ''"),
        # Every "still watching" any camera has ever sent, added up. Deliberately
        # a network total: heartbeats were not always on, so a per-node count
        # would punish the cameras that were running before it existed.
        "heartbeats_total": one("SELECT COALESCE(SUM(beats), 0) FROM nodes"),
        # 🚨 status='active' MATTERS HERE, AND ITS ABSENCE PRODUCED "15/12
        # CAMERAS ONLINE". nodes_active filters on status one line above; this
        # did not, and nothing stops a paused or revoked node from beating. So a
        # camera the hub refuses to accept sightings from (_ingest 403s it) and
        # omits from /api/nodes still counted as online, and kept inflating
        # heartbeats_total - which is what the public "hours watched" figure is
        # built from. A denominator that filters and a numerator that does not
        # is how a ratio exceeds 1.
        # ...and superseded_by matters for the same reason. A camera that moved
        # left its old row behind, and that row goes on beating until the
        # device adopts its new id - so one physical camera reported as two or
        # three ONLINE at once. The registered total above deliberately still
        # counts it, because it really was registered; this is the count of
        # things that are a camera right now.
        # 🚨 A PHONE THAT IS CATCHING PATROL CARS IS ONLINE.
        #
        # This counted heartbeats only, and ONLY FIXED CAMERAS BEAT. A phone in
        # drive mode posts sightings continuously and never sends a heartbeat,
        # so somebody actively driving and contributing counted as zero.
        # Measured during a viral wave: the map said "1 online" while seven
        # different people had posted within the hour, most of them on phones.
        # A number that reads "nobody is out there" to a new visitor, at the
        # exact moment the most people ever are, is worse than no number.
        #
        # Posting IS liveness, and it is stronger evidence than a heartbeat: a
        # beat says the process is running, a sighting says it is running AND
        # seeing. The window is wider than ONLINE_WINDOW_S because sightings are
        # event-driven - a quiet street produces none for minutes without the
        # camera being any less on, whereas a missed beat means something.
        # The beat window is per KIND - see PUBLIC_CAM_WINDOW_S. A phone chooses
        # its own 30s cadence; a public camera is swept by one poller once per
        # cycle, and judging the second by the first makes the whole fleet flap.
        "nodes_online":   one("SELECT COUNT(*) FROM nodes n "
                              "WHERE n.status='active' AND n.superseded_by IS NULL "
                              "AND (n.last_beat > "
                              "       (CASE WHEN n.kind='public_cam' THEN ? ELSE ? END)"
                              "     OR EXISTS ("
                              "  SELECT 1 FROM sightings s WHERE s.node_id = n.id "
                              "  AND s.ts > ?))",
                              (now() - PUBLIC_CAM_WINDOW_S, now() - ONLINE_WINDOW_S,
                               now() - POSTING_WINDOW_S)),
        "sightings_24h":  one("SELECT COUNT(*) FROM sightings WHERE ts > ?", (day,)),
        "public_24h":     one("SELECT COUNT(*) FROM sightings WHERE tier='public' AND ts > ?", (day,)),
        # Passes past a camera. Not vehicles - the same car twice is two.
        "traffic_24h":    one("SELECT COUNT(*) FROM sightings WHERE tier!='public' AND ts > ?", (day,)),
        # The same count over the last hour, and it exists to give "moving now"
        # something to be read against. A live figure of 0 is the normal state
        # of a small network at 4am, and on its own it is indistinguishable from
        # every camera being down - which is the reading a visitor will reach
        # for. A rate separates a quiet road from a dead one.
        "traffic_1h":     one("SELECT COUNT(*) FROM sightings WHERE tier!='public' AND ts > ?",
                              (now() - 3600,)),
        "vehicles_24h":   one(identified, (day,)),
        # 0 here means the question could not be asked, not that the answer
        # was none. The UI shows a dash rather than a zero.
        "vehicles_countable": one(countable, (day,)),
        "total":          one("SELECT COUNT(*) FROM sightings"),
        # No "lookups" figure: searches against public-tier data are not logged
        # or counted, by design. Counting them would mean tracking the people who
        # read a public record, which is the surveillance this project refuses.
    }


def merge_window_row(node_id: str, vclass: str, ts: float,
                     window: float = 6.0) -> Optional[dict]:
    """A recent public sighting this one is probably a continuation of.

    🚨 ONE VEHICLE WAS PUBLISHING AS SEVERAL SIGHTINGS.
    Sightings #26974, #26975 and #26976 arrived within 1.3 seconds of each
    other from the same camera. They are one marked municipal unit crossing the
    frame: the tracker lost and re-acquired it as the pillar of the window and
    a parked car occluded it, and each fragment completed independently and
    posted. The map drew three dots for one car and the counter agreed with the
    map, so nothing looked wrong.

    A short window and a matching class is a deliberately blunt test. It cannot
    tell one fragmented car from two patrol cars in convoy, and it will
    occasionally merge the second one - which is the right way round to be
    wrong. Over-counting police presence on a map whose only asset is
    truthfulness is worse than under-counting it, and the merged row keeps a
    detection count so the evidence is not lost.

    🚨 AND THEN THE FIX FOR IT WENT DEAD, SILENTLY.
    This filtered `tier = 'public'`, which was true when a confident government
    call published immediately. It stopped being true when candidates started
    being held for review: ingest now computes the tier, then forces it to
    'private' so a person decides. Every candidate therefore failed this
    predicate, no fragment ever matched, and #26974/5/6 came back - except now
    they arrive as three separate cards in the review pen, so a human confirms
    the same car three times onto the map.

    The caller decides WHEN to merge (only for government candidates, never for
    ordinary traffic - folding civilian passes by class and time would be far
    too blunt). This function's job is to find the fragment, so it no longer
    second-guesses the tier.
    
    """
    row = connect().execute(
        """SELECT * FROM sightings
           WHERE node_id = ? AND vclass = ?
             AND ts > ? AND ts <= ?
           ORDER BY ts DESC LIMIT 1""",
        (node_id, vclass, ts - window, ts)).fetchone()
    return dict(row) if row else None


def bump_detections(sighting_id: int, ts: float, conf: Optional[float]) -> None:
    """Fold another detection into an existing pass.

    Keeps the BEST confidence seen rather than the latest: the strongest look
    the camera got at the vehicle is the honest summary of the pass, and a
    fragment caught as it left the frame should not downgrade one caught square
    on.
    """
    conn = connect()
    conn.execute(
        """UPDATE sightings
           SET detections = COALESCE(detections, 1) + 1,
               ts = MIN(ts, ?),
               vclass_conf = MAX(COALESCE(vclass_conf, 0), COALESCE(?, 0))
           WHERE id = ?""", (ts, conf, sighting_id))
    conn.commit()


def unreview_sighting(sighting_id: int) -> None:
    """Put a sighting back to 'no decision has been made'.

    For UNDO only. `review_sighting` and `promote_sighting` both stamp
    `reviewed`, which is correct for a judgement and wrong for a judgement that
    was withdrawn: leaving 'retracted' behind after an undo hides the sighting
    from the possibly-missed queue for ever, so an accidental keypress would
    still cost the vehicle its chance of being looked at again.

    An undo that leaves a residue is not an undo.
    """
    conn = connect()
    conn.execute("UPDATE sightings SET reviewed=NULL, reviewed_at=NULL "
                 "WHERE id=?", (int(sighting_id),))
    conn.commit()


def review_sighting(sighting_id: int, verdict: str) -> None:
    """Record an operator's judgement on a published sighting.

    A retraction does not delete the row - the vehicle really did pass, and
    replacing one false claim (it was police) with another (nothing was there)
    is not a correction. It is demoted to the private tier and reclassified,
    which is what the map should have said in the first place, and the plate
    text goes with it because a private-tier row must never hold one.
    """
    conn = connect()
    if verdict == "retracted":
        # ⚠️ THE REASON IS A BOUND PARAMETER, NOT AN INLINE LITERAL.
        # It was written as two adjacent quoted strings across a line break -
        # Python's implicit concatenation, which does not exist in SQL. SQLite
        # saw two string literals with no operator between them and raised a
        # syntax error, so EVERY retraction failed with "internal error" while
        # confirm and promote (different code paths) worked fine.
        #
        # Worth noting how it survived my own testing: I checked that the
        # endpoint rejected a bad verdict and an unknown id, and both of those
        # return BEFORE reaching this statement. I tested the guards and never
        # once exercised the operation.
        conn.execute("""UPDATE sightings
                        SET tier='private', vclass='civilian', vclass_conf=NULL,
                            plate_text=NULL, plate_state=NULL, vclass_why=?,
                            reviewed=?, reviewed_at=?
                        WHERE id=?""",
                     ("retracted by the camera operator: not a public-tier vehicle",
                      verdict, now(), sighting_id))
    else:
        conn.execute("UPDATE sightings SET reviewed=?, reviewed_at=? WHERE id=?",
                     (verdict, now(), sighting_id))
    conn.commit()
    # A verdict is the answer to whatever the public flagged, so it clears the
    # flags too. Otherwise a reviewed sighting keeps showing up as "reported".
    resolve_reports(sighting_id)


# The fixed set of things a member of the public can flag. Free text is only a
# note attached to one of these, never a category of its own, so the queue can
# group and count flags without parsing prose.
REPORT_REASONS = {
    "not_government":    "not a government vehicle",
    "wrong_description": "wrong description (body or colour)",
    # 🚨 THE ONE REASON THAT ACTS BEFORE A HUMAN LOOKS. Every other flag is a
    # suggestion that waits in the queue, because being wrong about a vehicle
    # for a few hours costs nothing that cannot be corrected. This one is
    # different: a passer-by's arm, a face, a watch, a work van's lettering
    # caught behind the patrol car is a person's own detail on a public map,
    # and the harm is done while it waits. So a privacy flag HOLDS the
    # photograph immediately (see PRIVACY_REPORT / review_api.hold_photo) and
    # the review decides whether it comes back, cropped or whole.
    #
    # It is deliberately not a delete. The flag may be mistaken, and the stored
    # original is the only thing a full-quality crop can be cut from.
    "privacy":          "shows a person or a private detail",
    "other":            "other (see note)",
}

# The reason that takes the picture down first and asks afterwards.
PRIVACY_REPORT = "privacy"


def add_report(sighting_id: int, reason: str, note: str, ip: str) -> int:
    """Store a public flag against a published sighting. Caller validates that
    the reason is known and the sighting is public tier."""
    conn = connect()
    cur = conn.execute(
        "INSERT INTO reports (sighting_id, ts, reason, note, ip, resolved) "
        "VALUES (?,?,?,?,?,NULL)",
        (sighting_id, now(), reason, (note or "")[:500] or None, ip))
    conn.commit()
    return cur.lastrowid


def open_report_counts() -> dict:
    """{sighting_id: count} of unresolved flags, to mark the review queue."""
    conn = connect()
    return {r[0]: r[1] for r in conn.execute(
        "SELECT sighting_id, COUNT(*) FROM reports WHERE resolved IS NULL "
        "GROUP BY sighting_id").fetchall()}


def reports_for(sighting_id: int, open_only: bool = True) -> list:
    conn = connect()
    q = "SELECT reason, note, ts FROM reports WHERE sighting_id=?"
    if open_only:
        q += " AND resolved IS NULL"
    return [dict(r) for r in conn.execute(q + " ORDER BY ts DESC",
                                          (sighting_id,)).fetchall()]


def public_decision_counts() -> dict:
    """Who decided what is on the public map, as numbers anyone can check.

    Published on /api/policy in place of the old `classifier_validated`, which
    was a copy of a config toggle and measured nothing. The point of this
    project is that a person decides what gets asserted about a vehicle; that
    is either demonstrable or it is marketing.

    'unknown' is reported rather than folded into either side. Those are rows
    published before the column existed, and counting them as human would be
    exactly the kind of flattering guess this endpoint is meant to replace.
    """
    try:
        rows = connect().execute(
            "SELECT COALESCE(decided_by,'unknown') d, COUNT(*) n "
            "FROM sightings WHERE tier='public' GROUP BY d").fetchall()
    except sqlite3.Error:
        return {}
    by = {r["d"]: int(r["n"]) for r in rows}
    total = sum(by.values())
    # 🚨 "_all_time" IS NOT PADDING, IT IS THE WHOLE POINT.
    # This was published as `public_sightings`, and the map header shows a
    # figure with the same name counting the last 24 HOURS - so the site said 8
    # while this said 69 and both were right. On a transparency endpoint, two
    # true numbers wearing one name read as one of them lying, and the reader
    # has no way to tell which. app.js already carries a scar from the same
    # mistake, where the header counted 24h and the panel counted the selected
    # window and they contradicted each other on screen.
    #
    # These counts are ALL-TIME by necessity: the claim is that no sighting has
    # EVER reached the public tier without a person, which a 24-hour window
    # could never support.
    return {
        "public_sightings_all_time": total,
        "public_decided_by_human": by.get("human", 0),
        "public_decided_automatically": by.get("auto", 0),
        "public_decided_before_this_was_recorded": by.get("unknown", 0),
        # True only when nothing on the map got there without a person AND
        # there is no unaudited backlog hiding in 'unknown'.
        "every_public_sighting_human_decided": (
            total > 0 and by.get("auto", 0) == 0 and by.get("unknown", 0) == 0),
    }


def retracted_with_photo() -> list[dict]:
    """Retracted sightings that still carry a stored photograph.

    A retraction demotes the row and drops the plate text, but it never touched
    the picture - and /snap/<name> serves any file by name, so the photograph
    of a vehicle the map no longer claims is government stayed fetchable by
    direct URL. Unlinked is not deleted. This is what that leftover set is.
    """
    rows = connect().execute(
        "SELECT id, ts, node_id, tier, vclass, vclass_why, snap, reviewed_at "
        "FROM sightings WHERE reviewed='retracted' AND snap IS NOT NULL "
        "AND snap != '' ORDER BY reviewed_at DESC, ts DESC").fetchall()
    return [dict(r) for r in rows]


def public_review_queue_rows(limit: int = 200) -> list[dict]:
    """Public-tier sightings ordered for the operator-local review queue."""
    rows = connect().execute(
        "SELECT * FROM sightings WHERE tier='public' "
        "ORDER BY (reviewed IS NOT NULL), ts DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def private_unreviewed_since(cutoff: float, limit: int = 400) -> list[dict]:
    """Private-tier, unreviewed sightings newer than cutoff, for 'missed' scoring."""
    rows = connect().execute(
        "SELECT * FROM sightings WHERE tier='private' AND ts > ? "
        "AND reviewed IS NULL ORDER BY ts DESC LIMIT ?",
        (cutoff, int(limit))).fetchall()
    return [dict(r) for r in rows]


def clear_snap(sighting_id: int) -> Optional[str]:
    """Forget a sighting's photograph. Returns the filename it used to hold.

    Only the reference is cleared here; deleting the file is the caller's job,
    and it does it AFTER this succeeds. That order matters: a cleared row with
    an orphan file on disk is a tidy-up problem, while a deleted file still
    referenced by a row is a broken image on somebody's screen.
    """
    conn = connect()
    row = conn.execute("SELECT snap FROM sightings WHERE id=?",
                       (int(sighting_id),)).fetchone()
    if not row or not row["snap"]:
        return None
    conn.execute("UPDATE sightings SET snap=NULL WHERE id=?", (int(sighting_id),))
    conn.commit()
    return str(row["snap"])


def set_snap(sighting_id: int, name: str) -> None:
    """Point a sighting at a (usually replacement) photograph filename.

    Mirrors clear_snap's shape but writes rather than clears. The caller is
    responsible for any old-file cleanup and for snap_held bookkeeping.
    """
    conn = connect()
    conn.execute("UPDATE sightings SET snap=? WHERE id=?", (name, int(sighting_id)))
    conn.commit()


def sighting_contribution_stats(node_ids: Optional[list[str]]) -> dict:
    """Counts, never rows: how many sightings/cameras/published a scope has.

    node_ids=None means no node filter (pool-wide scope). An empty list means
    "no cameras in scope" and the caller should treat that as zero without
    querying, but this function still answers it correctly if asked.
    """
    conn = connect()
    if node_ids is None:
        where, args = "1=1", ()
    else:
        nodes = [n for n in node_ids if n]
        if not nodes:
            return {"cams": 0, "n": 0, "pub": 0}
        where = "node_id IN (%s)" % ",".join("?" * len(nodes))
        args = tuple(nodes)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, "
        f"SUM(CASE WHEN tier='public' THEN 1 ELSE 0 END) AS pub, "
        f"COUNT(DISTINCT node_id) AS cams "
        f"FROM sightings WHERE {where}", args).fetchone()
    return {
        "cams": int(row["cams"] or 0),
        "n": int(row["n"] or 0),
        "pub": int(row["pub"] or 0),
    }


def resolve_reports(sighting_id: int) -> None:
    """Clear a sighting's open flags. Called when the operator reviews it, so
    acting on a flag is what takes it out of the queue."""
    conn = connect()
    conn.execute("UPDATE reports SET resolved=? "
                 "WHERE sighting_id=? AND resolved IS NULL", (now(), sighting_id))
    conn.commit()


# --------------------------------------------------------------------------
# Reviewer tokens (multi-reviewer access, separate from the operator secret).
# --------------------------------------------------------------------------
def add_review_token(token_hash: str, label: str, scope: str,
                     nodes: str, created_by: str) -> int:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO review_tokens(token_hash,label,scope,nodes,created_at,created_by) "
        "VALUES(?,?,?,?,?,?)",
        (token_hash, label, scope, nodes, now(), created_by))
    conn.commit()
    return int(cur.lastrowid)


def review_token_by_hash(token_hash: str) -> Optional[dict]:
    r = connect().execute(
        "SELECT * FROM review_tokens WHERE token_hash=? AND revoked_at IS NULL",
        (token_hash,)).fetchone()
    return dict(r) if r else None


def touch_review_token(tid: int) -> None:
    conn = connect()
    conn.execute("UPDATE review_tokens SET last_used_at=? WHERE id=?", (now(), tid))
    conn.commit()


def revoke_review_token(tid: int) -> bool:
    conn = connect()
    cur = conn.execute(
        "UPDATE review_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), tid))
    conn.commit()
    return cur.rowcount > 0


def gov_heat() -> list[dict]:
    """Every confirmed government sighting ever, aggregated to a ~100m grid with
    counts - the cumulative "where patrols have been" layer, for spotting the
    hotspots. These positions are already the published public record; rounding
    to a grid adds a little fuzz on top and keeps the payload small as it grows.
    """
    rows = connect().execute(
        "SELECT lat, lon FROM sightings "
        "WHERE tier='public' AND reviewed='confirmed' "
        "AND lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    cells: dict = {}
    for r in rows:
        key = (round(r["lat"], 3), round(r["lon"], 3))
        cells[key] = cells.get(key, 0) + 1
    return [{"lat": k[0], "lon": k[1], "n": v} for k, v in cells.items()]


def add_driver_report(lat: float, lon: float, kind: str = "police",
                      ttl_s: float = 2700) -> int:
    """Record a live driver report of a patrol at a point. Expires by itself.

    🚨 THE POINT IS PUT ON A ROAD BEFORE IT IS STORED, AND IT IS STORED HERE.
    This used to write the phone's raw GPS - fifteen decimal places of it - and
    serve that to anyone. Every other position on this map is protected: node
    positions are jittered 60 m, sightings are placed along an 80 m road span.
    This one was not, and the schema's own defence ("only the point they tapped,
    which is about the patrol, not them") holds ONLY while the reporter is
    driving past. Somebody tapped it from inside their house, and the map
    published their house.

    A patrol is on a road. Snapping to the nearest one makes the claim MORE
    accurate and removes the problem entirely - the same trade road.py already
    made for cameras. If the road lookup is unavailable the point is jittered by
    the node budget instead, so the fallback is still not a raw position.

    Enforced at this choke point rather than in the handler, for the reason the
    tier gate lives in insert_sighting: a rule each caller has to remember is a
    rule that will be forgotten by the second caller.
    """
    lat, lon = float(lat), float(lon)
    try:
        import road
        snapped = road.snap_point(lat, lon, f"drive:{now()}:{lat:.4f},{lon:.4f}")
        if snapped:
            lat, lon = snapped
        else:
            import nodes as _nodes
            lat, lon = _nodes.jitter_position(
                lat, lon, float(CONFIG.get("node_position_jitter_m", 60)))
    except Exception:
        # 🚨 ROUNDING WAS NOT A FLOOR, IT WAS A HOLE.
        # 4 decimal places is ~11 m - which is WEAKER than the 60 m jitter this
        # same function applies when the road lookup merely returns nothing, and
        # far weaker than the 80 m span every other position on the map gets.
        # So the exception path published a reporter's position more precisely
        # than the success path did, and the comment called that "the floor".
        #
        # It is reachable: road.fetch_ways does not catch http.client
        # exceptions, so an Overpass IncompleteRead - ordinary under load -
        # lands here rather than in the None branch below.
        #
        # Jitter, always. A positioning failure must never be the most revealing
        # outcome available.
        try:
            import nodes as _nodes
            lat, lon = _nodes.jitter_position(
                lat, lon, float(CONFIG.get("node_position_jitter_m", 60)))
        except Exception:
            # Even the jitter helper failing must not publish a raw point.
            # Two decimal places is ~1.1 km: useless for a live patrol alert,
            # which is the correct way to fail.
            lat, lon = round(lat, 2), round(lon, 2)
    lat, lon = round(lat, 6), round(lon, 6)

    conn = connect()
    t = now()
    cur = conn.execute(
        "INSERT INTO driver_reports(ts,lat,lon,kind,expires) VALUES(?,?,?,?,?)",
        (t, lat, lon, kind, t + float(ttl_s)))
    conn.commit()
    return int(cur.lastrowid)


def active_driver_reports() -> list[dict]:
    prune_driver_reports()
    rows = connect().execute(
        "SELECT id,ts,lat,lon,kind,confirms,denies FROM driver_reports "
        "WHERE expires > ? ORDER BY ts DESC", (now(),)).fetchall()
    return [dict(r) for r in rows]


def vote_driver_report(rid: int, still_there: bool, ttl_s: float = 2700) -> bool:
    """A passing driver agrees ('still there', extends it) or disagrees ('gone',
    and enough of those retire it early)."""
    conn = connect()
    r = conn.execute("SELECT confirms,denies FROM driver_reports WHERE id=?",
                     (rid,)).fetchone()
    if not r:
        return False
    if still_there:
        conn.execute("UPDATE driver_reports SET confirms=confirms+1, expires=? "
                     "WHERE id=?", (now() + float(ttl_s), rid))
    else:
        d = r["denies"] + 1
        # Two independent "it's gone" reports retire it now.
        if d >= 2:
            conn.execute("UPDATE driver_reports SET denies=?, expires=? WHERE id=?",
                         (d, now() - 1, rid))
        else:
            conn.execute("UPDATE driver_reports SET denies=? WHERE id=?", (d, rid))
    conn.commit()
    return True


def prune_driver_reports() -> None:
    conn = connect()
    conn.execute("DELETE FROM driver_reports WHERE expires < ?", (now() - 3600,))
    conn.commit()


def list_review_tokens(include_revoked: bool = True) -> list[dict]:
    q = "SELECT id,label,scope,nodes,created_at,created_by,revoked_at,last_used_at FROM review_tokens"
    if not include_revoked:
        q += " WHERE revoked_at IS NULL"
    return [dict(r) for r in connect().execute(q + " ORDER BY created_at DESC").fetchall()]


def set_sighting_desc(sighting_id: int, body: Optional[str] = None,
                      color: Optional[str] = None) -> None:
    """Operator correction of the cosmetic description (e.g. the detector said
    'motorcycle' for an SUV). Only the descriptive fields - never the class, the
    plate, or the position, which have their own review paths."""
    sets, args = [], []
    if body is not None:
        sets.append("body=?"); args.append(body.strip() or None)
    if color is not None:
        sets.append("color=?"); args.append(color.strip() or None)
    if not sets:
        return
    args.append(sighting_id)
    conn = connect()
    conn.execute(f"UPDATE sightings SET {', '.join(sets)} WHERE id=?", args)
    conn.commit()


def set_superseded(old_id: str, new_id: str) -> None:
    """Mark a node as retired in favour of the one the camera moved to.

    Never overwrites an existing pointer: a camera moved twice retires A->B and
    then B->C, and rewriting A to point at C would erase the step that actually
    happened. The chain is the history.
    """
    conn = connect()
    conn.execute("UPDATE nodes SET superseded_by=? "
                 "WHERE id=? AND (superseded_by IS NULL OR superseded_by='')",
                 (str(new_id), str(old_id)))
    conn.commit()


def set_publish_span(node_id: str, on: bool) -> None:
    """Record whether this camera's owner agrees to publish its watched road."""
    conn = connect()
    conn.execute("UPDATE nodes SET publish_span=? WHERE id=?",
                 (1 if on else 0, str(node_id)))
    conn.commit()


def set_snap_held(sighting_id: int, held_name: Optional[str],
                  snap_name: Optional[str] = None) -> None:
    """Swap a sighting's photograph between served and held.

    The two columns are written together on purpose. `snap` is the name the map
    serves and `snap_held` is the name sitting in core.HELD, and a row carrying
    both would be claiming the same picture is in two places - one of which is
    not true, and whichever half a later reader trusts decides whether a held
    photograph is quietly served again.
    """
    conn = connect()
    conn.execute(
        "UPDATE sightings SET snap=?, snap_held=?, held_at=? WHERE id=?",
        (snap_name, held_name, now() if held_name else None, int(sighting_id)))
    conn.commit()


def held_photos(node_ids: Optional[list] = None, limit: int = 200) -> list:
    """Sightings whose photograph is held, oldest first.

    ⚠️ OLDEST FIRST, unlike every other queue here. A held photo is a published
    claim with its evidence missing, so the queue is a debt and the oldest debt
    is the one to pay - the reverse of the review pen, where the newest crop is
    the one still worth judging.
    """
    conn = connect()
    where, args = "s.snap_held IS NOT NULL", []
    if node_ids is not None:
        if not node_ids:
            return []
        where += " AND s.node_id IN (%s)" % ",".join("?" * len(node_ids))
        args.extend(node_ids)
    args.append(int(limit))
    rows = conn.execute(
        f"SELECT s.*, n.name AS node_name FROM sightings s "
        f"LEFT JOIN nodes n ON n.id = s.node_id "
        f"WHERE {where} ORDER BY COALESCE(s.held_at, s.ts) ASC LIMIT ?",
        args).fetchall()
    return [dict(r) for r in rows]


def snap_referenced_elsewhere(name: str, except_id: int) -> bool:
    """Does any OTHER sighting point at this snapshot file?

    Names carry a random token so two stores never collide, but a re-crop or a
    republish can leave two rows pointing at one file, and deleting a file that
    another live claim is still serving replaces a privacy problem with a
    broken photograph on someone else's sighting. Checked before every unlink.
    """
    if not name:
        return False
    conn = connect()
    r = conn.execute(
        "SELECT 1 FROM sightings WHERE id<>? AND (snap=? OR snap_held=?) LIMIT 1",
        (int(except_id), name, name)).fetchone()
    return r is not None


def promote_sighting(sighting_id: int, vclass: str = "police",
                     conf: Optional[float] = None, why: str = "",
                     decided_by: str = "human") -> None:
    """Put a wrongly-private sighting onto the public map.

    The mirror of a retraction, and needed for the same reason: the classifier
    can be wrong in both directions, and only one of them was correctable.
    The first genuine patrol car this network ever saw sat in the private tier
    because a gate was reading a field nothing populated - a bug, not a policy
    decision - and there was no way to say so.

    ⚠️ THE PLATE DOES NOT COME BACK. A private-tier snapshot has the plate
    destroyed in its pixels at capture time and the readable text was never
    stored, so a promoted sighting is public with no plate. That is rule 2
    working exactly as intended: a public SIGHTING carries no identifier, and
    the strict bar exists only for publishing plate TEXT. Nothing here can
    resurrect an identifier, which is the correct thing for this function to
    be incapable of.
    """
    # 🚨 THE POLICY CHECK LIVES HERE, BECAUSE THIS IS THE ONLY CHOKEPOINT.
    #
    # This function wrote tier='public' unconditionally and never consulted
    # `public_tiers`. Seven call sites reach it - node_label, review_api,
    # hub, box_publish - and each was enforcing the rule separately, which
    # means each could forget it separately. review_api already stopped
    # publishing `gov` after gov_dot put bin lorries on a police map; the other
    # paths were relying on their own copy of that judgement.
    #
    # A rule enforced in four places is a rule with four chances to be missed.
    # The tier is decided by privacy.tier_for, the same function the ingest path
    # uses, so there is exactly one answer to "may this class be public".
    #
    # ⚠️ Refuses rather than silently demoting. A caller asking to publish a
    # council truck has a bug in it, and quietly storing something other than
    # what it asked for would hide that bug rather than surface it.
    import privacy
    if privacy.tier_for(vclass) != "public":
        raise ValueError(
            f"refusing to publish vclass={vclass!r}: public_tiers is "
            f"{CONFIG.get('public_tiers')}, so this class is private. "
            f"Record the judgement instead (review_sighting).")

    # `decided_by` defaults to 'human' because every caller that existed when
    # this was written was a human acting deliberately; an automated caller has
    # to say so explicitly rather than inheriting the benefit of the doubt.
    conn = connect()
    cur = conn.execute("""UPDATE sightings
                    SET tier='public', vclass=?, vclass_conf=?, vclass_why=?,
                        reviewed='confirmed', reviewed_at=?, decided_by=?
                    WHERE id=?""",
                 (vclass, conf,
                  why or "confirmed by the camera operator as a public-tier vehicle",
                  now(), (decided_by or "human"), sighting_id))
    conn.commit()
    # 🚨 A HUMAN'S JUDGEMENT MUST NEVER BE SILENTLY DISCARDED.
    # Updating zero rows means the sighting is gone - the retention sweep can
    # delete a row while its card still sits in the review pen. The reviewer
    # sees a real crop, presses confirm, this UPDATE matches nothing, the crop
    # is deleted, an audit line is written under their name, and the caller
    # returns {"ok": true}. They are told their decision landed when it was
    # destroyed. Raising is the only honest answer; the caller can decide what
    # to tell them, but it must not be "done".
    if cur.rowcount == 0:
        raise LookupError(
            f"sighting {sighting_id} no longer exists - nothing was published. "
            f"It was most likely removed by the retention sweep while still in "
            f"the review pen.")


def review_stats() -> dict:
    c = connect()
    one = lambda q, a=(): c.execute(q, a).fetchone()[0]
    # ⚠️ 'merged' IS NOT A VERDICT. Rows folded into another pass are marked
    # reviewed so they drop out of the queue, and marking them 'confirmed' put
    # duplicates in the confirmed count - the header said 8 confirmed while the
    # page showed 6 cards. A field that means "has been dealt with" and a field
    # that means "a human judged this true" are different fields.
    return {
        "pending":   one("SELECT COUNT(*) FROM sightings WHERE tier='public' AND reviewed IS NULL"),
        "confirmed": one("SELECT COUNT(*) FROM sightings WHERE reviewed='confirmed'"),
        "retracted": one("SELECT COUNT(*) FROM sightings WHERE reviewed='retracted'"),
        "merged":    one("SELECT COUNT(*) FROM sightings WHERE reviewed='merged'"),
    }


def leaderboard(hours: int = 24, limit: int = 25) -> list[dict]:
    """Most-seen public-tier vehicles. The patrol-pattern view.

    🚨 THIS IS A READ PATH AND IT HAS TO OBEY THE SAME TWO GATES AS EVERY OTHER.
    It used to SELECT plate_text/plate_state AND the stable plate_hash for ALL
    public rows, with no `reviewed='confirmed'` filter and never routed through
    privacy.redact - the one path that skipped both. That served the plate text
    of UNCONFIRMED public rows (which redact deliberately withholds until a human
    confirms, because the classifier is ~95% precision) and handed out the
    durable cross-day identifier that the per-day hash alias exists to deny.

    Fixed two ways: only CONFIRMED public rows are counted, so a plate shown here
    is one a human already agreed is a government vehicle - the same bar
    /api/plate search uses; and the raw plate_hash is used only to GROUP,
    server-side, and never leaves this function. Nothing here can become a
    tracking key or expose an unreviewed plate.
    """
    since = now() - hours * 3600
    rows = connect().execute("""
        SELECT plate_text, plate_state, vclass,
               COUNT(*) AS hits,
               COUNT(DISTINCT node_id) AS cameras,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM sightings
        WHERE tier = 'public' AND reviewed = 'confirmed' AND ts > ?
        GROUP BY plate_hash
        ORDER BY hits DESC LIMIT ?""", (since, limit)).fetchall()
    return [dict(r) for r in rows]

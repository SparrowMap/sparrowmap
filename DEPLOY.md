# Putting SparrowMap on the box

Two machines, and the split is the whole security design.

```
  HOME (your hub)                         BOX  (sparrowmap.com)
  ───────────────                         ────────────────────
  full data, private tier, images         PUBLIC MIRROR
  training crops, review pages            public record only
  operator token                          no operator routes at all
  the detector                            volunteers post here
        └──────── you review here ────────────────┘
```

**A total compromise of the box hands the attacker the published government
record, which is already published.** Not because the box is impregnable -
nothing is - but because the sensitive data is not on it. See `mirror.py` and
`THREAT_MODEL.md`.

---

## 1. Contributors need nothing but a browser

There is one page: **`/node`**. It works on a phone in a window, a laptop with
its webcam, or a desktop with a USB camera. Verified on desktop - every
capability it needs is present: `getUserMedia`, Wake Lock, WebAssembly, and it
now offers a camera picker when a machine has more than one.

Nothing to install, no account, no app store. Open the page, name the camera,
press start.

The detector - a 10 MB model plus a 11 MB runtime - downloads once and is
cached by the browser. **No video and no full frame ever leaves the device.**
Each vehicle is uploaded as one crop, shrunk below plate legibility, about 5 KB.

---

## 2. Box setup, in order

**Do these before DNS points anywhere.** A domain resolving to an unhardened
box is found by scanners within hours.

```bash
# 1. A user for the app. Not root, no login shell.
adduser --system --group --home /opt/sparrow sparrow

# 2. SSH: keys only. Password auth on a public IP is brute-forced constantly.
#    In /etc/ssh/sshd_config:
#      PasswordAuthentication no
#      PermitRootLogin no
systemctl restart sshd

# 3. Firewall, default deny.
ufw default deny incoming
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
ufw enable
```

> 🚨 **8150 and 8151 must NOT be reachable from the internet.** The proxy
> reaches the hub on localhost. If the port is exposed directly, every header,
> the rate limiter and the auth cookie can be bypassed by going around the
> proxy - which is the single easiest mistake to make here.

```bash
# 4. Patch automatically. Most real compromises are a known CVE nobody applied.
apt install unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades

# 5. fail2ban on sshd, and on the proxy's 401/429 responses.
apt install fail2ban
```

**Then the app:**

```bash
cp config.launch.json config.json      # mirror mode, auth on, public tier OFF
python hub.py                          # writes data/operator.token on first run
```

Read `data/operator.token` once, keep it in a password manager, and note that
rotating it is `rm` plus a restart.

---

## 3. The proxy

```nginx
server {
    listen 443 ssl http2;
    server_name sparrowmap.com;

    # 🚨 TURN THE ACCESS LOG OFF.
    # Everything the app does to avoid keeping a record of who looked at what
    # is undone by this one line left at its default. A log of IP + URL is a
    # log of who searched which plate.
    access_log off;

    # The hub sets its own CSP, HSTS-compatible headers and no-referrer.
    # Do not add a second set here; two policies that disagree is how one of
    # them silently wins.
    location / {
        proxy_pass http://127.0.0.1:8150;
        proxy_set_header Host $host;
        # Deliberately NOT setting X-Forwarded-For. The app trusts the socket
        # for nothing security-critical, and a forwarded header it does not
        # need is a header that can be spoofed into an audit log.
    }
}
```

Certificates: `certbot --nginx -d sparrowmap.com`. Then set `behind_tls: true`
so the operator cookie gets `Secure`.

---

## 4. Turning it on, in the right order

The order matters. Each step is reversible; the last one is the only one that
makes a public claim.

1. **Box up, mirror mode, `publish_public_tier: false`.** The map is live and
   shows nothing yet. Nothing can be wrong because nothing is asserted.
2. **Point your home detector at the box** and let it run a few days.
3. **Read the review queue at home.** If the head is publishing things you
   would retract, it is not ready - `data/models/vehicle_head.npz` is the only
   file that has to move to change its mind.
4. **`publish_public_tier: true`.** Now the map asserts.
5. **Invite people.** config.launch.json ships `auto_approve_nodes: true` (the
   live box's setting since 2026-10-28), so a new camera posts at once; set it
   false if every new camera should wait for your approval.

---

## 5. Backups, and what a bad day looks like

* Back up `data/` from **home**, encrypted, off the machine, and **test a
  restore**. An untested backup is a belief.
* The box holds nothing you would cry over. That is the point. If it is
  compromised: rebuild it, rotate the operator token, revoke any node tokens
  you did not issue.
* Every submission is attributed to a node token, so abuse is fixed by
  revoking one node - not by rebuilding the map.

---

## 6. What is deliberately not here

* **No accounts, no CAPTCHA.** One operator and manual node approval; an
  account system would add attack surface, not remove it.
* **No analytics, no CDN, no third-party anything.** The CSP is strict enough
  to contain an injection precisely because there is nothing external to allow.
* **No review on the box.** Judging a claim needs the evidence, and the
  evidence stays home.

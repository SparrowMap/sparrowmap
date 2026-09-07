/* Headless Sparrow Send client for the @claude identity.
 *
 * Same end-to-end crypto as the browser app - it just loads the very same
 * sparrowsend-ratchet.js and sparrowsend-pow.js and runs them under Node's
 * WebCrypto. It holds ONE identity, polls the mailbox, decrypts, and prints any
 * NEW inbound message as a single stdout line (so a Monitor can wake the
 * session), and can send replies (with proof-of-work). State lives in a local
 * JSON file so a conversation survives between runs.
 *
 * Commands:
 *   node sparrow_claude.js init                 - make an identity
 *   node sparrow_claude.js whoami               - print address/handle/fingerprint
 *   node sparrow_claude.js claim <handle>       - claim a handle
 *   node sparrow_claude.js release <handle>     - release a handle
 *   node sparrow_claude.js poll                 - fetch+decrypt; print NEW msgs
 *   node sparrow_claude.js send <@handle|mb> <text...>
 */
"use strict";
// Node 18+ exposes a global WebCrypto `crypto`; ratchet.js uses globalThis.crypto.subtle.
if(!globalThis.crypto){ globalThis.crypto = require("crypto").webcrypto; }
const subtle = globalThis.crypto.subtle;
const fs = require("fs");
const path = require("path");
const R = require("../public/sparrowsend-ratchet.js");
const P = require("../public/sparrowsend-pow.js");

const HUB = process.env.RAVEN_HUB || process.env.SPARROW_HUB || "";
const UA = "SparrowClaude/1.0";
const STATE = path.join(__dirname, ".sparrow_claude_state.json");
const enc = new TextEncoder(), dec = new TextDecoder();

function requireHub(){
  if(!HUB){
    throw new Error("No RavenMap hub configured. Set RAVEN_HUB. Legacy SPARROW_HUB is also accepted.");
  }
  return HUB;
}

function b64u(b){ return Buffer.from(b).toString("base64").replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,""); }
function ub64u(s){ s=s.replace(/-/g,"+").replace(/_/g,"/"); while(s.length%4)s+="="; return new Uint8Array(Buffer.from(s,"base64")); }
function hex(b){ b=new Uint8Array(b); let o=""; for(let i=0;i<b.length;i++)o+=("0"+b[i].toString(16)).slice(-2); return o; }
async function sha256(bytes){ return new Uint8Array(await subtle.digest("SHA-256", bytes)); }

function loadState(){ let s; try{ s=JSON.parse(fs.readFileSync(STATE,"utf8")); }catch(e){ s={}; }
  return Object.assign({id:null, contacts:[], rat:{}, seen:{}, handle:null, allowFrom:[]}, s); }
// 🚨 ATOMIC write. The old plain writeFileSync left the state file as 3788
// bytes of NULs after an unclean shutdown mid-write, which zeroed the identity
// (private keys gone, unrecoverable). Write a temp then rename (atomic on the
// same filesystem) so a crash leaves either the old file or the new one, never
// a half/zeroed one. Also keep a .bak of the last good state.
// Sleep synchronously (saveState runs in sync paths, so no await).
function sleepSync(ms){ try{ Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,ms); }catch(e){ const t=Date.now(); while(Date.now()-t<ms){} } }
function saveState(s){
  const json = JSON.stringify(s);
  const tmp = STATE + ".tmp";
  fs.writeFileSync(tmp, json);
  try { if (fs.existsSync(STATE)) fs.copyFileSync(STATE, STATE + ".bak"); } catch(e){}
  // 🚨 WINDOWS: renameSync over an existing file throws EPERM/EBUSY/EACCES when the
  // target is momentarily held open - by the .bak copy just above, an AV/indexer, or
  // the Monitor's concurrent poll. The lock file serialises OUR processes, but not the
  // OS, so the swap still loses races (a send burst racing a poll threw EPERM once). It
  // is transient: retry briefly. The write itself never fails; only the atomic swap is
  // contended, and .bak above is the recovery copy for the theoretical last-resort tear.
  for (let i = 0; ; i++) {
    try { fs.renameSync(tmp, STATE); return; }
    catch (e) {
      if ((e.code === "EPERM" || e.code === "EBUSY" || e.code === "EACCES") && i < 25) { sleepSync(40); continue; }
      // Last resort after ~1s of contention: overwrite in place so the ratchet advance
      // is not lost. Non-atomic, but .bak holds the last good state if this ever tears.
      try { fs.writeFileSync(STATE, json); try{ fs.unlinkSync(tmp); }catch(_){ } return; }
      catch (_) { throw e; }
    }
  }
}

// The poll loop and a reply run as separate processes but share the per-contact
// ratchet, so a concurrent read-modify-write would corrupt it. Serialise with a
// lock file (stale lock older than 15s is broken, in case a process died).
const LOCK = STATE + ".lock";
const nap = ms => new Promise(r=>setTimeout(r,ms));
async function lock(){ const s=Date.now(); for(;;){ try{ fs.writeFileSync(LOCK,String(process.pid),{flag:"wx"}); return; }
  catch(e){ try{ if(Date.now()-fs.statSync(LOCK).mtimeMs>15000) fs.unlinkSync(LOCK); }catch(_){ } await nap(60); } } }
function unlock(){ try{ fs.unlinkSync(LOCK); }catch(e){} }

async function mkKeys(){
  return { sign: await subtle.generateKey({name:"ECDSA",namedCurve:"P-256"},true,["sign","verify"]),
           dh:   await subtle.generateKey({name:"ECDH",namedCurve:"P-256"},true,["deriveBits"]) };
}
async function exportID(k){ return {
  sPriv: await subtle.exportKey("jwk",k.sign.privateKey), sPub: await subtle.exportKey("jwk",k.sign.publicKey),
  ePriv: await subtle.exportKey("jwk",k.dh.privateKey),   ePub: await subtle.exportKey("jwk",k.dh.publicKey) }; }
async function importID(j){ return {
  sign:{ privateKey: await subtle.importKey("jwk",j.sPriv,{name:"ECDSA",namedCurve:"P-256"},true,["sign"]),
         publicKey:  await subtle.importKey("jwk",j.sPub,{name:"ECDSA",namedCurve:"P-256"},true,["verify"]) },
  dh:{   privateKey: await subtle.importKey("jwk",j.ePriv,{name:"ECDH",namedCurve:"P-256"},true,["deriveBits"]),
         publicKey:  await subtle.importKey("jwk",j.ePub,{name:"ECDH",namedCurve:"P-256"},true,[]) } }; }
async function finishID(keys){
  const sraw = new Uint8Array(await subtle.exportKey("raw",keys.sign.publicKey));
  const eraw = new Uint8Array(await subtle.exportKey("raw",keys.dh.publicKey));
  const mb = hex(await sha256(sraw)).slice(0,32);
  const address = b64u(enc.encode(JSON.stringify({s:b64u(sraw), e:b64u(eraw)})));
  const dhIdentity = { priv: await subtle.exportKey("jwk",keys.dh.privateKey), pub: R.b64(eraw) };
  return { sign:keys.sign, dh:keys.dh, sPubRaw:sraw, ePubRaw:eraw, mailbox:mb, address, dhIdentity };
}
function decodeAddress(a){ const j=JSON.parse(dec.decode(ub64u(a.trim()))); return {sRaw:ub64u(j.s), eRaw:ub64u(j.e)}; }
async function mailboxOf(sRaw){ return hex(await sha256(sRaw)).slice(0,32); }
function fp(mb){ mb=(mb||"").toUpperCase(); return mb.slice(0,4)+" "+mb.slice(4,8)+" "+mb.slice(8,12); }

async function api(pathname, body){
  const res = await fetch(HUB+pathname, {method:"POST", headers:{"Content-Type":"application/json","User-Agent":UA}, body:JSON.stringify(body)});
  const j = await res.json().catch(()=>({}));
  return {ok:res.ok, status:res.status, j};
}

async function loadID(st){
  if(!st.id) throw new Error("no identity - run: node sparrow_claude.js init");
  return finishID(await importID(st.id));
}

// ---- send (mirrors send.html postEnv, with progressive-PoW escalation) ----
async function postEnv(mb, envStr, mid, pow){
  const delays=[0,700,1800,3500]; let esc=0, last="relay unreachable";
  const body=()=>JSON.stringify({to:mb, env:envStr, mid, pt:pow&&pow.ts, pn:pow&&pow.nonce});
  for(let i=0;i<delays.length;i++){
    if(delays[i]) await new Promise(r=>setTimeout(r,delays[i]));
    try{
      const res=await fetch(HUB+"/api/send/put",{method:"POST",headers:{"Content-Type":"application/json","User-Agent":UA},body:body()});
      if(res.ok) return true;
      const j=await res.json().catch(()=>({})); last=j.error||("http "+res.status);
      if(res.status===400 && j.need_bits && esc<6){ esc++; pow=await P.stamp(mb, envStr, j.need_bits); i--; continue; }
      if(res.status!==429 && res.status<500) throw new Error(last);
    }catch(e){ last=e.message||String(e); }
  }
  throw new Error(last);
}
async function sendTo(ID, st, ct, obj){
  let rs = st.rat[ct.mb];
  if(!rs){ rs = await R.initSender(ID.dhIdentity, R.b64(ub64u(ct.eRaw))); }
  const msg = await R.encrypt(rs, enc.encode(JSON.stringify(obj)));
  st.rat[ct.mb] = rs;
  const mid = b64u(globalThis.crypto.getRandomValues(new Uint8Array(12)));
  const envStr = JSON.stringify({from:ID.address, mid, r:msg});
  const pow = await P.stamp(ct.mb, envStr, P.POW_BITS);
  await postEnv(ct.mb, envStr, mid, pow);
}

// ---- receive (mirrors send.html poll + ingest) ----
async function poll(ID, st, onMsg){
  const ch = await api("/api/send/challenge", {mb:ID.mailbox});
  if(!ch.j.challenge) return;
  const sig = await subtle.sign({name:"ECDSA",hash:"SHA-256"}, ID.sign.privateKey, enc.encode(ch.j.challenge));
  const got = await api("/api/send/get", {mb:ID.mailbox, pk:b64u(ID.sPubRaw), ch:ch.j.challenge, sig:b64u(sig)});
  const msgs = (got.j && got.j.msgs) || [];
  for(const envStr of msgs){
    let env; try{ env=JSON.parse(envStr); }catch(e){ continue; }
    if(!env.from||!env.r) continue;
    let from; try{ from=decodeAddress(env.from); }catch(e){ continue; }
    const mb = await mailboxOf(from.sRaw);
    // 🔒 Allowlist: if set, silently ignore anyone who isn't on it (their
    // message was already drained from the relay, it just never reaches Claude).
    if(st.allowFrom && st.allowFrom.length && st.allowFrom.indexOf(mb)<0) continue;
    if(env.mid){ const seen=st.seen[mb]||[]; if(seen.indexOf(env.mid)>=0) continue; seen.push(env.mid); if(seen.length>500)seen.splice(0,seen.length-500); st.seen[mb]=seen; }
    let ct = st.contacts.find(c=>c.mb===mb);
    if(!ct){ ct={name:"", mb, sRaw:b64u(from.sRaw), eRaw:b64u(from.eRaw), verified:false, unknown:true}; st.contacts.push(ct); }
    let rs = st.rat[mb]; if(!rs){ rs = await R.initReceiver(ID.dhIdentity, R.b64(from.eRaw)); }
    try{
      const pt = await R.decrypt(rs, env.r); st.rat[mb]=rs;
      const o = JSON.parse(dec.decode(pt));
      const text = o.t==="img" ? "[photo]" : (o.x||"");
      onMsg({mb, text, kind:o.t||"text", data:(o.t==="img"?o.d:null)});
    }catch(e){ /* undecryptable; leave state as-is */ }
  }
}

(async function main(){
  const [cmd, ...args] = process.argv.slice(2);
  const st = loadState();

  if(cmd==="init"){
    if(st.id){ console.log("identity already exists; whoami to see it"); return; }
    const keys = await mkKeys(); st.id = await exportID(keys); saveState(st);
    const ID = await finishID(keys);
    console.log("created identity  fingerprint "+fp(ID.mailbox)+"  mailbox "+ID.mailbox);
    return;
  }

  const ID = await loadID(st);

  if(cmd==="whoami"){
    console.log("handle    @"+(st.handle||"(none)"));
    console.log("fingerprint "+fp(ID.mailbox));
    console.log("mailbox   "+ID.mailbox);
    console.log("address   "+ID.address);
    return;
  }
  requireHub();
  if(cmd==="claim"){
    const h=(args[0]||"").toLowerCase().replace(/^@/,"");
    const sig = await subtle.sign({name:"ECDSA",hash:"SHA-256"}, ID.sign.privateKey, enc.encode("SPH1|"+h+"|"+ID.address));
    const pow = await P.stamp(ID.mailbox, "CLAIM|"+h+"|"+ID.address, 18);
    const r = await api("/api/send/claim", {handle:h, address:ID.address, sig:b64u(sig), pt:pow.ts, pn:pow.nonce});
    if(r.ok && r.j.ok){ st.handle=r.j.handle; saveState(st); console.log("claimed @"+r.j.handle); } else console.log("claim failed: "+(r.j.error||r.status));
    return;
  }
  if(cmd==="release"){
    const h=(args[0]||st.handle||"").toLowerCase().replace(/^@/,"");
    const sig = await subtle.sign({name:"ECDSA",hash:"SHA-256"}, ID.sign.privateKey, enc.encode("SPHREL1|"+h+"|"+ID.address));
    const r = await api("/api/send/release", {handle:h, address:ID.address, sig:b64u(sig)});
    if(r.ok && r.j.ok){ if(st.handle===h)st.handle=null; saveState(st); console.log("released @"+h); } else console.log("release failed: "+(r.j.error||r.status));
    return;
  }
  if(cmd==="resetpeer"){
    // Clear the ratchet + seen-mids for a peer so the next inbound re-inits the
    // session (use after THEY delete+re-add us, or an identity rotation).
    var t=(args[0]||""); var mb;
    if(/^[0-9a-f]{32}$/.test(t)) mb=t;
    else { var h=t.replace(/^@/,""); var look=await (await fetch(HUB+"/api/send/handle?h="+encodeURIComponent(h),{headers:{"User-Agent":UA}})).json().catch(function(){return{};});
      if(!look.address){ console.log("no such @handle"); return; } var d=decodeAddress(look.address); mb=await mailboxOf(d.sRaw); }
    await lock();
    try{ var s2=loadState(); delete s2.rat[mb]; delete s2.seen[mb]; saveState(s2); }
    finally{ unlock(); }
    console.log("reset session with "+mb.slice(0,8)+" (bot will re-init on their next message)");
    return;
  }
  if(cmd==="allow"){
    const t=(args[0]||""); let mb;
    if(/^[0-9a-f]{32}$/.test(t)) mb=t;
    else { const h=t.replace(/^@/,""); const look=await (await fetch(HUB+"/api/send/handle?h="+encodeURIComponent(h),{headers:{"User-Agent":UA}})).json().catch(()=>({}));
      if(!look.address){ console.log("no such @handle"); return; } const d=decodeAddress(look.address); mb=await mailboxOf(d.sRaw); }
    if(st.allowFrom.indexOf(mb)<0) st.allowFrom.push(mb); saveState(st);
    console.log("allowlist (only these can reach you): "+st.allowFrom.map(x=>x.slice(0,8)).join(", "));
    return;
  }
  if(cmd==="poll"){
    await lock();
    try{ const s2=loadState(); await poll(ID, s2, (m)=>{
      let extra="";
      if(m.kind==="img" && m.data){
        const mm=/^data:(image\/[a-z]+);base64,(.*)$/i.exec(m.data);
        if(mm){ try{ const dir=path.join(__dirname,"inbox_media"); fs.mkdirSync(dir,{recursive:true});
          const ext=mm[1].split("/")[1].replace("jpeg","jpg"); const fpath=path.join(dir, Date.now()+"."+ext);
          fs.writeFileSync(fpath, Buffer.from(mm[2],"base64")); extra=" saved:"+fpath; }catch(e){ extra=" (save failed)"; } }
      }
      console.log("MSG "+m.mb.slice(0,8)+" "+m.text.replace(/\s+/g," ").slice(0,400)+extra);
    }); saveState(s2); }
    finally{ unlock(); }
    return;
  }
  if(cmd==="send"){
    let target=args[0]; const text=args.slice(1).join(" ");
    if(!target||!text){ console.log("usage: send <@handle|mailbox> <text>"); return; }
    let ct;
    if(target[0]==="@" || !/^[0-9a-f]{32}$/.test(target)){
      const h=target.replace(/^@/,"");
      const look = await (await fetch(HUB+"/api/send/handle?h="+encodeURIComponent(h),{headers:{"User-Agent":UA}})).json().catch(()=>({}));
      if(!look.address){ console.log("no such @handle"); return; }
      const d = decodeAddress(look.address); const mb = await mailboxOf(d.sRaw);
      ct = st.contacts.find(c=>c.mb===mb) || {name:h, mb, sRaw:b64u(d.sRaw), eRaw:b64u(d.eRaw), verified:false};
    } else {
      ct = st.contacts.find(c=>c.mb===target);
      if(!ct){ console.log("unknown mailbox (they must message you first, or send to their @handle)"); return; }
    }
    await lock();
    try{
      const s2 = loadState();
      // resolve/attach the contact against the FRESH state under the lock
      let c2 = s2.contacts.find(c=>c.mb===ct.mb);
      if(!c2){ c2 = ct; s2.contacts.push(c2); }
      await sendTo(ID, s2, c2, {t:"text", x:text});
      saveState(s2);
    } finally{ unlock(); }
    console.log("sent to "+(ct.name?("@"+ct.name):ct.mb.slice(0,8)));
    return;
  }
  console.log("commands: init | whoami | claim <h> | release <h> | poll | send <@handle|mb> <text>");
})().catch(e=>{ console.error("ERR "+(e.message||e)); process.exit(1); });

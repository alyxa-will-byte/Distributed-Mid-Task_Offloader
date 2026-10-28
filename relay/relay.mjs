// axr relay: runs on the KVM 2 next to the Qi gateway (behind Caddy at relay.alyxrbytes.pro).
// Devices (your laptop = "controller", helpers = "worker") connect OUT to it over a websocket, so nobody needs open ports.
// It keeps the device registry (who is paired, paused, removed) and routes messages. Job contents are end-to-end
// encrypted between devices; the relay only sees ciphertext plus a "kind" label. See ../README.md.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createHash, createPublicKey, createPrivateKey, generateKeyPairSync, randomBytes, randomInt, sign, verify, timingSafeEqual } from 'node:crypto';
import { WebSocketServer } from 'ws';

export const LIMITS = { codeTtlMs: 10 * 60_000, pingMs: 20_000, maxMsg: 2 * 1024 * 1024, joinFailsPerMin: 10, nameLen: 60 };
export const ROLES = ['controller', 'worker'];
// Kinds a worker accepts only from a controller, and only while it is not paused (the relay enforces this).
const JOB_KINDS = new Set(['job', 'chunk', 'cancel']);

const WORDS = ('amber arch aspen basil birch bloom brook cedar cinder clay cloud comet coral crane delta dune ember fern ' +
  'finch flint fog frost gale glade grove harbor hazel heron iris ivory jade juniper kelp lark lilac linen lotus maple ' +
  'marsh meadow mint moss nectar oak olive onyx opal orchid pearl pine plume quartz raven reed ridge river robin sage ' +
  'shale slate sparrow spruce stone storm sun thistle tide vale willow wren').split(' ');

const b64 = (b) => Buffer.from(b).toString('base64');
const sha = (s) => createHash('sha256').update(s).digest('hex');
const rawEd25519 = (pk) => createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(pk, 'base64').toString('base64url') }, format: 'jwk' });
export function verifySig(pkB64, msg, sigB64) {
  try { return verify(null, Buffer.from(msg), rawEd25519(pkB64), Buffer.from(sigB64, 'base64')); } catch { return false; }
}
const isKey = (s) => typeof s === 'string' && /^[A-Za-z0-9+/]{43}=$/.test(s);   // 32 raw bytes, base64
const clean = (s, n = LIMITS.nameLen) => String(s ?? '').replace(/[^\w .@()+-]/g, '').trim().slice(0, n);

// Durable registry: a JSON file written atomically. Survives restarts and redeploys (keep the data dir on a volume).
export function fileStore(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const file = path.join(dir, 'registry.json');
  return {
    load() { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } },
    save(state) { const tmp = `${file}.tmp`; fs.writeFileSync(tmp, JSON.stringify(state, null, 1), { mode: 0o600 }); fs.renameSync(tmp, file); },
  };
}

// The relay's own signing key. Devices pin its public key when they pair, so only this relay can tell them they were removed.
export function relayKey(dir) {
  const f = path.join(dir, 'relay-key.pem');
  let pem; try { pem = fs.readFileSync(f, 'utf8'); } catch {
    pem = generateKeyPairSync('ed25519').privateKey.export({ type: 'pkcs8', format: 'pem' });
    fs.writeFileSync(f, pem, { mode: 0o600 });
  }
  const sk = createPrivateKey(pem);
  const pk = Buffer.from(createPublicKey(sk).export({ format: 'jwk' }).x, 'base64url').toString('base64');
  return { pk, sign: (msg) => b64(sign(null, Buffer.from(msg), sk)) };
}
export const removedMessage = (id, ts) => `axr-removed:${id}:${ts}`;

export function createRelay({ store, key, adminKey, now = () => Date.now(), log = () => {} }) {
  if (!adminKey || adminKey.length < 32) throw new Error('RELAY_ADMIN_KEY must be at least 32 characters');
  const state = Object.assign({ devices: {}, removed: {} }, store.load() || {});
  const codes = new Map();          // sha(code) -> { role, expires }
  const live = new Map();           // id -> { conns: Set<ws>, status }. Workers have one connection; a controller may have several (one per `axr` command).
  const fails = new Map();          // ip -> [timestamps]
  let dirty = false;
  const persist = () => { store.save(state); dirty = false; };
  const flush = setInterval(() => { if (dirty) persist(); }, 30_000); flush.unref?.();

  const pub = (id) => {
    const d = state.devices[id]; const l = live.get(id);
    return { id, name: d.name, role: d.role, os: d.os, arch: d.arch, paused: !!d.paused, online: !!l, paired_at: d.paired_at,
      last_seen: l ? now() : d.last_seen, box_pk: d.box_pk, status: l?.status ?? null };
  };
  const list = () => Object.keys(state.devices).map(pub);
  const tell = (ws, obj) => { if (ws.readyState === 1) ws.send(JSON.stringify(obj)); };
  const tellDevice = (l, obj) => { for (const ws of l.conns) tell(ws, obj); };
  const toControllers = (obj) => { for (const [id, l] of live) if (state.devices[id]?.role === 'controller') tellDevice(l, obj); };

  function newCode(role) {
    if (!ROLES.includes(role)) throw new Error('role must be controller or worker');
    const code = Array.from({ length: 6 }, () => WORDS[randomInt(WORDS.length)]).join('-');
    const expires = now() + LIMITS.codeTtlMs;
    codes.set(sha(code), { role, expires });
    return { code, role, expires };
  }
  function takeCode(code) {
    for (const [h, c] of codes) if (c.expires < now()) codes.delete(h);
    const h = sha(String(code).trim().toLowerCase()); const c = codes.get(h);
    if (!c) return null;
    codes.delete(h); return c;     // one use only
  }
  function find(ref) {
    if (state.devices[ref]) return ref;
    const hits = Object.keys(state.devices).filter((id) => id.startsWith(ref) || state.devices[id].name.toLowerCase() === String(ref).toLowerCase());
    return hits.length === 1 ? hits[0] : null;
  }

  // Owner actions. These are the "disconnect whenever I feel like it" controls.
  function admin(op, ref) {
    const id = find(ref);
    if (!id) return { error: 'no such device (use its name or id)' };
    const d = state.devices[id], l = live.get(id);
    if (op === 'pause' || op === 'resume') { d.paused = op === 'pause'; persist(); if (l) tellDevice(l, { t: 'paused', paused: d.paused }); }
    else if (op === 'disconnect') { if (l) for (const ws of l.conns) ws.close(4001, 'disconnected by owner'); }
    else if (op === 'remove') {
      delete state.devices[id]; state.removed[id] = now(); persist();
      if (l) { const ts = now(); for (const ws of l.conns) { tell(ws, { t: 'removed', id, ts, sig: key.sign(removedMessage(id, ts)) }); ws.close(4003, 'removed'); } }
    } else return { error: 'unknown action' };
    log(`${op} ${d.name} (${id})`);
    toControllers({ t: 'presence', device: state.devices[id] ? pub(id) : { id, removed: true } });
    return { ok: true, id, op };
  }

  function rateLimited(ip) {
    const t = now(), arr = (fails.get(ip) || []).filter((x) => t - x < 60_000);
    fails.set(ip, arr); return arr.length >= LIMITS.joinFailsPerMin;
  }
  const failed = (ip) => fails.set(ip, [...(fails.get(ip) || []), now()]);

  function attach(ws, id) {
    let l = live.get(id);
    if (!l) { l = { conns: new Set(), status: null }; live.set(id, l); }
    if (state.devices[id].role === 'worker') for (const old of l.conns) if (old !== ws) { l.conns.delete(old); old.close(4000, 'replaced by a newer connection'); }
    l.conns.add(ws); ws.deviceId = id; ws.alive = true;
    state.devices[id].last_seen = now(); dirty = true;
    tell(ws, { t: 'welcome', id, role: state.devices[id].role, name: state.devices[id].name, relay_pk: key.pk, paused: !!state.devices[id].paused });
    toControllers({ t: 'presence', device: pub(id) });
    log(`online ${state.devices[id].name} (${id})`);
  }

  function onMessage(ws, msg, ip) {
    const me = ws.deviceId;
    if (!me) {
      if (msg.t === 'join') {
        if (rateLimited(ip)) return ws.close(4029, 'too many attempts, wait a minute');
        if (!isKey(msg.sign_pk) || !isKey(msg.box_pk) || !verifySig(msg.sign_pk, ws.nonce, msg.sig)) { failed(ip); return ws.close(4002, 'bad join'); }
        const c = takeCode(msg.code);
        if (!c) { failed(ip); return ws.close(4002, 'that pairing code is wrong or expired'); }
        let id; do { id = randomBytes(6).toString('hex'); } while (state.devices[id] || state.removed[id]);
        state.devices[id] = { name: clean(msg.name) || `${c.role}-${id.slice(0, 4)}`, role: c.role, os: clean(msg.os, 20), arch: clean(msg.arch, 20),
          sign_pk: msg.sign_pk, box_pk: msg.box_pk, paired_at: now(), last_seen: now(), paused: false };
        persist(); log(`paired ${state.devices[id].name} as ${c.role}`);
        return attach(ws, id);
      }
      if (msg.t === 'hello') {
        const id = String(msg.id || '');
        if (state.removed[id]) { const ts = now(); tell(ws, { t: 'removed', id, ts, sig: key.sign(removedMessage(id, ts)) }); return ws.close(4003, 'removed'); }
        const d = state.devices[id];
        if (!d) { tell(ws, { t: 'error', code: 'unknown', error: 'this relay does not know this device' }); return ws.close(4004, 'unknown device'); }
        if (!verifySig(d.sign_pk, ws.nonce, msg.sig)) { failed(ip); return ws.close(4002, 'bad signature'); }
        if (msg.os) d.os = clean(msg.os, 20);
        return attach(ws, id);
      }
      return ws.close(4002, 'say hello first');
    }
    const d = state.devices[me]; if (!d) return ws.close(4003, 'removed');
    d.last_seen = now(); dirty = true;
    switch (msg.t) {
      case 'status': { const l = live.get(me); if (l) l.status = msg.status && typeof msg.status === 'object' ? msg.status : null; return; }
      case 'devices': if (d.role !== 'controller') return tell(ws, { t: 'error', error: 'only controllers can list devices' });
        return tell(ws, { t: 'devices', req: msg.req, devices: list() });
      case 'admin': {
        if (d.role !== 'controller') return tell(ws, { t: 'error', req: msg.req, error: 'only controllers can manage devices' });
        if (msg.op === 'pair') { try { return tell(ws, { t: 'admin', req: msg.req, ...newCode(msg.role || 'worker') }); } catch (e) { return tell(ws, { t: 'admin', req: msg.req, error: e.message }); } }
        return tell(ws, { t: 'admin', req: msg.req, ...admin(msg.op, String(msg.target || '')) });
      }
      case 'leave': return admin('remove', me);
      case 'send': {
        const to = String(msg.to || ''), kind = String(msg.kind || ''), td = state.devices[to], tl = live.get(to);
        if (!td || !tl) return tell(ws, { t: 'undeliverable', to, ref: msg.ref, error: 'that device is offline' });
        if (JOB_KINDS.has(kind)) {
          if (d.role !== 'controller' || td.role !== 'worker') return tell(ws, { t: 'undeliverable', to, ref: msg.ref, error: 'jobs go from a controller to a worker' });
          if (td.paused && kind === 'job') return tell(ws, { t: 'undeliverable', to, ref: msg.ref, error: `${td.name} is paused` });
        }
        return tellDevice(tl, { t: 'msg', from: me, from_role: d.role, from_name: d.name, from_box_pk: d.box_pk, kind, data: msg.data });
      }
      default: return tell(ws, { t: 'error', error: 'unknown message' });
    }
  }

  const server = http.createServer(async (req, res) => {
    const send = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify(obj)); };
    try {
      const p = new URL(req.url, 'http://x').pathname;
      if (p === '/health') return send(200, { ok: true, online: live.size });
      if (!p.startsWith('/admin/')) return send(404, { error: 'not found' });
      const a = Buffer.from((req.headers.authorization || '').replace(/^Bearer /, '')), b = Buffer.from(adminKey);
      if (a.length !== b.length || !timingSafeEqual(a, b)) return send(401, { error: 'not allowed' });
      let body = {};
      if (req.method === 'POST') { let raw = ''; for await (const c of req) { raw += c; if (raw.length > 10_000) return send(413, { error: 'too long' }); } try { body = raw ? JSON.parse(raw) : {}; } catch { return send(400, { error: 'bad json' }); } }
      if (req.method === 'POST' && p === '/admin/pair') { try { return send(200, newCode(body.role || 'worker')); } catch (e) { return send(400, { error: e.message }); } }
      if (req.method === 'GET' && p === '/admin/devices') return send(200, { devices: list() });
      const m = req.method === 'POST' && p.match(/^\/admin\/devices\/([^/]+)\/(pause|resume|disconnect|remove)$/);
      if (m) { const r = admin(m[2], decodeURIComponent(m[1])); return send(r.error ? 404 : 200, r); }
      return send(404, { error: 'not found' });
    } catch { if (!res.headersSent) send(500, { error: 'something went wrong' }); }
  });

  const wss = new WebSocketServer({ server, path: '/ws', maxPayload: LIMITS.maxMsg });
  wss.on('connection', (ws, req) => {
    const ip = String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',')[0].trim();
    ws.nonce = randomBytes(32).toString('base64');
    ws.alive = true;
    tell(ws, { t: 'challenge', nonce: ws.nonce, relay_pk: key.pk });
    const authTimer = setTimeout(() => { if (!ws.deviceId) ws.close(4002, 'too slow'); }, 15_000);
    ws.on('pong', () => { ws.alive = true; if (ws.deviceId && state.devices[ws.deviceId]) { state.devices[ws.deviceId].last_seen = now(); dirty = true; } });
    ws.on('message', (raw) => { let msg; try { msg = JSON.parse(raw); } catch { return ws.close(4002, 'bad json'); } if (msg && typeof msg === 'object') onMessage(ws, msg, ip); });
    ws.on('close', () => {
      clearTimeout(authTimer);
      const id = ws.deviceId;
      const l = id && live.get(id);
      if (l && l.conns.delete(ws) && l.conns.size === 0) {
        live.delete(id);
        if (state.devices[id]) { state.devices[id].last_seen = now(); persist(); toControllers({ t: 'presence', device: pub(id) }); log(`offline ${state.devices[id].name} (${id})`); }
      }
    });
  });
  const beat = setInterval(() => {
    for (const ws of wss.clients) { if (!ws.alive) { ws.terminate(); continue; } ws.alive = false; try { ws.ping(); } catch { /* closing */ } }
  }, LIMITS.pingMs); beat.unref?.();

  server.on('close', () => { clearInterval(flush); clearInterval(beat); if (dirty) persist(); });
  // Shut down for a restart: tell every device to come back (1012 = service restart), then stop listening.
  const close = () => new Promise((resolve) => {
    for (const ws of wss.clients) ws.close(1012, 'relay restarting');
    setTimeout(() => { for (const ws of wss.clients) ws.terminate(); }, 2000).unref?.();
    wss.close(); server.close(() => resolve());
  });
  return { server, wss, close, newCode, admin, list, state };
}

async function cli() {
  const { RELAY_ADMIN_KEY, RELAY_PORT = '8790', RELAY_DATA = './data', RELAY_HOST = '127.0.0.1' } = process.env;
  const [cmd, ...rest] = process.argv.slice(2);
  if (cmd && cmd !== 'serve') {
    // Talk to the running relay on this server: `node relay.mjs pair controller`, `node relay.mjs devices`, `node relay.mjs remove <name>`
    const call = async (method, p, body) => {
      const r = await fetch(`http://127.0.0.1:${RELAY_PORT}${p}`, { method, headers: { authorization: `Bearer ${RELAY_ADMIN_KEY}`, 'content-type': 'application/json' }, body: body && JSON.stringify(body) });
      const j = await r.json(); if (!r.ok) { console.error(j.error || r.status); process.exit(1); } return j;
    };
    if (cmd === 'pair') { const c = await call('POST', '/admin/pair', { role: rest[0] || 'worker' }); console.log(`${c.role} pairing code (valid 10 minutes, one use):\n\n  ${c.code}\n`); return; }
    if (cmd === 'devices') { for (const d of (await call('GET', '/admin/devices')).devices) console.log(`${d.online ? 'online ' : 'offline'}  ${d.paused ? 'paused ' : '       '} ${d.role.padEnd(10)} ${d.name.padEnd(24)} ${d.id}  ${d.os}`); return; }
    if (['pause', 'resume', 'disconnect', 'remove'].includes(cmd)) { console.log(await call('POST', `/admin/devices/${encodeURIComponent(rest[0] || '')}/${cmd}`)); return; }
    console.error('usage: node relay.mjs [serve | pair controller|worker | devices | pause|resume|disconnect|remove <name>]'); process.exit(1);
  }
  if (!RELAY_ADMIN_KEY || RELAY_ADMIN_KEY.length < 32) { console.error('Set RELAY_ADMIN_KEY (32+ chars)'); process.exit(1); }
  const { server, close } = createRelay({ store: fileStore(RELAY_DATA), key: relayKey(RELAY_DATA), adminKey: RELAY_ADMIN_KEY, log: (s) => console.log(new Date().toISOString(), s) });
  server.listen(Number(RELAY_PORT), RELAY_HOST, () => console.log(`axr relay on ${RELAY_HOST}:${RELAY_PORT}`));
  const stop = () => close().then(() => process.exit(0));
  process.on('SIGTERM', stop); process.on('SIGINT', stop);
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('relay.mjs')) cli();

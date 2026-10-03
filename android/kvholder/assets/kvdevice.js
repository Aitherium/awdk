// The phone's workspace device key: Ed25519 in WebCrypto, private half non-extractable.
// It signs every dial to the workspace relay (adk.kvholder_workspace.hello_message), and its
// public half goes into the device's enrollment record when the owner's pairing code is used.
// Runs in the app's WebView and under Node (the tests pass a Map-backed store).
(function (root) {
  'use strict';
  const DOMAIN = 'aither-kvholder-hello/1';
  const hex = (b) => Array.from(new Uint8Array(b), (x) => x.toString(16).padStart(2, '0')).join('');
  const b64 = (b) => { let s = ''; const u = new Uint8Array(b); for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]); return btoa(s); };

  function idbStore(dbName) {
    const open = () => new Promise((ok, bad) => {
      const r = indexedDB.open(dbName, 1);
      r.onupgradeneeded = () => r.result.createObjectStore('kv');
      r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error);
    });
    const tx = async (mode, fn) => { const db = await open(); return new Promise((ok, bad) => {
      const t = db.transaction('kv', mode), q = fn(t.objectStore('kv'));
      t.oncomplete = () => ok(q && q.result); t.onerror = () => bad(t.error);
    }); };
    return { get: (k) => tx('readonly', (s) => s.get(k)), put: (k, v) => tx('readwrite', (s) => s.put(v, k)) };
  }

  async function open(store, subtle) {
    subtle = subtle || root.crypto.subtle;
    let kp = await store.get('ed25519');
    if (!kp) {
      kp = await subtle.generateKey({name: 'Ed25519'}, false, ['sign', 'verify']);
      await store.put('ed25519', kp);
    }
    const publicHex = hex(await subtle.exportKey('raw', kp.publicKey));
    return {
      publicHex,
      async hello(relayHost, deviceId) {
        const ts = Math.floor(Date.now() / 1000);
        const nonce = b64(root.crypto.getRandomValues(new Uint8Array(12))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
        const relay = relayHost.toLowerCase();
        const msg = [DOMAIN, relay, deviceId, String(ts), nonce].join('\n');
        const sig = await subtle.sign({name: 'Ed25519'}, kp.privateKey, new TextEncoder().encode(msg));
        return {auth: 'device', device_id: deviceId, relay, ts, nonce, sig: b64(sig)};
      },
    };
  }

  const api = {open, idbStore, DOMAIN};
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.KVDevice = api;
})(typeof window !== 'undefined' ? window : globalThis);

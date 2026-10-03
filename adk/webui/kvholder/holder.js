(function (root) {
'use strict';
const MAGIC = 0x4E544150, VERSION = 3, NR = 48, HD = 256, MAX_GROUPS = 64, MAX_DIM = 4096, V4_TAG = 4;
const T = {HELLO:1,HELLO_OK:2,CONFIG:3,APPEND:4,TRUNCATE:5,ATTN:6,ATTN_OK:7,STATS:8,OK:9,ERR:10,BYE:11,PING:12,ATTN_BIG:13};
const NEG = -3.0e38;
const enc = new TextEncoder(), dec = new TextDecoder();

const f32b = new Float32Array(1), u32b = new Uint32Array(f32b.buffer);
function h2f(h) {
  const s = (h & 0x8000) ? -1 : 1, e = (h >> 10) & 0x1f, f = h & 0x3ff;
  if (e === 0) return s * f * 5.960464477539063e-8;
  if (e === 31) return f ? NaN : s * Infinity;
  return s * Math.pow(2, e - 15) * (1 + f / 1024);
}
function f2h(v) {
  f32b[0] = v; const x = u32b[0];
  const sign = (x >>> 16) & 0x8000, ex = (x >>> 23) & 0xff;
  let e = ex - 127 + 15, m = x & 0x7fffff;
  if (ex === 0xff) return sign | 0x7c00 | (m ? 0x200 : 0);
  if (e >= 31) return sign | 0x7c00;
  if (e <= 0) {
    if (e < -10) return sign;
    m = (m | 0x800000) >> (1 - e);
    if (m & 0x1000) m += 0x2000;
    return sign | (m >> 13);
  }
  if (m & 0x1000) { m += 0x2000; if (m & 0x800000) { m = 0; e += 1; if (e >= 31) return sign | 0x7c00; } }
  return sign | (e << 10) | (m >> 13);
}
function headBytes(t, dim) { dim = dim || HD; return t === 0 ? dim * 2 : t === 1 ? dim / 32 * 34 : t === 2 ? dim / 32 * 18 : -1; }
// PATN v4 (the adk extension): CONFIG may carry a 24-byte tail (tag 4, k_dim, v_dim, rows per KV
// head, V row stride, V bytes per head) for models whose attention is not 256 wide x 48 rows.
// Every config below carries the full shape; a v3 CONFIG gets the v3 defaults.
function mkCfg(nLayer, nkv, type, kd, vd, rows) {
  kd = kd || HD; vd = vd || kd; rows = rows || NR;
  const hb = headBytes(type, kd), hbv = headBytes(type, vd);
  return {nLayer, nkv, rs: nkv * hb, hb, type, kd, vd, rows, rsv: nkv * hbv, hbv};
}
function side(cfg, which) { return which === 'v' ? [cfg.vd, cfg.rsv, cfg.hbv] : [cfg.kd, cfg.rs, cfg.hb]; }
const isPow2 = (d) => (d & (d - 1)) === 0;

// ggml rows (n x row-stride bytes) -> Float32Array [n][nkv][dim] for the K or V side
function dequant(bytes, n, cfg, which) {
  const [D, rs, hb] = side(cfg, which);
  const out = new Float32Array(n * cfg.nkv * D);
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let j = 0; j < n; j++) for (let h = 0; h < cfg.nkv; h++) {
    const base = j * rs + h * hb, o = (j * cfg.nkv + h) * D;
    if (cfg.type === 0) {
      for (let d = 0; d < D; d++) out[o + d] = h2f(dv.getUint16(base + 2 * d, true));
    } else if (cfg.type === 1) {
      for (let b = 0; b < D / 32; b++) {
        const blk = base + b * 34, dd = h2f(dv.getUint16(blk, true));
        for (let i = 0; i < 32; i++) out[o + b * 32 + i] = dd * dv.getInt8(blk + 2 + i);
      }
    } else {
      for (let b = 0; b < D / 32; b++) {
        const blk = base + b * 18, dd = h2f(dv.getUint16(blk, true));
        for (let i = 0; i < 16; i++) {
          const q = bytes[blk + 2 + i];
          out[o + b * 32 + i] = dd * ((q & 0x0f) - 8);
          out[o + b * 32 + 16 + i] = dd * ((q >> 4) - 8);
        }
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------- CPU engine
class CpuEngine {
  constructor() { this.kind = 'cpu'; }
  configure(cfg) { this.cfg = cfg; this.layers = []; for (let i = 0; i < cfg.nLayer; i++) this.layers.push({n: 0, K: [], V: [], cache: null}); }
  bytesPerKey() { return this.cfg.rs + this.cfg.rsv; }
  append(layer, n, kRaw, vRaw) { const L = this.layers[layer]; L.K.push(kRaw.slice()); L.V.push(vRaw.slice()); L.n += n; L.cache = null; }
  truncate(keep) {
    for (const L of this.layers) {
      if (L.n <= keep) continue;
      const cut = (arrs, rs) => { const all = concat(arrs); return [all.slice(0, keep * rs)]; };
      L.K = cut(L.K, this.cfg.rs); L.V = cut(L.V, this.cfg.rsv); L.n = keep; L.cache = null;
    }
  }
  held(layer) { return this.layers[layer].n; }
  kv(layer) {
    const L = this.layers[layer];
    if (!L.cache || L.cache.n !== L.n) L.cache = {n: L.n, K: dequant(concat(L.K), L.n, this.cfg, 'k'), V: dequant(concat(L.V), L.n, this.cfg, 'v')};
    return L.cache;
  }
  async attn(layer, nk, scale, q, nTok, ng) {
    const {K, V} = this.kv(layer), {nkv, kd: KD, vd: VD, rows: R} = this.cfg, rows = nkv * R;
    const O = new Float32Array(ng * rows * VD), lse = new Float32Array(ng * rows).fill(-Infinity);
    const s = new Float64Array(nk), acc = new Float64Array(VD);
    for (let g = 0; g < ng; g++) for (let h = 0; h < nkv; h++) for (let r = 0; r < R; r++) {
      const row = g * rows + h * R + r;
      if (nk === 0 || (r % 8) >= nTok) continue;
      const qo = row * KD, oo = row * VD;
      let m = -Infinity;
      for (let k = 0; k < nk; k++) {
        const ko = (k * nkv + h) * KD; let d = 0;
        for (let i = 0; i < KD; i++) d += q[qo + i] * K[ko + i];
        s[k] = d * scale; if (s[k] > m) m = s[k];
      }
      acc.fill(0); let l = 0;
      for (let k = 0; k < nk; k++) {
        const p = Math.exp(s[k] - m); l += p; const vo = (k * nkv + h) * VD;
        for (let i = 0; i < VD; i++) acc[i] += p * V[vo + i];
      }
      for (let i = 0; i < VD; i++) O[oo + i] = acc[i] / l;
      lse[row] = m + Math.log(l);
    }
    return {O, lse};
  }
  storeName() { return 'wire'; }
  describe() { return 'cpu'; }
}

// tq4 key centering (hello "tq4": "centered"; adk.kvholder does the same). A model's keys share a
// large per-head offset that the 4-bit codebook spends its levels on (Qwen3-0.6B: perplexity 37.3
// uncentered, 10.84 centered, fp32 10.27). The first APPEND to a layer after CONFIG fixes
// mu[h] = that append's mean key per head; every key is encoded as k - mu. Scores then lack the
// per-row constant scale * q.mu: softmax and O are unchanged, and ATTN adds it back to the lse so
// the relay's log-sum-exp merge stays exact. TRUNCATE keeps mu; only CONFIG clears it.
function tq4Center(L, k, n, nkv, D) {  // k: f32 [n][nkv][D], centered in place
  if (!L.mu) {
    const mu = new Float64Array(nkv * D);
    for (let j = 0; j < n; j++) for (let i = 0; i < nkv * D; i++) mu[i] += k[j * nkv * D + i];
    L.mu = Float32Array.from(mu, (x) => x / n);
  }
  for (let j = 0; j < n; j++) for (let i = 0; i < nkv * D; i++) k[j * nkv * D + i] -= L.mu[i];
  return k;
}
function tq4Uncenter(L, lse, q, scale, g, nkv, R, D) {  // lse of group g += scale * q.mu
  if (!L.mu) return;
  for (let h = 0; h < nkv; h++) for (let r = 0; r < R; r++) {
    const row = (g * nkv + h) * R + r;
    if (!Number.isFinite(lse[row])) continue;
    let c = 0; for (let i = 0; i < D; i++) c += q[row * D + i] * L.mu[h * D + i];
    lse[row] += scale * c;
  }
}

// tq4 on the CPU: the same 4-bit codes, rotation and math as the Python holder's --store tq4
// (adk.kvholder._attend_tq4), so a phone without WebGPU still fits ~4x the keys per MB.
class CpuTq4Engine {
  constructor(opts) { this.kind = 'cpu-tq4'; this.tq4 = true; this.center = !(opts && opts.center === false); }
  storeName() { return 'tq4'; }
  configure(cfg) { this.cfg = cfg; this.layers = []; for (let i = 0; i < cfg.nLayer; i++) this.layers.push({n: 0, parts: []}); }
  bytesPerKey() { const {nkv, kd, vd} = this.cfg; return nkv * (kd / 2 + 4 + vd / 2 + 4); }
  append(layer, n, kRaw, vRaw) {
    const {nkv, kd, vd} = this.cfg, L = this.layers[layer];
    let k = dequant(kRaw, n, this.cfg, 'k');
    if (this.center) k = tq4Center(L, k, n, nkv, kd);
    const K = tq4Encode(k, n * nkv, kd), V = tq4Encode(dequant(vRaw, n, this.cfg, 'v'), n * nkv, vd);
    L.parts.push({n, kc: K.codes, kn: K.norms, vc: V.codes, vn: V.norms}); L.n += n;
  }
  truncate(keep) {
    const {nkv, kd, vd} = this.cfg;
    for (const L of this.layers) {
      if (L.n <= keep) continue;
      const kept = []; let have = 0;
      for (const p of L.parts) {
        const take = Math.min(p.n, keep - have);
        if (take <= 0) break;
        kept.push(take === p.n ? p : {n: take, kc: p.kc.slice(0, take * nkv * kd / 2), kn: p.kn.slice(0, take * nkv),
          vc: p.vc.slice(0, take * nkv * vd / 2), vn: p.vn.slice(0, take * nkv)});
        have += take;
      }
      L.parts = kept; L.n = keep;
    }
  }
  held(layer) { return this.layers[layer].n; }
  async attn(layer, nk, scale, q, nTok, ng) {
    const {nkv, kd: KD, vd: VD, rows: R} = this.cfg, rows = nkv * R, L = this.layers[layer];
    const O = new Float32Array(ng * rows * VD), lse = new Float32Array(ng * rows).fill(-Infinity);
    const invK = 1 / Math.sqrt(KD), invV = 1 / Math.sqrt(VD), hk = KD / 2, hv = VD / 2;
    const s = new Float64Array(nk), acc = new Float64Array(VD), qs = new Float32Array(KD);
    for (let g = 0; g < ng; g++) for (let h = 0; h < nkv; h++) for (let r = 0; r < R; r++) {
      const row = g * rows + h * R + r;
      if (nk === 0 || (r % 8) >= nTok) continue;
      for (let i = 0; i < KD; i++) qs[i] = q[row * KD + i] * scale;
      tq4Rotate(qs, 0, KD);
      let m = -Infinity, k = 0;
      for (const p of L.parts) for (let j = 0; j < p.n && k < nk; j++, k++) {
        const co = (j * nkv + h) * hk; let d = 0;
        for (let i = 0; i < hk; i++) { const b = p.kc[co + i]; d += qs[2 * i] * TQ4_C[b & 15] + qs[2 * i + 1] * TQ4_C[b >> 4]; }
        s[k] = d * p.kn[j * nkv + h] * invK; if (s[k] > m) m = s[k];
      }
      acc.fill(0); let l = 0; k = 0;
      for (const p of L.parts) for (let j = 0; j < p.n && k < nk; j++, k++) {
        const e = Math.exp(s[k] - m); l += e;
        const w = e * p.vn[j * nkv + h] * invV, co = (j * nkv + h) * hv;
        for (let i = 0; i < hv; i++) { const b = p.vc[co + i]; acc[2 * i] += w * TQ4_C[b & 15]; acc[2 * i + 1] += w * TQ4_C[b >> 4]; }
      }
      const o = new Float32Array(VD);
      for (let i = 0; i < VD; i++) o[i] = acc[i];
      tq4Unrotate(o, 0, VD);
      for (let i = 0; i < VD; i++) O[row * VD + i] = o[i] / l;
      lse[row] = m + Math.log(l);
    }
    for (let g = 0; g < ng; g++) tq4Uncenter(L, lse, q, scale, g, nkv, R, KD);
    return {O, lse};
  }
  describe() { return 'cpu-tq4'; }
}

function concat(arrs) {
  if (arrs.length === 1) return arrs[0];
  let n = 0; for (const a of arrs) n += a.length;
  const out = new Uint8Array(n); let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
}

// ---------------------------------------------------------------- WebGPU engine
// One workgroup of 256 threads per (block of RB live query rows, KV head, key chunk). Each tile
// of TK = 256 / RB keys is read from memory ONCE for all RB rows: thread (row j, key t) scores
// one dot against queries kept in workgroup memory, and each thread owns value dims i, i+256, ...
// of all RB rows. (One workgroup per row re-read every key 48 times; on a phone GPU that memory
// traffic was the whole cost.) Padding rows (t >= n_tok) are never computed: live rows are
// packed, and the merge writes padding as -inf. Shaders are specialized per CONFIG shape.
const WG = 256;
const f32lit = (x) => { const t = String(x); return /[.e]/.test(t) ? t : t + '.0'; };
function gpuPlan(KD, VD, lim) {
  const OPT = Math.ceil(VD / WG), M = 4;  // M keys per thread per tile
  const need = (rb) => (rb * KD + rb * (WG / rb) * M + WG + 3 * rb) * 4;  // workgroup bytes
  // 16 rows per workgroup (each key read once for 16 rows) when queries fit in workgroup memory
  const RB = OPT <= 2 && need(16) <= lim ? 16 : OPT <= 4 ? 8 : 4, TK = WG / RB, TT = TK * M;
  const shared = need(RB) <= lim;  // else queries are read from storage
  return {OPT, RB, TK, M, TT, shared};
}
const PARTIAL_WGSL = (kind, KD, VD, R, lim) => {  // kind: 'f32' | 'f16' | 'tq4'
  const {OPT, RB, TK, M, TT, shared} = gpuPlan(KD, VD, lim || 16384), tq = kind === 'tq4', K4 = KD / 4;
  const qv = (d4) => shared ? `qs[j * ${K4}u + ${d4}]` : `(Q[qb + ${d4}] * p.scale)`;
  // a thread scores keys t, t + TK, ... (M of them) for its row j; vec4 loads of K and Q
  const dot = tq ? `
        let kw = (k * p.nkv + h) * ${KD / 32}u;
        for (var w = 0u; w < ${KD / 32}u; w = w + 1u) {
          let wd = K[kw + w];
          for (var e = 0u; e < 4u; e = e + 1u) {
            let word = wd[e]; let qb4 = w * 8u + e * 2u;
            let a = ${qv('qb4')}; let b2 = ${qv('qb4 + 1u')};
            acc = acc + dot(a, vec4<f32>(C[word & 15u], C[(word >> 4u) & 15u], C[(word >> 8u) & 15u], C[(word >> 12u) & 15u]))
                      + dot(b2, vec4<f32>(C[(word >> 16u) & 15u], C[(word >> 20u) & 15u], C[(word >> 24u) & 15u], C[word >> 28u]));
          }
        }
        s = acc * KN[k * p.nkv + h] * INVK;` : `
        let kb = (k * p.nkv + h) * ${K4}u;
        for (var d = 0u; d < ${K4}u; d = d + 1u) { acc = acc + dot(${qv('d')}, vec4<f32>(K[kb + d])); }
        s = acc;`;
  const vval = tq
    ? `let key = (t0 + u) * p.nkv + h; let v = C[(V[key * ${VD / 8}u + d / 8u] >> ((d % 8u) * 4u)) & 15u] * VN[key] * INVV;`
    : `let v = f32(V[((t0 + u) * p.nkv + h) * ${VD}u + d]);`;
  return `${kind === 'f16' ? 'enable f16;' : ''}
struct P { nk: u32, nkv: u32, slot: u32, ntok: u32, scale: f32, rows: u32, chunk: u32, nl: u32 };
@group(0) @binding(0) var<uniform> p: P;
@group(0) @binding(1) var<storage, read> K: array<${tq ? 'vec4<u32>' : `vec4<${kind}>`}>;
@group(0) @binding(2) var<storage, read> V: array<${tq ? 'u32' : kind}>;
@group(0) @binding(3) var<storage, read> Q: array<vec4<f32>>;
@group(0) @binding(4) var<storage, read_write> PO: array<f32>;
@group(0) @binding(5) var<storage, read_write> PML: array<f32>;
${tq ? `@group(0) @binding(6) var<storage, read> KN: array<f32>;
@group(0) @binding(7) var<storage, read> VN: array<f32>;
var<private> C: array<f32, 16> = array<f32, 16>(${TQ4_C.map((c) => c.toFixed(4)).join(', ')});
const INVK: f32 = ${f32lit(1 / Math.sqrt(KD))};
const INVV: f32 = ${f32lit(1 / Math.sqrt(VD))};` : ''}
${shared ? `var<workgroup> qs: array<vec4<f32>, ${RB * K4}>;` : ''}
var<workgroup> sc: array<vec4<f32>, ${RB / 4 * TT}>;  // [key][row / 4]: 4 rows per load
var<workgroup> red: array<f32, 256>;
var<workgroup> rm: array<f32, ${RB}>;
var<workgroup> rl: array<f32, ${RB}>;
var<workgroup> ra: array<f32, ${RB}>;
const NEG: f32 = -3.0e38;
fn qrow(li: u32) -> u32 { return (li / p.ntok) * 8u + li % p.ntok; }  // live row -> PATN row
@compute @workgroup_size(256)
fn main(@builtin(workgroup_id) wg: vec3<u32>, @builtin(local_invocation_index) i: u32) {
  let h = wg.y; let slot = p.slot + wg.z;
  let k0 = wg.z * p.chunk;
  let k1 = min(k0 + p.chunk, p.nk);
  let li0 = wg.x * ${RB}u;
  ${shared ? `for (var x = i; x < ${RB * K4}u; x = x + 256u) {
    let li = li0 + x / ${K4}u;
    var qv = vec4<f32>(0.0);
    if (li < p.nl) { qv = Q[(h * ${R}u + qrow(li)) * ${K4}u + x % ${K4}u] * p.scale; }
    qs[x] = qv;
  }` : ''}
  if (i < ${RB}u) { rm[i] = NEG; rl[i] = 0.0; }
  workgroupBarrier();
  let j = i / ${TK}u; let t = i % ${TK}u;
  let jok = li0 + j < p.nl;
  ${shared ? '' : `let qb = (h * ${R}u + qrow(min(li0 + j, p.nl - 1u))) * ${K4}u;`}
  var o: array<vec4<f32>, ${RB / 4 * OPT}>;  // [row / 4][c]
  var sv: array<f32, ${M}>;
  for (var t0 = k0; t0 < k1; t0 = t0 + ${TT}u) {
    var lm = NEG;
    for (var m = 0u; m < ${M}u; m = m + 1u) {
      let k = t0 + t + m * ${TK}u;
      var s = NEG;
      if (k < k1 && jok) {
        var acc = 0.0;${dot}
      }
      sv[m] = s; lm = max(lm, s);
    }
    red[i] = lm;
    workgroupBarrier();
    if (i < ${RB}u) {
      var tm = NEG;
      for (var u = 0u; u < ${TK}u; u = u + 1u) { tm = max(tm, red[i * ${TK}u + u]); }
      let nm = max(rm[i], tm);
      ra[i] = exp(rm[i] - nm); rm[i] = nm;
    }
    workgroupBarrier();
    var ls = 0.0;
    for (var m = 0u; m < ${M}u; m = m + 1u) {
      let k = t0 + t + m * ${TK}u;
      var pv = 0.0;
      if (k < k1 && jok) { pv = exp(sv[m] - rm[j]); }
      sc[(t + m * ${TK}u) * ${RB / 4}u + j / 4u][j % 4u] = pv; ls = ls + pv;
    }
    red[i] = ls;
    workgroupBarrier();
    if (i < ${RB}u) {
      var ps = 0.0;
      for (var u = 0u; u < ${TK}u; u = u + 1u) { ps = ps + red[i * ${TK}u + u]; }
      rl[i] = rl[i] * ra[i] + ps;
    }
    let cnt = min(${TT}u, k1 - t0);
    for (var c = 0u; c < ${OPT}u; c = c + 1u) {
      let d = i + c * 256u;
      if (d < ${VD}u) {
        for (var g = 0u; g < ${RB / 4}u; g = g + 1u) {
          o[g * ${OPT}u + c] = o[g * ${OPT}u + c] * vec4<f32>(ra[g * 4u], ra[g * 4u + 1u], ra[g * 4u + 2u], ra[g * 4u + 3u]);
        }
        for (var u = 0u; u < cnt; u = u + 1u) {
          ${vval}
          for (var g = 0u; g < ${RB / 4}u; g = g + 1u) { o[g * ${OPT}u + c] = o[g * ${OPT}u + c] + sc[u * ${RB / 4}u + g] * v; }
        }
      }
    }
    workgroupBarrier();
  }
  for (var jj = 0u; jj < ${RB}u; jj = jj + 1u) {
    let li = li0 + jj;
    if (li < p.nl) {
      let base = slot * p.rows + h * ${R}u + qrow(li);
      for (var c = 0u; c < ${OPT}u; c = c + 1u) {
        let d = i + c * 256u;
        if (d < ${VD}u) { PO[base * ${VD}u + d] = o[(jj / 4u) * ${OPT}u + c][jj % 4u]; }
      }
      if (i == 0u) { PML[base * 2u] = rm[jj]; PML[base * 2u + 1u] = rl[jj]; }
    }
  }
}`;
};
const MERGE_WGSL = (VD, R) => `
struct M { nslots: u32, rows: u32, ntok: u32, b: u32 };
@group(0) @binding(0) var<uniform> mp: M;
@group(0) @binding(1) var<storage, read> PO: array<f32>;
@group(0) @binding(2) var<storage, read> PML: array<f32>;
@group(0) @binding(3) var<storage, read_write> OUT: array<f32>;
@group(0) @binding(4) var<storage, read_write> LSE: array<f32>;
const NEG: f32 = -3.0e38;
@compute @workgroup_size(256)
fn main(@builtin(workgroup_id) wg: vec3<u32>, @builtin(local_invocation_index) i: u32) {
  let row = wg.x;
  if ((row % ${R}u) % 8u >= mp.ntok) {  // padding: never computed
    for (var d = i; d < ${VD}u; d = d + 256u) { OUT[row * ${VD}u + d] = 0.0; }
    if (i == 0u) { LSE[row] = NEG; }
    return;
  }
  var mx = NEG;
  for (var s = 0u; s < mp.nslots; s = s + 1u) {
    let b = (s * mp.rows + row) * 2u;
    if (PML[b + 1u] > 0.0) { mx = max(mx, PML[b]); }
  }
  var den = 0.0;
  for (var s = 0u; s < mp.nslots; s = s + 1u) {
    let l = PML[(s * mp.rows + row) * 2u + 1u];
    if (l > 0.0) { den = den + exp(PML[(s * mp.rows + row) * 2u] - mx) * l; }
  }
  for (var d = i; d < ${VD}u; d = d + 256u) {
    var acc = 0.0;
    for (var s = 0u; s < mp.nslots; s = s + 1u) {
      let b = (s * mp.rows + row) * 2u;
      if (PML[b + 1u] > 0.0) { acc = acc + exp(PML[b] - mx) * PO[(s * mp.rows + row) * ${VD}u + d]; }
    }
    OUT[row * ${VD}u + d] = select(0.0, acc / den, den > 0.0);
  }
  if (i == 0u) { LSE[row] = select(NEG, mx + log(den), den > 0.0); }
}`;

// ---------------------------------------------------------------- tq4: TurboQuant-style 4-bit store
// Rotate (signs, then a Walsh-Hadamard transform), quantize each coordinate to 16 Lloyd-Max
// levels for a unit Gaussian, keep one norm per vector. The shader unpacks nibbles in
// registers while it multiplies; norms fold into scores and weights. ~4x the keys per MB of f16.
// Any power-of-two width up to MAX_DIM. The signs are the Python holder's (adk.kvholder.tq4_rotation:
// numpy default_rng(0x7A5), one bit per sign, set = -1), so both holders store identical codes.
const TQ4_C = [-2.7326, -2.0690, -1.6181, -1.2562, -0.9424, -0.6568, -0.3881, -0.1284,
  0.1284, 0.3881, 0.6568, 0.9424, 1.2562, 1.6181, 2.0690, 2.7326];
const TQ4_EDGES = TQ4_C.slice(1).map((c, i) => (c + TQ4_C[i]) / 2);
const TQ4_SIGN_B64 = (
  'sLU+CofxvBX5tjyCAWPBbdpbGBbX4aVylMf28Q0NnleVJaLd65zL4WEUMYoMCmQaokthWq+w5afNghfQxQdk68hDq7v7519w' +
  'Rsu9tgPcp5US5BahPs/3Ir2VbHjC2qx6mPVCmsBWogDqqW+7/c5yA6kKQfpmvSBCNod0vSOzf7MPuHGfALLEzIz0U2dPq9mS' +
  'V/x1S+sysMKxziVvSyzZx5BrVJFAW7AKBKlr68IxO/utic8PkfhtQHY1jazZQp+NRDTQFa2WDep8mitElL+SBU1z4qUV0ecA' +
  '9vUNJhnSYexc14h4KudPUnAV+4D0KFbIZGa0X55c1eEbK5M+Cz0nvH7rqKQ4l1PHJRnHRFfIBZZJku4D5KKSVUyzw2IJT0ld' +
  'qnod2WnG5Q3BqIxLzugbqgNHcotwmkEZON0KDhUS92tL8mTc3mOS56TWXErbSIJPcdFDV7naFmQ3xb++5FaZ3gExpN/iSocU' +
  'VHfi8L4sLvzf7ZpZ39xfRYegDk3Ohp/TLy1YXO+NTrNW4RGxf/SBtzVtaHl5gMb8r/pgweVj+ba7i0KBnHDnj8DcmoFaS+pG' +
  'CQf3+nLxSqr+9iisBojlo4f694STzA2ygqFW1x7HgufXwKHKSSs3NEnPMyHts1NsgHh+JJ+EZIOqLil+ctieQmp01lYvHGZL' +
  'Ecwi5m1XNB8=');
const TQ4_SIGN = (() => {
  const raw = atob(TQ4_SIGN_B64), out = new Float32Array(MAX_DIM);
  for (let j = 0; j < MAX_DIM; j++) out[j] = (raw.charCodeAt(j >> 3) >> (j & 7)) & 1 ? -1 : 1;
  return out;
})();
function fwht(a, off, D) {  // in place, length D; applying it twice multiplies by D
  for (let h = 1; h < D; h <<= 1)
    for (let i = 0; i < D; i += h << 1)
      for (let j = i; j < i + h; j++) { const x = a[off + j], y = a[off + j + h]; a[off + j] = x + y; a[off + j + h] = x - y; }
}
function tq4Rotate(a, off, D) {
  D = D || HD; const s = 1 / Math.sqrt(D);
  for (let j = 0; j < D; j++) a[off + j] *= TQ4_SIGN[j]; fwht(a, off, D); for (let j = 0; j < D; j++) a[off + j] *= s;
}
function tq4Unrotate(a, off, D) { D = D || HD; const s = 1 / Math.sqrt(D); fwht(a, off, D); for (let j = 0; j < D; j++) a[off + j] *= TQ4_SIGN[j] * s; }
// f32 [vecs][D] -> codes Uint8Array [vecs][D/2], norms Float32Array [vecs]
function tq4Encode(x, vecs, D) {
  D = D || HD;
  const codes = new Uint8Array(vecs * D / 2), norms = new Float32Array(vecs), sq = Math.sqrt(D);
  for (let v = 0; v < vecs; v++) {
    const off = v * D;
    tq4Rotate(x, off, D);
    let nn = 0; for (let j = 0; j < D; j++) nn += x[off + j] * x[off + j];
    const norm = Math.sqrt(nn), k = norm > 0 ? sq / norm : 0;
    norms[v] = norm;
    for (let j = 0; j < D; j += 2) {
      let c0 = 0, c1 = 0; const u0 = x[off + j] * k, u1 = x[off + j + 1] * k;
      while (c0 < 15 && u0 > TQ4_EDGES[c0]) c0++;
      while (c1 < 15 && u1 > TQ4_EDGES[c1]) c1++;
      codes[(off + j) >> 1] = c0 | (c1 << 4);
    }
  }
  return {codes, norms};
}
class GpuEngine {
  static async create(gpu, opts) {
    if (!gpu) return null;
    const adapter = await gpu.requestAdapter({powerPreference: 'high-performance'});
    if (!adapter) return null;
    const f16 = adapter.features.has('shader-f16');
    const want = Math.min(adapter.limits.maxStorageBufferBindingSize, adapter.limits.maxBufferSize, 256 * 1024 * 1024);
    const device = await adapter.requestDevice({
      requiredFeatures: f16 ? ['shader-f16'] : [],
      requiredLimits: {maxStorageBufferBindingSize: want, maxBufferSize: want},
    });
    const e = new GpuEngine();
    e.tq4 = !!(opts && opts.tq4); e.center = e.tq4 && opts.center !== false;
    e.device = device; e.f16 = f16 && !e.tq4; e.maxBind = want; e.pipes = {};
    e.kind = 'webgpu' + (e.tq4 ? '-tq4' : f16 ? '-f16' : '');
    const info = adapter.info || {};
    e.adapterName = [info.vendor, info.architecture, info.device].filter(Boolean).join(' ') || 'gpu';
    // warm-up: the first dispatch pays the driver's first-use costs; pay them now, not on a call
    const bad = await e.configure(mkCfg(1, 1, 0));
    if (bad) throw new Error(bad);
    e.append(0, 256, new Uint8Array(256 * HD * 2), new Uint8Array(256 * HD * 2));
    await e.attn(0, 256, 0.0625, new Float32Array(NR * HD), 8, 1);
    return e;
  }
  elem() { return this.f16 ? 2 : 4; }
  storeName() { return this.tq4 ? 'tq4' : this.f16 ? 'f16' : 'f32'; }
  sideBytes(D) { return this.tq4 ? this.cfg.nkv * D / 2 : this.cfg.nkv * D * this.elem(); }
  bytesPerKey() {
    const {nkv, kd, vd} = this.cfg;
    return this.tq4 ? nkv * (kd / 2 + 4 + vd / 2 + 4) : nkv * (kd + vd) * this.elem();
  }
  // compile (once per shape) before dropping anything: a shape this GPU cannot run is an ERR
  // reply and the engine keeps what it held
  async configure(cfg) {
    const key = cfg.kd + 'x' + cfg.vd + 'x' + cfg.rows;
    if (!this.pipes[key]) {
      const lim = this.device.limits.maxComputeWorkgroupStorageSize || 16384;
      const code = PARTIAL_WGSL(this.tq4 ? 'tq4' : this.f16 ? 'f16' : 'f32', cfg.kd, cfg.vd, cfg.rows, lim);
      const mk = (src) => this.device.createComputePipelineAsync({layout: 'auto', compute: {module: this.device.createShaderModule({code: src}), entryPoint: 'main'}});
      try {
        const [partial, merge] = await Promise.all([mk(code), mk(MERGE_WGSL(cfg.vd, cfg.rows))]);
        this.pipes[key] = {partial, merge};
      } catch (err) {
        return 'bad CONFIG: shader for k' + cfg.kd + ' v' + cfg.vd + ' rows ' + cfg.rows + ' failed: ' + String(err && err.message || err).slice(0, 120);
      }
    }
    if (this.layers) for (const L of this.layers) for (const s of L.segs) this.dropSeg(s);
    this.cfg = cfg; this.partial = this.pipes[key].partial; this.merge = this.pipes[key].merge;
    this.rb = gpuPlan(cfg.kd, cfg.vd, this.device.limits.maxComputeWorkgroupStorageSize || 16384).RB;
    const widest = Math.max(this.sideBytes(cfg.kd), this.sideBytes(cfg.vd));
    this.segKeys = Math.max(256, Math.floor(Math.min(this.maxBind, 64 * 1024 * 1024) / widest / 256) * 256);
    this.layers = []; for (let i = 0; i < cfg.nLayer; i++) this.layers.push({n: 0, segs: []});
    this.scratch = null;
    return null;
  }
  held(layer) { return this.layers[layer].n; }
  sync() { return this.device.queue.onSubmittedWorkDone(); }
  dropSeg(s) { s.k.destroy(); s.v.destroy(); if (s.kn) { s.kn.destroy(); s.vn.destroy(); } }
  append(layer, n, kRaw, vRaw) {
    if (this.tq4) return this.appendTq4(layer, n, kRaw, vRaw);
    const L = this.layers[layer], cfg = this.cfg, kb = this.sideBytes(cfg.kd), vb = this.sideBytes(cfg.vd);
    const K = this.pack(dequant(kRaw, n, cfg, 'k')), V = this.pack(dequant(vRaw, n, cfg, 'v'));
    const kper = cfg.nkv * cfg.kd, vper = cfg.nkv * cfg.vd;
    let done = 0;
    while (done < n) {
      const pos = L.n + done, si = Math.floor(pos / this.segKeys), off = pos % this.segKeys;
      while (L.segs.length <= si) {
        const u = GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST;
        L.segs.push({k: this.device.createBuffer({size: this.segKeys * kb, usage: u}), v: this.device.createBuffer({size: this.segKeys * vb, usage: u})});
      }
      const take = Math.min(n - done, this.segKeys - off);
      this.device.queue.writeBuffer(L.segs[si].k, off * kb, K, done * kper, take * kper);
      this.device.queue.writeBuffer(L.segs[si].v, off * vb, V, done * vper, take * vper);
      done += take;
    }
    L.n += n;
  }
  appendTq4(layer, n, kRaw, vRaw) {
    const L = this.layers[layer], cfg = this.cfg, nkv = cfg.nkv, cbk = nkv * cfg.kd / 2, cbv = nkv * cfg.vd / 2, nb = nkv * 4;
    let k = dequant(kRaw, n, cfg, 'k');
    if (this.center) k = tq4Center(L, k, n, nkv, cfg.kd);
    const K = tq4Encode(k, n * nkv, cfg.kd), V = tq4Encode(dequant(vRaw, n, cfg, 'v'), n * nkv, cfg.vd);
    const u = GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST;
    let done = 0;
    while (done < n) {
      const pos = L.n + done, si = Math.floor(pos / this.segKeys), off = pos % this.segKeys;
      while (L.segs.length <= si) {
        const mk = (size) => this.device.createBuffer({size, usage: u});
        L.segs.push({k: mk(this.segKeys * cbk), v: mk(this.segKeys * cbv), kn: mk(this.segKeys * nb), vn: mk(this.segKeys * nb)});
      }
      const take = Math.min(n - done, this.segKeys - off), sg = L.segs[si];
      this.device.queue.writeBuffer(sg.k, off * cbk, K.codes, done * cbk, take * cbk);
      this.device.queue.writeBuffer(sg.v, off * cbv, V.codes, done * cbv, take * cbv);
      this.device.queue.writeBuffer(sg.kn, off * nb, K.norms, done * nkv, take * nkv);
      this.device.queue.writeBuffer(sg.vn, off * nb, V.norms, done * nkv, take * nkv);
      done += take;
    }
    L.n += n;
  }
  pack(f) {
    if (!this.f16) return f;
    const u = new Uint16Array(f.length);
    for (let i = 0; i < f.length; i++) u[i] = f2h(f[i]);
    return u;
  }
  truncate(keep) {
    for (const L of this.layers) {
      if (L.n <= keep) continue;
      const need = Math.ceil(keep / this.segKeys);
      while (L.segs.length > need) this.dropSeg(L.segs.pop());
      L.n = keep;
    }
  }
  buf(name, size, usage) {
    this.scratch = this.scratch || {};
    const b = this.scratch[name];
    if (b && b.size >= size) return b;
    if (b) b.destroy();
    return (this.scratch[name] = this.device.createBuffer({size, usage}));
  }
  async attn(layer, nk, scale, q, nTok, ng) {
    const d = this.device, {nkv, kd: KD, vd: VD, rows: R} = this.cfg, rows = nkv * R, L = this.layers[layer];
    const S = GPUBufferUsage.STORAGE, CD = GPUBufferUsage.COPY_DST, CS = GPUBufferUsage.COPY_SRC;
    const O = new Float32Array(ng * rows * VD), lse = new Float32Array(ng * rows).fill(-Infinity);
    if (nk === 0) return {O, lse};
    // flash-decoding split: chunks of keys run in parallel workgroups, ~128 partials per call
    const chunk = Math.max(256, Math.ceil(nk / 128 / 256) * 256);
    const ntok = Math.min(nTok, 8), nl = R / 8 * ntok, blk = this.rb;  // live rows per KV head
    const nseg = Math.ceil(nk / this.segKeys), plan = [];
    let nslots = 0;
    for (let s = 0; s < nseg; s++) {
      const ks = Math.min(this.segKeys, nk - s * this.segKeys), nc = Math.ceil(ks / chunk);
      plan.push({s, ks, nc, base: nslots}); nslots += nc;
    }
    const qb = this.buf('q', rows * KD * 4, S | CD);
    const po = this.buf('po', nslots * rows * VD * 4, S);
    const pml = this.buf('pml', nslots * rows * 2 * 4, S);
    const out = this.buf('out', rows * VD * 4, S | CS);
    const outl = this.buf('outl', rows * 4, S | CS);
    const rb = this.buf('rb', rows * VD * 4, GPUBufferUsage.MAP_READ | CD);
    const rbl = this.buf('rbl', rows * 4, GPUBufferUsage.MAP_READ | CD);
    const ub = this.buf('u' + nseg, 256 * (nseg + 1), GPUBufferUsage.UNIFORM | CD);
    const q0 = q;
    if (this.tq4) { q = q.slice(); for (let v = 0; v < ng * rows; v++) tq4Rotate(q, v * KD, KD); }
    for (let g = 0; g < ng; g++) {
      d.queue.writeBuffer(qb, 0, q, g * rows * KD, rows * KD);
      const enc2 = d.createCommandEncoder();
      for (const {s, ks, nc, base} of plan) {
        const pu = new ArrayBuffer(32), pv = new DataView(pu);
        pv.setUint32(0, ks, true); pv.setUint32(4, nkv, true); pv.setUint32(8, base, true);
        pv.setUint32(12, nTok, true); pv.setFloat32(16, scale, true); pv.setUint32(20, rows, true);
        pv.setUint32(24, chunk, true); pv.setUint32(28, nl, true);
        d.queue.writeBuffer(ub, 256 * s, pu);
        const bg = d.createBindGroup({layout: this.partial.getBindGroupLayout(0), entries: [
          {binding: 0, resource: {buffer: ub, offset: 256 * s, size: 32}},
          {binding: 1, resource: {buffer: L.segs[s].k}}, {binding: 2, resource: {buffer: L.segs[s].v}},
          {binding: 3, resource: {buffer: qb}}, {binding: 4, resource: {buffer: po}}, {binding: 5, resource: {buffer: pml}},
          ...(this.tq4 ? [{binding: 6, resource: {buffer: L.segs[s].kn}}, {binding: 7, resource: {buffer: L.segs[s].vn}}] : [])]});
        const pass = enc2.beginComputePass(); pass.setPipeline(this.partial); pass.setBindGroup(0, bg);
        if (nl) { pass.dispatchWorkgroups(Math.ceil(nl / blk), nkv, nc); }
        pass.end();
      }
      const mu = new ArrayBuffer(16), mv = new DataView(mu);
      mv.setUint32(0, nslots, true); mv.setUint32(4, rows, true); mv.setUint32(8, ntok, true);
      d.queue.writeBuffer(ub, 256 * nseg, mu);
      const mbg = d.createBindGroup({layout: this.merge.getBindGroupLayout(0), entries: [
        {binding: 0, resource: {buffer: ub, offset: 256 * nseg, size: 16}},
        {binding: 1, resource: {buffer: po}}, {binding: 2, resource: {buffer: pml}},
        {binding: 3, resource: {buffer: out}}, {binding: 4, resource: {buffer: outl}}]});
      const mp = enc2.beginComputePass(); mp.setPipeline(this.merge); mp.setBindGroup(0, mbg);
      mp.dispatchWorkgroups(rows); mp.end();
      enc2.copyBufferToBuffer(out, 0, rb, 0, rows * VD * 4);
      enc2.copyBufferToBuffer(outl, 0, rbl, 0, rows * 4);
      const tw = performance.now();
      d.queue.submit([enc2.finish()]);
      await Promise.all([rb.mapAsync(GPUMapMode.READ, 0, rows * VD * 4), rbl.mapAsync(GPUMapMode.READ, 0, rows * 4)]);
      O.set(new Float32Array(rb.getMappedRange(0, rows * VD * 4)), g * rows * VD);
      const l = new Float32Array(rbl.getMappedRange(0, rows * 4));
      for (let i = 0; i < rows; i++) lse[g * rows + i] = l[i] < -1e37 ? -Infinity : l[i];
      rb.unmap(); rbl.unmap();
      if (this.tq4) for (let v = 0; v < rows; v++) tq4Unrotate(O, (g * rows + v) * VD, VD);
      if (this.center) tq4Uncenter(L, lse, q0, scale, g, nkv, R, KD);
      this.lastPhase = 'gpu+map=' + (performance.now() - tw).toFixed(1) + 'ms slots=' + nslots;
    }
    return {O, lse};
  }
  describe() { return this.kind + ' ' + this.adapterName; }
}

// ---------------------------------------------------------------- PATN state machine
class Holder {
  constructor(engine, budgetBytes, device) {
    this.engine = engine; this.budget = budgetBytes; this.device = device || 'browser-kvholder';
    this.cfg = null; this.held = 0; this.calls = 0; this.appended = 0; this.lastMs = 0; this.sumMs = 0;
  }
  async handle(type, p) {
    const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    switch (type) {
      case T.HELLO: {
        const out = new Uint8Array(72), v = new DataView(out.buffer);
        v.setUint32(0, VERSION, true); v.setUint32(4, 0, true);
        out.set(enc.encode((this.device + ' ' + this.engine.kind + ' max_mb=' + Math.floor(this.budget / 1048576)).slice(0, 63)), 8);
        return [T.HELLO_OK, out];
      }
      case T.CONFIG: {
        if (p.length < 44) return this.err('short CONFIG');
        const c = {nLayer: dv.getUint32(0, true), nkv: dv.getUint32(4, true), rs: dv.getUint32(8, true), hb: dv.getUint32(12, true), type: dv.getUint32(16, true),
          kd: HD, vd: HD, rows: NR, rsv: 0, hbv: 0};
        if (p.length >= 68 && dv.getUint32(44, true) === V4_TAG) {
          c.kd = dv.getUint32(48, true); c.vd = dv.getUint32(52, true); c.rows = dv.getUint32(56, true);
          c.rsv = dv.getUint32(60, true); c.hbv = dv.getUint32(64, true);
        }
        c.rsv = c.rsv || c.rs; c.hbv = c.hbv || c.hb;  // 0: V rows look like K rows
        const dimOk = (d) => d > 0 && d <= MAX_DIM && d % 32 === 0;
        if (!(c.nkv > 0 && c.nkv <= 16 && c.nLayer > 0 && c.nLayer <= 256 && c.rows > 0 && c.rows <= 1024 && c.rows % 8 === 0 &&
              dimOk(c.kd) && dimOk(c.vd) && c.rs >= c.nkv * c.hb && c.rsv >= c.nkv * c.hbv)) return this.err('bad CONFIG');
        if (headBytes(c.type, c.kd) !== c.hb || headBytes(c.type, c.vd) !== c.hbv) return this.err('bad CONFIG: unsupported row format');
        if (this.engine.tq4 && !(isPow2(c.kd) && isPow2(c.vd))) return this.err('bad CONFIG: tq4 needs power-of-two head dims (use f32 or wire)');
        const bad = await this.engine.configure(c);
        if (bad) return this.err(bad);
        this.cfg = c; this.held = 0;
        return [T.OK, new Uint8Array(0)];
      }
      case T.APPEND: {
        if (!this.cfg || p.length < 12) return this.err('short APPEND');
        const layer = dv.getUint32(0, true), pos0 = dv.getUint32(4, true), n = dv.getUint32(8, true), nb = n * this.cfg.rs, nbv = n * this.cfg.rsv;
        if (layer >= this.cfg.nLayer || p.length !== 12 + nb + nbv) return this.err('bad APPEND');
        const have = this.engine.held(layer);
        if (pos0 !== have) return this.err('APPEND pos0 ' + pos0 + ' != held ' + have);
        const per = this.engine.bytesPerKey();
        if (this.used() + n * per > this.budget) return this.err('out of memory at ' + have + ' keys');
        this.engine.append(layer, n, p.subarray(12, 12 + nb), p.subarray(12 + nb, 12 + nb + nbv));
        if (this.engine.sync) await this.engine.sync();  // OK means resident: no upload lands on an ATTN
        this.appended += n;
        const out = new Uint8Array(4); new DataView(out.buffer).setUint32(0, this.engine.held(layer), true);
        return [T.OK, out];
      }
      case T.TRUNCATE: {
        const keep = p.length >= 4 ? dv.getUint32(0, true) : 0;
        if (this.cfg) this.engine.truncate(keep);
        return [T.OK, new Uint8Array(0)];
      }
      case T.ATTN: case T.ATTN_BIG: return this.attn(p, dv, type === T.ATTN_BIG);
      case T.STATS: {
        const held = this.cfg ? this.engine.held(0) : 0;
        const s = 'state=idle attn_calls=' + this.calls + ' last_ms=' + this.lastMs.toFixed(3) + ' mean_ms=' + (this.calls ? this.sumMs / this.calls : 0).toFixed(3) +
          ' held=' + held + ' appended=' + this.appended + ' held_mb=' + (this.used() / 1048576).toFixed(1) + ' max_mb=' + Math.floor(this.budget / 1048576) + ' engine=' + this.engine.describe() + (this.phase ? ' last=' + this.phase : '');
        return [T.OK, enc.encode(s)];
      }
      case T.PING: { const n = p.length >= 4 ? dv.getUint32(0, true) : 0; return [T.OK, new Uint8Array(Math.min(n, 64 << 20)).fill(0x5a)]; }
      case T.BYE: return null;
      default: return this.err('unknown message ' + type);
    }
  }
  maxHeld() { if (!this.cfg) return 0; let m = 0; for (let l = 0; l < this.cfg.nLayer; l++) m = Math.max(m, this.engine.held(l)); return m; }
  used() { if (!this.cfg) return 0; let n = 0; for (let l = 0; l < this.cfg.nLayer; l++) n += this.engine.held(l); return n * this.engine.bytesPerKey(); }
  err(text) { return [T.ERR, enc.encode(text)]; }
  async attn(p, dv, big) {
    if (!this.cfg || p.length < 16) return this.err('short ATTN');
    const layer = dv.getUint32(0, true), nTok = dv.getUint32(4, true), nkReq = dv.getUint32(8, true), scale = dv.getFloat32(12, true);
    const qn = this.cfg.nkv * this.cfg.rows * this.cfg.kd, body = p.length - 16, ng = Math.floor(body / (2 * qn));
    if (layer >= this.cfg.nLayer || ng < 1 || body !== ng * 2 * qn || (!big && ng !== 1) || ng > MAX_GROUPS) return this.err('bad ATTN');
    const t0 = (typeof performance !== 'undefined' ? performance : Date).now();
    const held = this.engine.held(layer), nk = nkReq === 0 ? held : Math.min(nkReq, held);
    const q = new Float32Array(ng * qn);
    for (let i = 0; i < q.length; i++) q[i] = h2f(dv.getUint16(16 + 2 * i, true));
    const t1 = (typeof performance !== 'undefined' ? performance : Date).now();
    const {O, lse} = await this.engine.attn(layer, nk, scale, q, nTok, ng);
    const ms = (typeof performance !== 'undefined' ? performance : Date).now() - t0;
    this.phase = 'q=' + (t1 - t0).toFixed(1) + 'ms engine=' + (ms - (t1 - t0)).toFixed(1) + 'ms' + (this.engine.lastPhase ? ' (' + this.engine.lastPhase + ')' : '');
    this.calls++; this.lastMs = ms; this.sumMs += ms;
    const out = new Uint8Array(24 + O.length * 2 + lse.length * 4), ov = new DataView(out.buffer);
    ov.setUint32(0, nk, true); ov.setFloat32(4, ms, true); ov.setFloat32(8, this.engine.kind.startsWith('cpu') ? 0 : ms, true);
    ov.setFloat32(12, this.engine.kind.startsWith('cpu') ? ms : 0, true); ov.setUint32(16, 0, true); ov.setUint32(20, Math.ceil(nk / 4096), true);
    for (let i = 0; i < O.length; i++) ov.setUint16(24 + 2 * i, f2h(O[i]), true);
    const lo = 24 + O.length * 2;
    for (let i = 0; i < lse.length; i++) ov.setFloat32(lo + 4 * i, lse[i], true);
    return [T.ATTN_OK, out];
  }
}

// ---------------------------------------------------------------- transport: dial the relay
function frame(type, payload) {
  const out = new Uint8Array(16 + payload.length), v = new DataView(out.buffer);
  v.setUint32(0, MAGIC, true); v.setUint32(4, type, true); v.setUint32(8, payload.length, true); v.setUint32(12, 0, true);
  out.set(payload, 16); return out;
}
function connect(url, token, holder, on) {
  on = on || {};
  const ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  let chain = Promise.resolve();
  ws.onopen = () => ws.send(JSON.stringify({hello: 'kvholder', token: token, device: holder.device + ' (' + holder.engine.kind + ')', version: VERSION, max_bytes: holder.budget, held: holder.maxHeld(),
    store: holder.engine.storeName(), ...(holder.engine.tq4 && holder.engine.center ? {tq4: 'centered'} : {})}));
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const m = JSON.parse(ev.data);
      if (m.ok) { if (m.session) on.session && on.session(m.session); on.attached && on.attached(); } else { on.error && on.error(m.error || 'refused'); ws.close(); }
      return;
    }
    const msg = new Uint8Array(ev.data);
    chain = chain.then(async () => {
      const v = new DataView(msg.buffer);
      if (v.getUint32(0, true) !== MAGIC) return;
      const type = v.getUint32(4, true), n = v.getUint32(8, true);
      const rep = await holder.handle(type, msg.subarray(16, 16 + n));
      if (rep) ws.send(frame(rep[0], rep[1]));
      on.activity && on.activity(holder);
    }).catch((e) => { on.error && on.error(String(e)); });
  };
  ws.onclose = () => { on.closed && on.closed(); };
  return ws;
}

// A phone browser freezes a background tab: its socket stays open but nothing answers, so the
// engine's next call waits out the relay's timeout. Close the link while the page is hidden (the
// relay drops the holder at its next call and the next CONFIG places keys on the others), and
// reattach when it is visible again: with the session token and the same keys the relay swaps
// the link and nothing is lost. on.pause / on.resume do the closing and the dialing.
function pauseWhenHidden(doc, win, on) {
  let paused = false;
  const pause = () => { if (!paused) { paused = true; on.pause(); } };
  const resume = () => { if (paused && doc.visibilityState !== 'hidden') { paused = false; on.resume(); } };
  doc.addEventListener('visibilitychange', () => (doc.visibilityState === 'hidden' ? pause() : resume()));
  win.addEventListener('pagehide', pause);
  win.addEventListener('pageshow', resume);
  return {get paused() { return paused; }};
}

const api = {Holder, CpuEngine, CpuTq4Engine, GpuEngine, connect, pauseWhenHidden, h2f, f2h, dequant, mkCfg, headBytes, tq4Encode, tq4Rotate, tq4Unrotate, VERSION,
  wgsl: {partial: PARTIAL_WGSL, merge: MERGE_WGSL, plan: gpuPlan}};
if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.KVHolder = api;
})(typeof window !== 'undefined' ? window : globalThis);

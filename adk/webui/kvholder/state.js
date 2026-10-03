// What a KV holder (or the relay) is doing, from its call counter alone. Shared by index.html
// and swarm.html, and run by the tests under Node: the one place the 5 s rule lives.
//
// The rule: "active" exactly while a call arrived within the last WINDOW_S seconds, and the
// rate shown is calls over that same window, so "active" never sits next to 0 calls/s.
(function (root) {
'use strict';
const WINDOW_S = 5;

// A cumulative counter sampled over time: [[seconds, count], ...] plus when it last rose.
function counter() { return {pts: [], lastAt: null}; }

function observe(c, now, count) {
  const p = c.pts;
  if (p.length && count > p[p.length - 1][1]) c.lastAt = now;
  if (p.length && count < p[p.length - 1][1]) { p.length = 0; c.lastAt = null; }  // a new holder or relay
  p.push([now, count]);
  // keep the newest point at or before the window's start: it is the rate's baseline
  while (p.length > 2 && p[1][0] <= now - WINDOW_S) p.shift();
  return c;
}

function rate(c, now) {
  const p = c.pts;
  if (p.length < 2) return 0;
  let base = p[0];
  for (const q of p) { if (q[0] <= now - WINDOW_S) base = q; else break; }
  return Math.max(0, p[p.length - 1][1] - base[1]) / WINDOW_S;
}

function active(c, now) { return c.lastAt !== null && now - c.lastAt < WINDOW_S; }

// phase: what the link is doing ('attached' once the relay took it; anything else passes
// through: 'starting', 'connecting', 'retrying', 'stopped', ...). configured: the engine sent
// a model shape. keys: positions this holder (or the pool) keeps.
function derive(phase, configured, keys, c, now) {
  const r = rate(c, now), on = active(c, now);
  const ago = c.lastAt === null ? null : now - c.lastAt;
  let state = phase;
  if (phase === 'attached') state = !configured ? 'waiting' : on ? 'active' : keys ? 'holding' : 'ready';
  return {state, rate: on ? r : 0, ago};
}

const api = {WINDOW_S, counter, observe, rate, active, derive};
if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.KVState = api;
})(typeof window !== 'undefined' ? window : globalThis);

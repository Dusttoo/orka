#!/usr/bin/env node
// vqa-capture.mjs -- the headless Stage 4 visual-QA engine.
//
// The headless capture engine behind run-visual-qa.sh: full-page shots at
// desktop + mobile widths, plus the deterministic signals an unattended run
// needs to FAIL a broken page without a human looking:
//
//   - final navigation HTTP status per route (>=400 is a hard fail)
//   - uncaught page errors (window.onerror / unhandledrejection)
//   - non-noise console errors (see the noise filter below; override via env)
//   - failed same-origin network requests
//   - a blank / error-boundary heuristic (Next error overlay, "Application error",
//     near-empty body)
//   - a navigation timeout (page never reached networkidle) is a hard fail
//
// It writes one PNG per route/width plus a machine-readable manifest.json, and exits
// non-zero if any route hard-fails. The visual-QA agent then reads manifest.json for
// the deterministic verdict and the PNGs for the design/AC comparison vision can do
// but a script cannot.
//
// Invoked by run-visual-qa.sh; you normally do not call this directly. Inputs via env:
//   BASE_URL       base origin, e.g. http://localhost:3000
//   OUT            output dir (PNGs + manifest.json)
//   ROUTES_JSON    JSON array of route paths, e.g. ["/membership","/perks"]
//   STORAGE_STATE  (optional) path to a Playwright storageState JSON for authed surfaces
//   WIDTHS_JSON    (optional) override viewports; default desktop 1280 + mobile 390
//   NAV_TIMEOUT_MS (optional) per-route navigation timeout, default 30000
//   BLANK_MIN_CHARS(optional) min body innerText length to not count as blank, default 40

import { chromium } from 'playwright';
import {
  applyViewportRequestEvidence,
  completeCaptureManifest,
  observeCaptureRequests,
  recordCompletedRoute,
} from './vqa-capture-state.mjs';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';

const baseUrl = (process.env.BASE_URL || 'http://localhost:3000').replace(/\/$/, '');
const out = process.env.OUT;
const routes = JSON.parse(process.env.ROUTES_JSON || '[]');
const storageState = process.env.STORAGE_STATE && existsSync(process.env.STORAGE_STATE)
  ? process.env.STORAGE_STATE
  : undefined;
const widths = process.env.WIDTHS_JSON
  ? JSON.parse(process.env.WIDTHS_JSON)
  : [{ name: 'desktop', w: 1280, h: 900 }, { name: 'mobile', w: 390, h: 844 }];
const navTimeout = Number(process.env.NAV_TIMEOUT_MS || 30000);
const blankMinChars = Number(process.env.BLANK_MIN_CHARS || 40);

if (!out) { console.error('OUT env is required'); process.exit(2); }
if (!Array.isArray(routes) || routes.length === 0) {
  console.error('ROUTES_JSON must be a non-empty JSON array'); process.exit(2);
}
mkdirSync(out, { recursive: true });

// --- Console / request noise filter -----------------------------------------
// Some console/network errors are third-party infrastructure noise, not app
// bugs, and must not fail the gate. The defaults below cover common analytics
// beacons; override the fragment list for your app via NOISE_FRAGMENTS_JSON
// (a JSON array of substrings). Keep this list in sync with whatever noise
// filter your e2e suite uses.
const KNOWN_NOISE_FRAGMENTS = process.env.NOISE_FRAGMENTS_JSON
  ? JSON.parse(process.env.NOISE_FRAGMENTS_JSON)
  : [
      '/_vercel/insights/script.js',
      '/_vercel/speed-insights/script.js',
    ];
const KNOWN_NOISE_PATTERNS = [/Failed to load resource/i];
function isInfraNoise(text) {
  if (!text) return false;
  if (KNOWN_NOISE_FRAGMENTS.some((f) => text.includes(f))) return true;
  return KNOWN_NOISE_PATTERNS.some((re) => re.test(text));
}

// Markers that a page rendered an error state rather than the feature.
const ERROR_MARKERS = [
  'Application error: a client-side exception',
  'Unhandled Runtime Error',
  'Build Error',
  'This page could not be found', // Next default 404 body
  'Internal Server Error',
];

const slug = (r) => (r === '/' ? 'home' : r.replace(/^\//, '').replace(/[^\w-]+/g, '_')) || 'home';

const manifest = {
  baseUrl,
  authed: Boolean(storageState),
  generatedAt: new Date().toISOString(),
  routes: [],
  summary: { total: routes.length, hardFailures: 0, verdict: 'PASS' },
};

const browser = await chromium.launch();

for (const route of routes) {
  const url = baseUrl + (route.startsWith('/') ? route : `/${route}`);
  const entry = {
    route,
    url,
    shots: [],
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    ignoredRequestFailures: [],
    hardFail: false,
    reasons: [],
  };

  for (const { name, w, h } of widths) {
    const ctx = await browser.newContext({
      viewport: { width: w, height: h },
      ...(storageState ? { storageState } : {}),
    });
    const page = await ctx.newPage();
    const pageErrorsAtStart = entry.pageErrors.length;
    const observedRequests = observeCaptureRequests(page, { baseUrl, isInfraNoise });

    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      const text = msg.text();
      if (isInfraNoise(text)) return;
      entry.consoleErrors.push(text);
    });
    page.on('pageerror', (err) => entry.pageErrors.push(String(err?.message || err)));
    const shot = { width: w, name, file: null, status: null, blank: null, timedOut: false };
    try {
      const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: navTimeout });
      shot.status = resp ? resp.status() : null;

      const bodyText = (await page.evaluate(() => document.body?.innerText || '')).trim();
      const hasErrorMarker = ERROR_MARKERS.some((m) => bodyText.includes(m));
      shot.blank = bodyText.length < blankMinChars || hasErrorMarker;

      const file = `${out}/${slug(route)}.${name}.png`;
      await page.screenshot({ path: file, fullPage: true });
      shot.file = file;
    } catch (e) {
      shot.timedOut = true;
      entry.reasons.push(`${name}: navigation failed/timed out -- ${e.message}`);
    } finally {
      const pageErrors = entry.pageErrors.slice(pageErrorsAtStart);
      await applyViewportRequestEvidence(entry, observedRequests, {
        viewport: name,
        expectedStateReached: !shot.timedOut && (shot.status == null || shot.status < 400) && shot.blank === false,
        pageErrors,
      });
      await ctx.close();
    }
    entry.shots.push(shot);
  }

  // --- Deterministic hard-fail rules ---------------------------------------
  recordCompletedRoute(manifest, entry);
  const tag = entry.hardFail ? 'HARD-FAIL' : 'ok';
  console.log(`[${tag}] ${route} -> status ${entry.shots.map((s) => s.status ?? 'ERR').join('/')}${entry.reasons.length ? '  (' + entry.reasons.join('; ') + ')' : ''}`);
}

await browser.close();

completeCaptureManifest(manifest);
writeFileSync(`${out}/manifest.json`, JSON.stringify(manifest, null, 2));

console.log(`\nmanifest: ${out}/manifest.json`);
console.log(`deterministic verdict: ${manifest.summary.verdict} (${manifest.summary.hardFailures}/${manifest.summary.total} routes hard-failed)`);

// Non-zero exit on any hard failure so the gate stops the merge without a human.
process.exit(manifest.summary.verdict === 'FAIL' ? 1 : 0);

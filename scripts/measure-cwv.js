#!/usr/bin/env node
/**
 * scripts/measure-cwv.js
 *
 * Boots `next start` on a random free port, runs Lighthouse against the home
 * page, prints TTI, LCP and CLS, then exits non-zero when LCP > 4000 ms.
 *
 * Usage:
 *   npm run measure-cwv
 *
 * Requirements:
 *   - The project must have been built first (`npm run build`).
 *   - Google Chrome / Chromium must be installed.
 *   - Dependencies: lighthouse, chrome-launcher (both in devDependencies).
 */

'use strict';

const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');

const LCP_THRESHOLD_MS = 4000;
const ROOT = path.resolve(__dirname, '..');

/** Finds a free TCP port on 127.0.0.1. */
function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on('error', reject);
  });
}

/** Polls a URL until it responds 2xx or times out. */
function waitForServer(url, { retries = 40, intervalMs = 500 } = {}) {
  return new Promise((resolve, reject) => {
    let attempts = 0;

    function attempt() {
      attempts += 1;
      const req = http.get(url, (res) => {
        res.resume(); // drain
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve();
        } else if (attempts >= retries) {
          reject(new Error(`Server at ${url} did not become ready after ${retries} attempts`));
        } else {
          setTimeout(attempt, intervalMs);
        }
      });
      req.setTimeout(800, () => req.destroy());
      req.on('error', () => {
        if (attempts >= retries) {
          reject(new Error(`Server at ${url} did not become ready after ${retries} attempts`));
        } else {
          setTimeout(attempt, intervalMs);
        }
      });
    }

    attempt();
  });
}

async function main() {
  const port = await findFreePort();
  const baseUrl = `http://localhost:${port}`;

  console.log(`[measure-cwv] Starting Next.js server on port ${port}…`);

  const server = spawn(
    process.execPath, // node
    [require.resolve('next/dist/bin/next'), 'start', '--port', String(port)],
    {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PORT: String(port) },
    },
  );

  let serverExited = false;
  server.stdout.on('data', () => {}); // drain stdout
  server.stderr.on('data', (d) => process.stderr.write(d));
  server.on('exit', (code) => {
    serverExited = true;
    if (code !== null && code !== 0) {
      console.error(`[measure-cwv] next start exited with code ${code}`);
    }
  });

  try {
    await waitForServer(`${baseUrl}/`);
    console.log(`[measure-cwv] Server ready — running Lighthouse against ${baseUrl}`);

    // Dynamic requires so the script fails gracefully if devDeps are absent
    const chromeLauncher = require('chrome-launcher'); // eslint-disable-line global-require
    const lighthouse = require('lighthouse'); // eslint-disable-line global-require

    const chrome = await chromeLauncher.launch({
      chromeFlags: ['--headless', '--no-sandbox', '--disable-gpu'],
    });

    let lhr;
    try {
      const runnerResult = await lighthouse(baseUrl, {
        port: chrome.port,
        output: 'json',
        logLevel: 'error',
        onlyCategories: ['performance'],
      });
      lhr = runnerResult.lhr;
    } finally {
      await chrome.kill();
    }

    const audits = lhr.audits;
    const lcp = audits['largest-contentful-paint']?.numericValue ?? null;
    const tti = audits['interactive']?.numericValue ?? null;
    const cls = audits['cumulative-layout-shift']?.numericValue ?? null;

    console.log('\n=== Core Web Vitals ===');
    console.log(`LCP : ${lcp !== null ? `${lcp.toFixed(0)} ms` : 'n/a'}`);
    console.log(`TTI : ${tti !== null ? `${tti.toFixed(0)} ms` : 'n/a'}`);
    console.log(`CLS : ${cls !== null ? cls.toFixed(3) : 'n/a'}`);
    console.log('=======================\n');

    if (lcp !== null && lcp > LCP_THRESHOLD_MS) {
      console.error(
        `[measure-cwv] FAIL — LCP ${lcp.toFixed(0)} ms exceeds threshold of ${LCP_THRESHOLD_MS} ms`,
      );
      process.exitCode = 1;
    } else {
      console.log('[measure-cwv] PASS — LCP within acceptable range');
    }
  } finally {
    if (!serverExited) {
      server.kill('SIGTERM');
    }
  }
}

main().catch((err) => {
  console.error('[measure-cwv] Fatal error:', err.message);
  process.exit(1);
});

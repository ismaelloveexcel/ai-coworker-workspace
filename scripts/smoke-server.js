/* eslint-disable @typescript-eslint/no-require-imports */
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const PORT = 3000;

const server = http.createServer((req, res) => {
  const base = `http://localhost:${PORT}`;
  const parsed = new URL(req.url || '/', base);
  const pathname = parsed.pathname;

  if (pathname === '/') {
    const html = fs.readFileSync(
      path.join(__dirname, '..', 'frontend', 'index.html'),
      'utf-8'
    );
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
  } else if (pathname === '/success') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(
      '<!DOCTYPE html><html><body><h1>Task complete</h1></body></html>'
    );
  } else if (pathname === '/pack') {
    const data = parsed.searchParams.get('data') ?? '';
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(
      `<!DOCTYPE html><html><body><h1>Pack result</h1><p>packed:${data}</p></body></html>`
    );
  } else {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  }
});

server.listen(PORT, () => {
  console.log(`Smoke server listening on http://localhost:${PORT}`);
});

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: /.*\.spec\.ts/,
  use: {
    baseURL: 'http://127.0.0.1:3100',
  },
  webServer: {
    command: 'npx tsx scripts/smoke-server.ts',
    url: 'http://127.0.0.1:3100',
    env: { PORT: '3100' },
    reuseExistingServer: !process.env.CI,
    timeout: 15000,
  },
});

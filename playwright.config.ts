import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.ts',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://localhost:3000',
  },
  webServer: {
    command: 'npx tsx scripts/smoke-server.ts',
    port: 3000,
    reuseExistingServer: process.env['PW_REUSE_EXISTING_SERVER'] === 'true',
    timeout: 15_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});

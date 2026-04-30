import { test, expect } from '@playwright/test';

test('homepage loads with app title', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/AI Coworker/);
});

test('/success?test=1 returns task complete message', async ({ page }) => {
  await page.goto('/success?test=1');
  await expect(page.locator('body')).toContainText('Task complete');
});

test('/pack?data=<fixture> returns packed data', async ({ page }) => {
  await page.goto('/pack?data=fixture');
  await expect(page.locator('body')).toContainText('packed:fixture');
});

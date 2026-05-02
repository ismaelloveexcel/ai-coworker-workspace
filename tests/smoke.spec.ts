import { test, expect } from '@playwright/test';

test('homepage loads with app title', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/AI Coworker/);
  await expect(page.getByRole('heading', { name: 'AI Coworker Command Center' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Factory Line' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Run Agent' })).toBeVisible();
});

test('homepage shows operator studio panels', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Artifact Studio' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Recipe Library' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Run History' })).toBeVisible();
});

test('/success?test=1 returns task complete message', async ({ page }) => {
  await page.goto('/success?test=1');
  await expect(page.locator('body')).toContainText('Task complete');
});

test('/pack?data=<fixture> returns packed data', async ({ page }) => {
  await page.goto('/pack?data=fixture');
  await expect(page.locator('body')).toContainText('packed:fixture');
});

test('motion budget: __motionOK flag is a boolean', async ({ page }) => {
  await page.goto('/');
  const motionOK = await page.evaluate(() => window.__motionOK);
  expect(typeof motionOK).toBe('boolean');
});

test('motion budget: reduced-motion mode disables animations', async ({ browser }) => {
  // Emulate the OS "prefers-reduced-motion: reduce" setting
  const ctx = await browser.newContext({ reducedMotion: 'reduce' });
  const page = await ctx.newPage();
  await page.goto('/');

  // window.__motionOK must be false when OS motion preference is "reduce"
  const motionOK = await page.evaluate(() => window.__motionOK);
  expect(motionOK).toBe(false);

  // Verify the CSS override is in effect: animation-duration on body should be ~0
  const animDuration = await page.evaluate(() =>
    getComputedStyle(document.body).animationDuration
  );
  // Browsers normalise 0.01ms to either '0s' or '0.00001s' depending on the UA.
  // The key constraint is that it is negligibly short (< 1ms).
  const ms = parseFloat(animDuration) * (animDuration.endsWith('ms') ? 1 : 1000);
  expect(ms).toBeLessThan(1);

  await ctx.close();
});

test('motion budget: full-motion mode exposes motionOK=true', async ({ browser }) => {
  const ctx = await browser.newContext({ reducedMotion: 'no-preference' });
  const page = await ctx.newPage();
  await page.goto('/');
  const motionOK = await page.evaluate(() => window.__motionOK);
  expect(motionOK).toBe(true);
  await ctx.close();
});

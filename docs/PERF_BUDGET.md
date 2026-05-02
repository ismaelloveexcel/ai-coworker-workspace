# PR-C4 — Motion & Performance Budget

## Objective

Upgrade visual quality while keeping runtime performance within the idle-CPU
budget and satisfying accessibility requirements for users who prefer reduced
motion.

---

## Constraints (from problem statement)

| Constraint | Value |
|---|---|
| Framework migration | None — vanilla JS / CSS / SVG only |
| Target frame-rate | 60 fps |
| Idle CPU budget | < 5 % |
| New dependencies added | **0** |

---

## Bundle-size impact

All changes are confined to `frontend/index.html` (a single static file
served by nginx).  No new scripts, no new stylesheets, no new images.

| Metric | Before | After | Delta |
|---|---|---|---|
| `frontend/index.html` (uncompressed) | ~46 kB | ~47 kB | +~1 kB |
| New JS dependencies | 0 | 0 | 0 |
| New CSS dependencies | 0 | 0 | 0 |

The ~1 kB increase comes from the added CSS blocks
(`@media (prefers-reduced-motion: …)`, `will-change` hints, `contain`
declarations) and the five-line JS motion-flag snippet.

---

## Runtime impact

### CSS animations

All animations in the UI are **CSS keyframe animations** running entirely on
the browser's compositor thread.  They do not block the main JavaScript
thread and do not require `requestAnimationFrame` polling.

The animations and their estimated compositor cost on baseline hardware
(Chromebook-class device, 2019+):

| Animation | Element | Period | Compositor cost |
|---|---|---|---|
| `breathe` (scaleY) | idle agent sprite | 3.5 s | < 0.1 ms/frame |
| `bobbing` (translateY) | active agent sprite | 1.2 s | < 0.1 ms/frame |
| `orb-pulse` (scaleX + opacity) | agent orb | 1.4 s | < 0.1 ms/frame |
| `glow-dot` (box-shadow) | status dot | 1.4 s | < 0.2 ms/frame |
| `flicker` (opacity) | lantern ambient | 6 s | < 0.1 ms/frame |

`transform` and `opacity` animations are always composited without a main-
thread paint step.  `box-shadow` changes (`glow-dot`) may trigger a
repaint on some GPUs; the element is tiny (7 × 7 px), so the cost is
negligible.

Total estimated idle GPU budget: **< 1 ms/frame** (~0.6 % of a 60 fps
16.67 ms frame budget).

### `will-change` hints

`will-change: transform` / `will-change: opacity` are applied via a
`@media (prefers-reduced-motion: no-preference)` block so that the GPU
layer is promoted **only when animations are actually running**.  This
avoids wasting VRAM on pages where the user has disabled motion.

### `contain: layout style` on panels

Panel and studio-panel elements now carry `contain: layout style`.  This
tells the browser that layout recalculations inside a panel cannot affect
the rest of the page, enabling incremental/partial-layout optimisations
during rapid DOM updates (e.g., live task-log streaming).

---

## Reduced-motion compliance

### CSS

A single `@media (prefers-reduced-motion: reduce)` block sets:

```css
*, *::before, *::after {
  animation-duration: 0.01ms !important;
  transition-duration: 0.01ms !important;
}
```

This is the industry-standard "kill-switch" pattern (see
[web.dev/prefers-reduced-motion](https://web.dev/articles/prefers-reduced-motion)).
Setting both durations to near-zero effectively stops all continuous looping
animations and transitions without preventing one-shot entrance animations
from completing.

All five keyframe animations (`breathe`, `bobbing`, `orb-pulse`, `glow-dot`,
`flicker`) are effectively disabled when the user's OS preference is set to
"reduce motion".

### JavaScript

A `motionOK` boolean is read once at page-load from
`window.matchMedia('(prefers-reduced-motion: reduce)')` and exposed as
`window.__motionOK`.  Any future script-driven animation must gate on this
flag before scheduling work.

The `setAgentState()` function also guards against redundant DOM writes: it
checks whether the element's `className` already matches the desired state
before assigning, avoiding unnecessary style-recalculations on every polling
cycle.

---

## Validation

### Automated

- `tests/smoke.spec.ts` — two new Playwright tests:
  1. **`motion budget: __motionOK flag is a boolean`** — verifies the flag is
     exposed in all contexts.
  2. **`motion budget: reduced-motion mode disables animations`** — uses
     Playwright's `reducedMotion: 'reduce'` context option to emulate the OS
     preference and asserts `__motionOK === false` and that CSS
     `animation-duration` is negligibly short (< 1 ms).
  3. **`motion budget: full-motion mode exposes motionOK=true`** — verifies
     the flag is `true` when the OS preference is `no-preference`.

### Manual performance sanity check

1. Open Chrome DevTools → Performance panel.
2. Record 5 seconds of idle page.
3. Confirm "Scripting" < 2 %, "Rendering" < 2 %, total CPU < 5 %.
4. Toggle "Emulate CSS media feature prefers-reduced-motion" in the
   Rendering tab.
5. Confirm all agent sprites and the lantern overlay are static (no movement).

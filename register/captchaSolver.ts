// Captcha solver - auto mode with Sobel edge detection + manual fallback
// Automatically solves Aliyun slide puzzle captcha using browser-side Canvas analysis

import type { Page } from 'playwright';
import type { CaptchaSolveResult, CaptchaMode } from './types';
import { log } from './utils/logger';
import { AUTO_MAX_ATTEMPTS, AUTO_TOTAL_TIMEOUT_MS, canStartAutoCaptchaAttempt } from './utils/captchaPolicy';

/** Maximum time to wait for user to solve captcha in manual mode (ms) */
const MANUAL_TIMEOUT_MS = 180_000;

/** Poll interval to check if captcha is solved (ms) */
const POLL_INTERVAL_MS = 500;

// Captcha detection text patterns (actual page uses these Chinese strings)
const CAPTCHA_DETECT_TEXTS = [
  '请完成人机验证',
  '请完成安全验证',
  '点击开始验证',
  '请拖动滑块完成拼图',
  '人机验证',
  '为了你的账号安全，请完成人机验证', // actual heading text on signup captcha step
];

// Success indicators: captcha solved or moved to next step
const SUCCESS_INDICATORS = [
  '验证成功',
  '验证邮箱', // OTP page appeared = captcha passed
  '验证码', // verification code input appeared
  'OTP',
  '验证您的邮箱',
];

// ============================================================================
// Main Entry Point
// ============================================================================

/**
 * Solve Aliyun slide puzzle captcha.
 *
 * @param page - Playwright page instance
 * @param email - Account email (for logging)
 * @param mode - 'auto' for automatic solving, 'manual' for user prompt
 */
export async function solveCaptcha(
  page: Page,
  email: string,
  mode: CaptchaMode = 'auto',
): Promise<CaptchaSolveResult> {
  // Inject __name polyfill into browser context.
  // tsx/esbuild injects __name(fn, "name") wrappers around function declarations
  // when compiling TypeScript. When page.evaluate() serializes these functions
  // into the browser, __name doesn't exist there, causing ReferenceError.
  // Use string form to prevent esbuild from transforming this callback itself.
  await page.evaluate(`
    if (typeof globalThis.__name === 'undefined') {
      globalThis.__name = function(fn) { return fn; };
    }
  `);

  if (mode === 'auto') {
    return solveCaptchaAuto(page, email);
  }
  return solveCaptchaManual(page, email);
}

// Legacy export for backward compatibility
export { solveCaptchaManual };

// ============================================================================
// Auto Mode - Sobel Edge Detection + Human-like Drag
// ============================================================================

/**
 * Automatically solve captcha using edge detection algorithm.
 * Retries indefinitely (with refresh) until solved or total timeout reached.
 * Never falls back to manual mode.
 */
async function solveCaptchaAuto(page: Page, email: string): Promise<CaptchaSolveResult> {
  const startTime = Date.now();

  // Wait for captcha to appear
  try {
    await page.waitForFunction(
      (texts: string[]) => {
        const bodyText = document.body.innerText || '';
        return texts.some((t) => bodyText.includes(t));
      },
      CAPTCHA_DETECT_TEXTS,
      { timeout: 10_000 },
    );
  } catch {
    if (await isPostCaptchaPage(page)) {
      log.info('[Auto] Captcha not detected, already at verification step');
      return { success: true, durationMs: Date.now() - startTime };
    }
    log.info('[Auto] No captcha detected, continuing...');
    return { success: true, durationMs: Date.now() - startTime };
  }

  log.info('[Auto] Captcha detected, starting auto-solve...', { email });

  // Click "点击开始验证" to trigger slide puzzle
  const startBtn = page.locator('text=点击开始验证');
  try {
    await startBtn.waitFor({ state: 'visible', timeout: 5000 });
    await startBtn.click();
    // Wait for puzzle elements to fully load after clicking start
    await waitForPuzzleElements(page, 10000);
  } catch {
    // Start button not found - puzzle may already be visible, but wait for elements
    log.warn('[Auto] Start button not found, checking if puzzle is already visible...');
    const puzzleLoaded = await waitForPuzzleElements(page, 8000);
    if (!puzzleLoaded) {
      log.warn('[Auto] Puzzle elements not found, may need manual intervention');
    }
  }

  // Continuous auto-retry loop until solved or total timeout
  let attempt = 0;
  while (canStartAutoCaptchaAttempt(attempt, startTime, Date.now())) {
    attempt++;
    log.info(`[Auto] Attempt ${attempt}...`);

    // Check if already solved (between retries)
    if (await isPostCaptchaPage(page)) {
      log.info('[Auto] Captcha already solved!', { durationMs: Date.now() - startTime });
      return { success: true, durationMs: Date.now() - startTime };
    }

    // Ensure puzzle images are loaded
    const loaded = await waitForPuzzleImages(page, 8000);
    if (!loaded) {
      log.warn('[Auto] Puzzle images not loaded, refreshing...');
      await clickRefresh(page);
      await waitForPuzzleElements(page, 8000);
      continue;
    }

    // Detect gap position
    const gapInfo = await detectGapPosition(page);
    if (!gapInfo) {
      log.warn('[Auto] Gap detection failed, refreshing...');
      await clickRefresh(page);
      await waitForPuzzleElements(page, 8000);
      continue;
    }

    log.info('[Auto] Gap detected', {
      x: gapInfo.rX,
      targetPuzzleCSS: gapInfo.targetPuzzleCSS,
      gapDisplayX: Math.round(gapInfo.gapDisplayX),
      puzzleDisplayX: Math.round(gapInfo.puzzleDisplayX),
      displayWidth: Math.round(gapInfo.displayWidth),
    });

    // Execute human-like drag with feedback loop
    const dragResult = await executeHumanDrag(page, gapInfo.targetPuzzleCSS);
    if (!dragResult.success) {
      log.warn('[Auto] Drag execution failed', { error: dragResult.error });
      await clickRefresh(page);
      // Wait for new puzzle elements to fully load after refresh
      await waitForPuzzleElements(page, 8000);
      continue;
    }

    // Wait and check result
    await page.waitForTimeout(2500);

    if (await isPostCaptchaPage(page)) {
      const elapsed = Date.now() - startTime;
      log.info('[Auto] Captcha solved successfully!', { attempt, durationMs: elapsed });
      return { success: true, durationMs: elapsed };
    }

    // Failed — refresh and retry
    if (await anyCaptchaVisible(page)) {
      log.warn(`[Auto] Attempt ${attempt} failed, captcha still visible. Refreshing...`);
      await clickRefresh(page);
      // Wait for new puzzle elements to fully load after refresh
      await waitForPuzzleElements(page, 8000);
    }
  }

  const elapsed = Date.now() - startTime;
  if (attempt >= AUTO_MAX_ATTEMPTS) {
    log.error(`[Auto] Reached max attempts (${AUTO_MAX_ATTEMPTS}) after ${elapsed}ms`);
    return { success: false, error: `auto-solve exhausted after ${AUTO_MAX_ATTEMPTS} attempts`, durationMs: elapsed };
  }
  log.error(`[Auto] Auto-solve timeout after ${attempt} attempts (${elapsed}ms)`);
  return { success: false, error: `auto-solve timeout after ${attempt} attempts`, durationMs: elapsed };
}

/**
 * Wait for puzzle background and piece images to be fully loaded.
 */
async function waitForPuzzleImages(page: Page, timeoutMs = 5000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const loaded = await page.evaluate(() => {
      const bg = document.getElementById('aliyunCaptcha-img') as HTMLImageElement | null;
      const pz = document.getElementById('aliyunCaptcha-puzzle') as HTMLImageElement | null;
      return bg && pz && bg.naturalWidth > 0 && pz.naturalWidth > 0;
    });
    if (loaded) return true;
    await page.waitForTimeout(200);
  }
  return false;
}

/**
 * Wait for all puzzle elements to be ready: images + slider.
 * Returns true when all elements are visible and loaded.
 */
async function waitForPuzzleElements(page: Page, timeoutMs = 8000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const ready = await page.evaluate(() => {
      const bg = document.getElementById('aliyunCaptcha-img') as HTMLImageElement | null;
      const pz = document.getElementById('aliyunCaptcha-puzzle') as HTMLImageElement | null;
      const slider = document.getElementById('aliyunCaptcha-sliding-slider') as HTMLElement | null;
      const bgLoaded = bg && bg.naturalWidth > 0;
      const pzLoaded = pz && pz.naturalWidth > 0;
      const sliderVisible = slider && slider.offsetParent !== null;
      return bgLoaded && pzLoaded && sliderVisible;
    });
    if (ready) return true;
    await page.waitForTimeout(300);
  }
  return false;
}

/**
 * Click refresh button to get new puzzle.
 * Waits for the button to be visible before clicking.
 */
async function clickRefresh(page: Page): Promise<void> {
  try {
    // Try multiple refresh button selectors
    const refreshSelectors = [
      '#aliyunCaptcha-btn-refresh',
      'button:has-text("刷新验证码")',
      '#aliyunCaptcha-img-box button',
      '.aliyunCaptcha-btn-refresh',
    ];
    for (const sel of refreshSelectors) {
      const btn = page.locator(sel).first();
      if (await btn.isVisible().catch(() => false)) {
        await btn.click();
        // Wait for old puzzle to unload and new one to start loading
        await page.waitForTimeout(1000);
        return;
      }
    }
    log.warn('[Auto] No refresh button found');
  } catch {
    // Ignore refresh errors
  }
}

// ============================================================================
// Gap Detection Algorithm (Sobel Edge + Template Matching)
// ============================================================================

interface GapDetectionResult {
  rX: number;       // Refined gap X position in natural coordinates
  pMnX: number;     // Puzzle shape min X (alpha boundary)
  sW: number;       // Puzzle shape width
  scale: number;    // Display scale (displayWidth / naturalWidth)
  dragDist: number; // Estimated drag distance (unreliable due to non-linear mapping)
  bW: number;       // Background natural width
  gapDisplayX: number;     // Gap position in display coordinates
  puzzleDisplayX: number;  // Puzzle piece current position in display coordinates
  displayWidth: number;    // Actual display width from DOM
  targetPuzzleCSS: number; // Target puzzle CSS left value (used by feedback-loop drag)
}

/**
 * Detect the gap position using Sobel edge detection and template matching.
 *
 * Algorithm:
 * 1. Fetch images as base64 to bypass CORS canvas taint
 * 2. Apply Sobel X filter to detect vertical edges
 * 3. Project edge energy by column
 * 4. Find window with max left+right edge energy
 * 5. Refine using puzzle edge shape matching
 */
async function detectGapPosition(page: Page): Promise<GapDetectionResult | null> {
  return page.evaluate(async () => {
    // Fetch image as base64 to bypass CORS taint
    // NOTE: Use arrow functions (not named function declarations) to avoid
    // esbuild injecting __name() wrappers that don't exist in browser context.
    const imgToBase64 = async (url: string): Promise<string> => {
      // If already data URL, return as-is
      if (url.startsWith('data:')) return url;
      const resp = await fetch(url);
      const blob = await resp.blob();
      return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result as string);
        reader.readAsDataURL(blob);
      });
    };

    // Load image from data URL
    const loadImg = (dataUrl: string): Promise<HTMLImageElement> => {
      return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = dataUrl;
      });
    };

    const bgEl = document.getElementById('aliyunCaptcha-img') as HTMLImageElement | null;
    const pzEl = document.getElementById('aliyunCaptcha-puzzle') as HTMLImageElement | null;
    if (!bgEl || !pzEl || !bgEl.src || !pzEl.src) return null;

    try {
      // IMPORTANT: Capture DOM positions BEFORE any canvas operations.
      // Some captcha SDKs hide elements when canvas taint is detected.
      // getBoundingClientRect() must run while elements are still visible.
      const bgRect = bgEl.getBoundingClientRect();
      const pzRect = pzEl.getBoundingClientRect();
      const hasDomPositions = bgRect.width > 0 && pzRect.width > 0;

      const bgB64 = await imgToBase64(bgEl.src);
      const pzB64 = await imgToBase64(pzEl.src);

      const bgImg = await loadImg(bgB64);
      const puzzleImg = await loadImg(pzB64);

      // Create canvases
      const bgC = document.createElement('canvas');
      const pC = document.createElement('canvas');
      bgC.width = bgImg.naturalWidth;
      bgC.height = bgImg.naturalHeight;
      pC.width = puzzleImg.naturalWidth;
      pC.height = puzzleImg.naturalHeight;

      const bgCtx = bgC.getContext('2d')!;
      const pCtx = pC.getContext('2d')!;
      bgCtx.drawImage(bgImg, 0, 0);
      pCtx.drawImage(puzzleImg, 0, 0);

      const bgD = bgCtx.getImageData(0, 0, bgC.width, bgC.height);
      const pD = pCtx.getImageData(0, 0, pC.width, pC.height);
      const bW = bgC.width, bH = bgC.height;
      const pW = pC.width, pH = pC.height;

      // Grayscale helper (arrow function to avoid esbuild __name injection)
      const gray = (data: ImageData, w: number, x: number, y: number): number => {
        const i = (y * w + x) * 4;
        return 0.299 * data.data[i] + 0.587 * data.data[i + 1] + 0.114 * data.data[i + 2];
      };

      // Combined Sobel X + Y edge detection for better gap boundary detection
      // Using both directions captures the full gap outline (vertical AND horizontal edges)
      const eMap = new Float32Array(bW * bH);
      for (let y = 1; y < bH - 1; y++) {
        for (let x = 1; x < bW - 1; x++) {
          const gx =
            -gray(bgD, bW, x - 1, y - 1) + gray(bgD, bW, x + 1, y - 1) +
            -2 * gray(bgD, bW, x - 1, y) + 2 * gray(bgD, bW, x + 1, y) +
            -gray(bgD, bW, x - 1, y + 1) + gray(bgD, bW, x + 1, y + 1);
          const gy =
            -gray(bgD, bW, x - 1, y - 1) - 2 * gray(bgD, bW, x, y - 1) - gray(bgD, bW, x + 1, y - 1) +
            gray(bgD, bW, x - 1, y + 1) + 2 * gray(bgD, bW, x, y + 1) + gray(bgD, bW, x + 1, y + 1);
          eMap[y * bW + x] = Math.sqrt(gx * gx + gy * gy);
        }
      }

      // Column energy projection (skip top/bottom 10%)
      const mY = Math.floor(bH * 0.1);
      const cE = new Float32Array(bW);
      for (let x = 0; x < bW; x++) {
        let s = 0;
        for (let y = mY; y < bH - mY; y++) s += eMap[y * bW + x];
        cE[x] = s;
      }

      // Find puzzle shape bounds via alpha channel
      let pMnX = pW, pMxX = 0;
      for (let y = 0; y < pH; y++) {
        for (let x = 0; x < pW; x++) {
          if (pD.data[(y * pW + x) * 4 + 3] > 128) {
            if (x < pMnX) pMnX = x;
            if (x > pMxX) pMxX = x;
          }
        }
      }
      const sW = pMxX - pMnX + 1;

      // Determine search start: skip puzzle piece current region to avoid
      // matching the puzzle piece's own edges as the "gap"
      let searchStart: number;
      if (hasDomPositions) {
        // Use actual DOM position to compute where puzzle piece is in natural coords
        const pzCurrentLeft = pzRect.left - bgRect.left;
        const pzCurrentLeftNat = pzCurrentLeft * (bW / bgRect.width);
        searchStart = Math.max(sW, Math.round(pzCurrentLeftNat + pW + 10));
      } else {
        // Fallback: puzzle piece image width is small (cropped), typically starts at left=0
        // Skip past the puzzle image width + margin in natural coords
        searchStart = Math.max(sW, pW + 10);
      }

      // Extract puzzle edge pixels (boundary of opaque shape)
      // NOTE: Must extract BEFORE candidate scoring, since template matching
      // uses these edge pixels to evaluate each candidate position.
      const ePs: Array<{ x: number; y: number }> = [];
      for (let y = 1; y < pH - 1; y++) {
        for (let x = 1; x < pW - 1; x++) {
          if (pD.data[(y * pW + x) * 4 + 3] > 128) {
            const nb = [
              pD.data[((y - 1) * pW + x) * 4 + 3],
              pD.data[((y + 1) * pW + x) * 4 + 3],
              pD.data[(y * pW + (x - 1)) * 4 + 3],
              pD.data[(y * pW + (x + 1)) * 4 + 3],
            ];
            if (nb.some(a => a < 128)) ePs.push({ x, y });
          }
        }
      }

      // Multi-candidate window search + 2D template matching
      //
      // Problem with single-peak approach:
      //   The window search cE[x] + cE[x+sW] can find a false peak at
      //   (gap_left - sW) because the gap's left border edge is very strong.
      //   Combined with any natural edge at (gap_left - sW), it scores higher
      //   than the true gap at gap_left. The old ±40px refinement range was
      //   smaller than sW (~49px), so shape matching could not correct it.
      //   Result: puzzle RIGHT side aligns with gap LEFT side.
      //
      // Fix: Collect top-N candidate peaks from window search, then score
      // each with full 2D shape template matching. The true gap has the
      // strongest shape-edge correlation, even if column energy ranks it #2.

      // Step 1: Collect top-N candidates from column energy window search
      const NUM_CANDIDATES = 8;
      const candidates: Array<{ x: number; energy: number }> = [];
      for (let x = searchStart; x < bW - sW; x++) {
        const e = (cE[x] || 0) + (cE[x + sW] || 0);
        // Insert into sorted candidates (descending by energy)
        if (candidates.length < NUM_CANDIDATES || e > candidates[candidates.length - 1].energy) {
          // Check minimum distance from existing candidates to avoid clustering
          const tooClose = candidates.some(c => Math.abs(c.x - x) < sW * 0.5);
          if (!tooClose) {
            candidates.push({ x, energy: e });
            candidates.sort((a, b) => b.energy - a.energy);
            if (candidates.length > NUM_CANDIDATES) candidates.pop();
          }
        }
      }

      // Step 2: For each candidate, do full 2D shape template matching
      // across a ±(sW) pixel window to find the best alignment
      let rX = searchStart, bestShapeScore = -1;
      for (const cand of candidates) {
        const refMin = Math.max(searchStart, cand.x - sW);
        const refMax = Math.min(bW - sW - 1, cand.x + sW);
        for (let ox = refMin; ox <= refMax; ox++) {
          let sc = 0;
          for (const ep of ePs) {
            // Align edge pixel relative to shape origin (pMnX), not image origin
            const bx = ox + (ep.x - pMnX), by = ep.y;
            if (bx >= 1 && bx < bW - 1 && by >= 1 && by < bH - 1) {
              sc += eMap[by * bW + bx];
            }
          }
          if (sc > bestShapeScore) { bestShapeScore = sc; rX = ox; }
        }
      }

      // Calculate drag distance
      let displayWidth: number;
      let scale: number;
      let gapDisplayX: number;
      let puzzleDisplayX: number;
      let dragDist: number;

      if (hasDomPositions) {
        // Use actual DOM positions for accurate calculation
        displayWidth = bgRect.width;
        scale = displayWidth / bW;
        gapDisplayX = rX * scale;

        // Puzzle shape's display position:
        //   puzzle element offset from bg + shape offset within puzzle image
        const puzzleScale = pzRect.width / pW;
        puzzleDisplayX = (pzRect.left - bgRect.left) + pMnX * puzzleScale;

        dragDist = gapDisplayX - puzzleDisplayX;
      } else {
        // Fallback: assume standard Aliyun captcha layout (display width ≈ 300)
        // Puzzle element starts at CSS left=0, shape offset = pMnX
        displayWidth = 300;
        scale = displayWidth / bW;
        gapDisplayX = rX * scale;
        puzzleDisplayX = pMnX * scale;

        dragDist = (rX - pMnX) * scale;
      }

      // Target puzzle CSS left: the CSS `left` value the puzzle element needs
      // to reach for its shape to align with the gap.
      // When puzzle CSS left = T, the shape's left edge is at T + pMnX (since pMnX ≈ 2).
      // We need: T + pMnX = gapDisplayX → T = gapDisplayX - pMnX
      // NOTE: pMnX here is in natural puzzle coords but puzzle displays at 1:1 scale
      const targetPuzzleCSS = gapDisplayX - pMnX;

      return {
        rX, pMnX, sW, scale,
        dragDist: Math.round(dragDist),
        bW, gapDisplayX, puzzleDisplayX, displayWidth,
        targetPuzzleCSS: Math.round(targetPuzzleCSS * 10) / 10,
      };
    } catch {
      return null;
    }
  });
}

// ============================================================================
// Human-like Drag Execution
// ============================================================================

interface DragResult {
  success: boolean;
  error?: string;
}

/**
 * Execute a human-like drag using feedback loop.
 *
 * The Aliyun captcha uses a NON-LINEAR (quadratic) mapping between slider
 * position and puzzle position. A pre-computed drag distance will always be
 * wrong. Instead, we monitor the puzzle element's CSS `left` value in
 * real-time and stop when it reaches the target.
 *
 * Human-like features:
 * - Accelerate/decelerate phases
 * - Y-axis jitter (smooth random walk)
 * - Slight overshoot + correction
 * - Variable speed with micro-pauses
 *
 * @param page - Playwright page
 * @param targetPuzzleCSS - Target CSS `left` value for the puzzle element
 */
async function executeHumanDrag(page: Page, targetPuzzleCSS: number): Promise<DragResult> {
  try {
    // Get slider element position - wait for it to be visible first
    const sliderEl = page.locator('#aliyunCaptcha-sliding-slider');

    // Wait for slider to be visible (it may take time to appear after puzzle images load)
    try {
      await sliderEl.waitFor({ state: 'visible', timeout: 5000 });
    } catch {
      return { success: false, error: 'slider not visible after waiting' };
    }

    const sliderBox = await sliderEl.boundingBox();
    if (!sliderBox) {
      return { success: false, error: 'slider not found' };
    }

    const sx = sliderBox.x + sliderBox.width / 2;
    const sy = sliderBox.y + sliderBox.height / 2;

    // Read current puzzle CSS left
    const readPuzzleCSS = (): Promise<number> =>
      page.evaluate(() => {
        const pz = document.getElementById('aliyunCaptcha-puzzle');
        return pz ? parseFloat(pz.style.left) || 0 : 0;
      });

    // Smooth Y-axis random walk state
    let yWalk = 0;

    // Start drag
    await page.mouse.move(sx, sy);
    await page.waitForTimeout(80 + Math.floor(Math.random() * 120));
    await page.mouse.down();
    await page.waitForTimeout(40 + Math.floor(Math.random() * 80));

    // Phase 1: Fast approach — move slider in coarse steps until puzzle is
    // within ~15px of target. Use large increments (8-15px) for speed.
    const COARSE_TOLERANCE = 15;
    let totalSliderDx = 0;
    let currentPuzzleCSS = await readPuzzleCSS();
    let stepCount = 0;

    while (currentPuzzleCSS < targetPuzzleCSS - COARSE_TOLERANCE && stepCount < 120) {
      // Coarse step: 8-15px of slider movement
      const remaining = targetPuzzleCSS - currentPuzzleCSS;
      // Larger steps when far away, smaller steps when close
      const stepSize = remaining > 60
        ? 10 + Math.floor(Math.random() * 8)   // 10-17px
        : 5 + Math.floor(Math.random() * 6);   // 5-10px

      totalSliderDx += stepSize;
      yWalk += (Math.random() - 0.5) * 1.2;
      yWalk *= 0.85;

      await page.mouse.move(sx + totalSliderDx, sy + yWalk);

      // Variable speed
      const phase = totalSliderDx / 260; // approximate progress (slider max ~260)
      let delay: number;
      if (phase < 0.10) {
        delay = 16 + Math.floor(Math.random() * 10); // Slow start
      } else if (phase < 0.70) {
        delay = 4 + Math.floor(Math.random() * 5);   // Fast middle
      } else {
        delay = 8 + Math.floor(Math.random() * 8);   // Deceleration
      }

      // Occasional micro-pause (2% chance)
      if (Math.random() < 0.02 && phase > 0.2 && phase < 0.8) {
        delay += 30 + Math.floor(Math.random() * 50);
      }

      await page.waitForTimeout(delay);

      // Read puzzle position every 3 steps to reduce overhead
      stepCount++;
      if (stepCount % 3 === 0) {
        currentPuzzleCSS = await readPuzzleCSS();
      }
    }

    // Phase 2: Fine approach — move in tiny steps (1-3px) until within tolerance
    const FINE_TOLERANCE = 3;
    currentPuzzleCSS = await readPuzzleCSS();
    let fineSteps = 0;

    while (Math.abs(currentPuzzleCSS - targetPuzzleCSS) > FINE_TOLERANCE && fineSteps < 60) {
      const diff = targetPuzzleCSS - currentPuzzleCSS;
      // Move 1-3px slider, direction based on over/undershoot
      const fineStep = diff > 0
        ? 1 + Math.floor(Math.random() * 2)
        : -(1 + Math.floor(Math.random() * 2));

      totalSliderDx += fineStep;
      yWalk += (Math.random() - 0.5) * 0.6;
      yWalk *= 0.9;

      await page.mouse.move(sx + totalSliderDx, sy + yWalk);
      await page.waitForTimeout(12 + Math.floor(Math.random() * 12));

      fineSteps++;
      if (fineSteps % 2 === 0) {
        currentPuzzleCSS = await readPuzzleCSS();
      }
    }

    // Phase 3: Brief overshoot + settle (human micro-correction)
    const overshootPx = 2 + Math.floor(Math.random() * 3); // 2-4px slider overshoot
    totalSliderDx += overshootPx;
    yWalk += (Math.random() - 0.5) * 0.5;
    await page.mouse.move(sx + totalSliderDx, sy + yWalk);
    await page.waitForTimeout(30 + Math.floor(Math.random() * 40));

    // Correct back
    totalSliderDx -= overshootPx;
    yWalk += (Math.random() - 0.5) * 0.3;
    await page.mouse.move(sx + totalSliderDx, sy + yWalk);
    await page.waitForTimeout(20 + Math.floor(Math.random() * 30));

    // Brief hold before release (humans don't release instantly)
    await page.waitForTimeout(50 + Math.floor(Math.random() * 80));
    await page.mouse.up();

    return { success: true };
  } catch (err) {
    return { success: false, error: (err as Error).message };
  }
}

// ============================================================================
// Manual Mode - User Prompt Fallback
// ============================================================================

/**
 * Wait for the Aliyun captcha to appear, then prompt the user
 * to solve it manually. Returns when captcha is solved or times out.
 */
async function solveCaptchaManual(page: Page, email: string): Promise<CaptchaSolveResult> {
  const startTime = Date.now();

  // Wait for captcha container to appear
  // NOTE: Cannot use comma-joined `text=A, text=B` with waitForSelector —
  // Playwright treats everything after `text=` as a single text value, so the
  // comma-joined selector never matches. Use waitForFunction + innerText instead.
  try {
    await page.waitForFunction(
      (texts: string[]) => {
        const bodyText = document.body.innerText || '';
        return texts.some((t) => bodyText.includes(t));
      },
      CAPTCHA_DETECT_TEXTS,
      { timeout: 10_000 },
    );
  } catch {
    if (await isPostCaptchaPage(page)) {
      log.info('[Manual] Captcha not detected, already at verification step');
      return { success: true, durationMs: Date.now() - startTime };
    }
    log.info('[Manual] No captcha detected, continuing...');
    return { success: true, durationMs: Date.now() - startTime };
  }

  // Show prominent banner in terminal
  log.banner([
    '[CAPTCHA] Please complete the verification in the browser!',
    `Account: ${email}`,
    `Timeout: ${MANUAL_TIMEOUT_MS / 1000} seconds`,
    'Steps: Click "点击开始验证" -> Solve slide puzzle',
  ]);

  // Poll until captcha is solved or times out
  while (Date.now() - startTime < MANUAL_TIMEOUT_MS) {
    if (await isPostCaptchaPage(page)) {
      const elapsed = Date.now() - startTime;
      log.info('[Manual] Captcha solved! Moved to next step.', { durationMs: elapsed });
      return { success: true, durationMs: elapsed };
    }

    const stillVisible = await anyCaptchaVisible(page);
    if (!stillVisible) {
      const elapsed = Date.now() - startTime;
      log.info('[Manual] Captcha indicators disappeared', { durationMs: elapsed });
      return { success: true, durationMs: elapsed };
    }

    await page.waitForTimeout(POLL_INTERVAL_MS);
  }

  const elapsed = Date.now() - startTime;
  log.error('[Manual] Captcha timeout - user did not solve in time');
  return { success: false, durationMs: elapsed, error: 'captcha_timeout' };
}

// ============================================================================
// Utility Functions
// ============================================================================

/** Check if any captcha-related text is still visible */
async function anyCaptchaVisible(page: Page): Promise<boolean> {
  for (const text of CAPTCHA_DETECT_TEXTS) {
    const visible = await page.locator(`text=${text}`).first().isVisible().catch(() => false);
    if (visible) return true;
  }
  return false;
}

/** Check if the page has moved past captcha (OTP input or success) */
async function isPostCaptchaPage(page: Page): Promise<boolean> {
  for (const text of SUCCESS_INDICATORS) {
    const visible = await page.locator(`text=${text}`).first().isVisible().catch(() => false);
    if (visible) return true;
  }

  // Also check for OTP input fields
  const otpInput = await page
    .locator('input[aria-label*="OTP"], input[maxlength="1"]')
    .first()
    .isVisible()
    .catch(() => false);
  if (otpInput) return true;

  // Check if captcha container is hidden AND no captcha text is visible
  // BUG FIX: Previously, we only checked if aliyunCaptcha-img-box had 0 dimensions.
  // However, this element exists but has 0x0 size BEFORE the puzzle loads (captcha step).
  // We must also verify no captcha-related text is visible to avoid false positives.
  const captchaGone = await page.evaluate((captchaTexts: string[]) => {
    const captcha = document.getElementById('aliyunCaptcha-img-box');
    if (captcha) {
      const r = captcha.getBoundingClientRect();
      const hasZeroSize = r.width === 0 || r.height === 0;
      if (!hasZeroSize) {
        // Captcha container is visible, definitely not gone
        return false;
      }
      // Container has 0 size - check if any captcha text is still visible
      // If captcha text is visible, we're still on captcha step (not past it)
      const bodyText = document.body.innerText || '';
      const hasCaptchaText = captchaTexts.some((t) => bodyText.includes(t));
      if (hasCaptchaText) {
        // Still shows captcha-related text, not past captcha yet
        return false;
      }
      // Container hidden AND no captcha text = truly past captcha
      return true;
    }
    return false;
  }, CAPTCHA_DETECT_TEXTS);
  if (captchaGone) return true;

  return false;
}

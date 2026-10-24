// ビューワーを実ブラウザで開き、ライブ・KVS 録画・S3 録画の再生を確かめる(scripts/e2e/run.sh から呼ぶ)。
//
//   node scripts/e2e/browser.mjs --url https://dxxx.cloudfront.net --out <スクショの出力先> \
//     [--browser chrome|webkit|chromium] [--modes live,kvs,s3] [--s3-at <epoch ms>] [--camera cam-001] [--headed]
//
// Playwright は viewer/node_modules のものを使う(viewer で npm ci 済みであること)。
// chrome は本物の Google Chrome(channel: 'chrome')。同梱の Chromium は H.265 を再生できないことがある。
// 結果は 1 行 1 モードの JSON(`RESULT {...}`)で標準出力に出す。

import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../viewer/package.json'));
const { chromium, webkit } = require('playwright');

const { values: args } = parseArgs({
  options: {
    url: { type: 'string' },
    out: { type: 'string', default: 'e2e-shots' },
    browser: { type: 'string', default: 'chrome' },
    modes: { type: 'string', default: 'live,kvs,s3' },
    's3-at': { type: 'string' },
    'kvs-at': { type: 'string' },
    camera: { type: 'string', default: 'cam-001' },
    headed: { type: 'boolean', default: false },
  },
});
if (!args.url) throw new Error('--url が必要');
mkdirSync(args.out, { recursive: true });

const SOURCE_LABEL = { live: 'ライブ', kvs: '直近の録画(KVS)', s3: '録画(S3)' };

const launch = async () => {
  if (args.browser === 'webkit') return webkit.launch({ headless: !args.headed });
  if (args.browser === 'chromium') return chromium.launch({ headless: !args.headed });
  return chromium.launch({ channel: 'chrome', headless: !args.headed });
};

const browser = await launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ja-JP', colorScheme: 'light' });
const page = await ctx.newPage();
const consoleErrors = [];
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));
// どのプレイリストを読んだか(hls.js か ネイティブかに関係なく見える)
const playlists = [];
page.on('request', (r) => {
  const u = r.url();
  if (/\.m3u8|getHLS|vod\.m3u8/.test(u)) playlists.push(u.replace(/\?.*/, ''));
});

const results = [];
const emit = (r) => {
  results.push(r);
  console.log('RESULT ' + JSON.stringify(r));
};

// ブラウザの対応状況(ビューワーの判定 src/lib/codec.ts と同じ文字列)
await page.goto(args.url, { waitUntil: 'domcontentloaded' });
const env = await page.evaluate(() => {
  const v = document.createElement('video');
  const codecs = ['hvc1.1.6.L93.B0', 'hev1.1.6.L93.B0', 'hvc1.1.6.L93.90'];
  const mse = window.ManagedMediaSource ?? window.MediaSource;
  return {
    userAgent: navigator.userAgent,
    canPlayHls: v.canPlayType('application/vnd.apple.mpegurl'),
    mse: mse ? Object.fromEntries(codecs.map((c) => [c, mse.isTypeSupported(`video/mp4; codecs="${c}"`)])) : null,
    canPlayType: Object.fromEntries(codecs.map((c) => [c, v.canPlayType(`video/mp4; codecs="${c}"`)])),
  };
});
console.log('ENV ' + JSON.stringify(env));

const slider = page.getByRole('slider', { name: '再生位置' });
await slider.waitFor({ timeout: 30_000 });
// 1 時間表示にする(クリックで狙った時刻に飛べるように)
await page.locator('label', { hasText: '1時間' }).click();
await page.waitForTimeout(800);

const videoState = () =>
  page.evaluate(() => {
    const v = document.querySelector('video');
    const chip = document.querySelector('[title="再生元"]');
    const overlay = [...document.querySelectorAll('div')].find((d) => /再生できません|取得できませんでした|ライブ映像がありません/.test(d.textContent ?? '') && d.children.length < 6);
    return {
      currentTime: v.currentTime,
      videoWidth: v.videoWidth,
      videoHeight: v.videoHeight,
      paused: v.paused,
      readyState: v.readyState,
      error: v.error ? { code: v.error.code, message: v.error.message } : null,
      src: v.currentSrc || v.src,
      chip: chip?.textContent?.trim() ?? null,
      overlay: overlay?.textContent?.trim().slice(0, 200) ?? null,
      quality: v.getVideoPlaybackQuality?.() ? { total: v.getVideoPlaybackQuality().totalVideoFrames, dropped: v.getVideoPlaybackQuality().droppedVideoFrames } : null,
    };
  });

// 再生元が切り替わり、currentTime が進むのを待つ
async function checkPlaying(mode) {
  const want = SOURCE_LABEL[mode];
  const deadline = Date.now() + 45_000;
  let s = await videoState();
  while (Date.now() < deadline && !(s.chip === want && s.readyState >= 2 && s.videoWidth > 0)) {
    if (s.overlay && /再生できません|取得できませんでした/.test(s.overlay)) break;
    await page.waitForTimeout(500);
    s = await videoState();
  }
  const t0 = s.currentTime;
  await page.waitForTimeout(4000);
  const s1 = await videoState();
  const advanced = s1.currentTime - t0;
  const ok = s1.chip === want && s1.videoWidth > 0 && advanced > 1.5 && !s1.error && !s1.overlay;
  const shot = join(args.out, `${args.browser}-${mode}.png`);
  await page.screenshot({ path: shot });
  const engine = s1.src.startsWith('blob:') ? 'hls.js(MSE)' : s1.src ? 'native' : 'unknown';
  emit({
    mode,
    browser: args.browser,
    ok,
    engine,
    chip: s1.chip,
    advanced: Number(advanced.toFixed(2)),
    videoWidth: s1.videoWidth,
    videoHeight: s1.videoHeight,
    error: s1.error,
    overlay: s1.overlay,
    frames: s1.quality,
    screenshot: shot,
  });
  return ok;
}

// シークバーの、指定した絶対時刻の位置をクリックする
async function clickAt(at) {
  const [min, max] = await slider.evaluate((el) => [Number(el.getAttribute('aria-valuemin')), Number(el.getAttribute('aria-valuemax'))]);
  if (at < min || at > max) throw new Error(`時刻 ${new Date(at).toISOString()} がシークバーの範囲外`);
  const box = await slider.boundingBox();
  await page.mouse.click(box.x + ((at - min) / (max - min)) * box.width, box.y + box.height / 2);
}

const cams = await page.evaluate(async () => (await fetch('/api/cameras')).json());
const cam = cams.cameras.find((c) => c.camera_id === args.camera);
const processed = cam?.video_processed_until ? Date.parse(cam.video_processed_until) : null;

for (const mode of args.modes.split(',').filter(Boolean)) {
  playlists.length = 0;
  try {
    if (mode === 'live') {
      const live = page.getByRole('button', { name: 'ライブ' });
      if (await live.isEnabled().catch(() => false)) await live.click().catch(() => undefined);
    } else if (mode === 'kvs') {
      // timeline の kvs の範囲(処理済み位置より後)のうち、ライブ扱い(現在から 15 秒以内)より前
      const now = Date.now();
      let at = Number(args['kvs-at']);
      if (!Number.isFinite(at)) {
        const from = new Date(now - 30 * 60_000).toISOString();
        const tl = await page.evaluate(async (u) => (await fetch(u)).json(), `/api/cameras/${args.camera}/timeline?from=${from}&to=${new Date(now).toISOString()}`);
        const kvs = (tl.recorded ?? []).filter((r) => r.source === 'kvs').map((r) => [Date.parse(r.start), Date.parse(r.end)]);
        const last = kvs[kvs.length - 1];
        if (!last) throw new Error(`timeline に kvs の範囲がない(処理済み位置 ${tl.video_processed_until})`);
        at = Math.max(last[0] + 3_000, now - 90_000);
      }
      if (at > now - 20_000) throw new Error(`KVS 録画の時間帯が短すぎる(${new Date(at).toISOString()})`);
      await clickAt(at);
    } else if (mode === 's3') {
      const at = Number(args['s3-at'] ?? (processed ? processed - 40_000 : NaN));
      if (!Number.isFinite(at)) throw new Error('S3 録画の時刻が分からない(--s3-at)');
      await clickAt(at);
    }
    const ok = await checkPlaying(mode);
    results[results.length - 1].playlists = [...new Set(playlists)].slice(0, 4);
    if (!ok) console.log(`  ${mode}: 再生を確認できない`);
  } catch (e) {
    const shot = join(args.out, `${args.browser}-${mode}-error.png`);
    await page.screenshot({ path: shot }).catch(() => undefined);
    emit({ mode, browser: args.browser, ok: false, error: String(e), screenshot: shot });
  }
}

if (consoleErrors.length) console.log('CONSOLE ' + JSON.stringify(consoleErrors.slice(0, 20)));
await browser.close();
process.exit(results.every((r) => r.ok) ? 0 : 1);

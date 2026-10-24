// ビューワーを実ブラウザで開き、ライブ・KVS 録画・S3 録画の再生を確かめる(scripts/e2e/run.sh から呼ぶ)。
//
//   node scripts/e2e/browser.mjs --url https://dxxx.cloudfront.net --out <スクショの出力先> \
//     [--browser chrome|webkit|chromium] [--modes live,kvs,s3,webrtc-h265,webrtc-h264,hls-fallback,ptz] [--s3-at <epoch ms>] [--camera cam-001] [--headed]
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
let quiet = false; // true の間は結果を出さない(前提の確認に checkPlaying を使うとき)
const emit = (r) => {
  if (quiet) return;
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
    const chip = document.querySelector('[data-source-chip]');
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
  while (Date.now() < deadline && !(s.chip?.startsWith(want) && s.readyState >= 2 && s.videoWidth > 0)) {
    if (s.overlay && /再生できません|取得できませんでした/.test(s.overlay)) break;
    await page.waitForTimeout(500);
    s = await videoState();
  }
  const t0 = s.currentTime;
  await page.waitForTimeout(4000);
  const s1 = await videoState();
  const advanced = s1.currentTime - t0;
  const ok = !!s1.chip?.startsWith(want) && s1.videoWidth > 0 && advanced > 1.5 && !s1.error && !s1.overlay;
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

// 焼き込みの 4 行目(PTZ pan ... zoom ...)の白い文字の形。fontsize = 高さ / 24、行間 6、y = 16 から(emulator/streams.py)
const ptzLineMask = () =>
  page.evaluate(() => {
    const v = document.querySelector('video');
    if (!v || !v.videoWidth) return null;
    const scale = v.videoHeight / 720;
    const fs = Math.max(16, Math.floor(v.videoHeight / 24));
    const y0 = Math.round(16 + 3 * (fs + 6 * scale) - 4 * scale);
    const h = Math.round(fs + 10 * scale);
    const w = Math.round(v.videoWidth * 0.7);
    const c = (window.__ptzCanvas ??= document.createElement('canvas'));
    c.width = w;
    c.height = h;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(v, 16, y0, w, h, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h).data;
    // 白い文字だけを 1 にする(背景は黒 60% の箱越しなので暗い)
    const bits = new Uint8Array(w * h);
    let on = 0;
    for (let i = 0; i < w * h; i++) {
      const lum = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2];
      if (lum > 180) { bits[i] = 1; on++; }
    }
    return { bits: Array.from(bits), on, t: v.currentTime };
  });
const maskDiff = (a, b) => {
  let n = 0;
  for (let i = 0; i < a.bits.length; i++) if (a.bits[i] !== b.bits[i]) n++;
  return n / Math.max(1, Math.max(a.on, b.on));
};

// PTZ: absolute で zoom 1 → GET .../ptz に反映 → ライブ映像の焼き込み 4 行目が変わるまでの時間
async function checkPtz() {
  const cam = args.camera;
  const ptzUrl = `/api/cameras/${cam}/ptz`;
  const before = await page.evaluate(async (u) => (await fetch(u)).json(), ptzUrl);
  if (!before.supported) throw new Error(`PTZ 非対応: ${JSON.stringify(before)}`);
  // ライブで再生中にする
  const live = page.getByRole('button', { name: 'ライブ' });
  if (await live.isEnabled().catch(() => false)) await live.click().catch(() => undefined);
  quiet = true;
  const playing = await checkPlaying('live').finally(() => { quiet = false; });
  if (!playing) throw new Error('ライブが再生されていない');
  // 基準の形(2 秒ぶん。ノイズの大きさも見る)
  const base = await ptzLineMask();
  if (!base || base.on < 50) throw new Error(`4 行目の文字が見つからない(on=${base?.on})`);
  let noise = 0;
  for (let i = 0; i < 10; i++) {
    await page.waitForTimeout(200);
    const m = await ptzLineMask();
    noise = Math.max(noise, maskDiff(base, m));
  }
  const target = before.zoom >= 0.5 ? 0 : 1;
  await page.screenshot({ path: join(args.out, `${args.browser}-ptz-before.png`) });
  const t0 = Date.now();
  const post = await page.evaluate(async ([u, z]) => {
    const r = await fetch(u, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ op: 'absolute', pan: 0, tilt: 0, zoom: z }) });
    return { status: r.status, body: await r.json().catch(() => null) };
  }, [ptzUrl, target]);
  const postMs = Date.now() - t0;
  if (post.status !== 202 && post.status !== 200) throw new Error(`POST ${ptzUrl} → ${post.status} ${JSON.stringify(post.body)}`);
  let apiMs = null;
  let videoMs = null;
  let after = null;
  const threshold = Math.max(0.15, noise * 3);
  while (Date.now() - t0 < 30_000 && (apiMs === null || videoMs === null)) {
    if (videoMs === null) {
      const m = await ptzLineMask();
      if (m && maskDiff(base, m) > threshold) videoMs = Date.now() - t0;
    }
    if (apiMs === null && (Date.now() - t0) % 500 < 120) {
      after = await page.evaluate(async (u) => (await fetch(u)).json(), ptzUrl);
      if (Math.abs((after.zoom ?? -1) - target) < 0.01) apiMs = Date.now() - t0;
    }
    await page.waitForTimeout(100);
  }
  await page.waitForTimeout(1500);
  const shot = join(args.out, `${args.browser}-ptz-after.png`);
  await page.screenshot({ path: shot });
  emit({
    mode: 'ptz',
    browser: args.browser,
    ok: apiMs !== null && videoMs !== null,
    request_id: post.body?.request_id,
    zoom: `${before.zoom} → ${target}`,
    post_ms: postMs,
    api_ms: apiMs,
    video_ms: videoMs,
    noise: Number(noise.toFixed(3)),
    // ライブの方式(PTZ は WebRTC のライブで測る。HLS なら数秒遅れる)
    live: await page.evaluate(() => globalThis.__ccLive ?? null),
    position: after ? { pan: after.pan, tilt: after.tilt, zoom: after.zoom, move_status: after.move_status } : null,
    screenshot: shot,
  });
  // 元に戻す(後の検証と録画に影響しないように)
  await page.evaluate(async (u) => fetch(u, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ op: 'absolute', pan: 0, tilt: 0, zoom: 0 }) }), ptzUrl);
}

// ライブの方式(KVS WebRTC と HLS への切り替え)。モードごとに新しいコンテキストで開く
//   webrtc-h265: そのまま開く → WebRTC で H.265(main)
//   webrtc-h264: RTCRtpReceiver.getCapabilities から H.265 を消す(H.265 を受けられないブラウザ)→ WebRTC で H.264(sub)
//   hls-fallback: GET .../webrtc を 409 にする(エッジの WebRTC が止まっている)→ HLS
const RTC_EXPECT = {
  'webrtc-h265': { transport: 'webrtc', codec: 'H.265' },
  'webrtc-h264': { transport: 'webrtc', codec: 'H.264' },
  'hls-fallback': { transport: 'hls', codec: null },
};
async function checkLiveTransport(mode) {
  const want = RTC_EXPECT[mode];
  const c = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ja-JP', colorScheme: 'light' });
  try {
    if (mode === 'webrtc-h264') {
      await c.addInitScript(() => {
        const orig = RTCRtpReceiver.getCapabilities.bind(RTCRtpReceiver);
        RTCRtpReceiver.getCapabilities = (kind) => {
          const r = orig(kind);
          if (r && kind === 'video') r.codecs = r.codecs.filter((x) => !/h265|hevc/i.test(x.mimeType));
          return r;
        };
      });
    }
    if (mode === 'hls-fallback') {
      // page.route は MSW(Service Worker)の応答を横取りできないので、fetch を包む(実環境でもモックでも同じに動く)
      await c.addInitScript(() => {
        const orig = globalThis.fetch.bind(globalThis);
        globalThis.fetch = (input, init) => {
          const u = typeof input === 'string' ? input : input.url;
          if (/\/api\/cameras\/[^/]+\/webrtc/.test(u)) {
            return Promise.resolve(new Response(JSON.stringify({ error: { code: 'WEBRTC_UNAVAILABLE', message: 'E2E: WebRTC を止めた' } }),
              { status: 409, headers: { 'content-type': 'application/json' } }));
          }
          return orig(input, init);
        };
      });
    }
    const p = await c.newPage();
    const t0 = Date.now();
    await p.goto(args.url, { waitUntil: 'domcontentloaded' });
    const state = () => p.evaluate(() => {
      const v = document.querySelector('video');
      return {
        live: globalThis.__ccLive ?? null,
        chip: document.querySelector('[data-source-chip]')?.textContent?.trim() ?? null,
        t: v?.currentTime ?? 0, w: v?.videoWidth ?? 0, h: v?.videoHeight ?? 0, rs: v?.readyState ?? 0,
      };
    });
    let st = await state();
    const deadline = Date.now() + 45_000;
    while (Date.now() < deadline && !(st.live && !st.live.connecting && st.rs >= 2 && st.w > 0)) {
      await p.waitForTimeout(250);
      st = await state();
    }
    const playMs = Date.now() - t0;
    await p.waitForTimeout(3000); // getStats でコーデックを確かめる時間
    const t1 = (await state()).t;
    await p.waitForTimeout(4000);
    const st2 = await state();
    const stats = await p.evaluate(async () => (globalThis.__ccLiveStats ? globalThis.__ccLiveStats() : null)).catch(() => null);
    const advanced = st2.t - t1;
    const live = st2.live;
    const ok = !!live && live.transport === want.transport && (want.codec === null || live.codec === want.codec)
      && st2.w > 0 && advanced > 1.5;
    const shot = join(args.out, `${args.browser}-${mode}.png`);
    await p.screenshot({ path: shot });
    emit({
      mode, browser: args.browser, ok, transport: live?.transport, codec: live?.codec, chip: st2.chip,
      first_frame_ms: live?.firstFrameMs ?? null, play_ms: playMs, fallback: live?.fallback ?? null, relay: live?.relay ?? null,
      advanced: Number(advanced.toFixed(2)), videoWidth: st2.w, videoHeight: st2.h, stats, screenshot: shot,
    });
  } finally {
    await c.close();
  }
}

for (const mode of args.modes.split(',').filter(Boolean)) {
  playlists.length = 0;
  try {
    if (mode in RTC_EXPECT) {
      await checkLiveTransport(mode);
      continue;
    }
    if (mode === 'ptz') {
      await checkPtz();
      continue;
    }
    if (mode === 'live') {
      const live = page.getByRole('button', { name: 'ライブ' });
      if (await live.isEnabled().catch(() => false)) await live.click().catch(() => undefined);
    } else if (mode === 'kvs') {
      // timeline の kvs の範囲(処理済み位置より後)のうち、ライブ扱い(現在から 15 秒以内)より前
      // 退避が追いついている(処理済み位置が 1 分ごとに進む)と、KVS だけの範囲は 0〜60 秒程度しかない。
      // 範囲が 23 秒以上になるまで最大 90 秒待つ
      let at = Number(args['kvs-at']);
      let now = Date.now();
      const deadline = now + 90_000;
      while (!Number.isFinite(Number(args['kvs-at']))) {
        now = Date.now();
        const from = new Date(now - 30 * 60_000).toISOString();
        const tl = await page.evaluate(async (u) => (await fetch(u)).json(), `/api/cameras/${args.camera}/timeline?from=${from}&to=${new Date(now).toISOString()}`);
        const kvs = (tl.recorded ?? []).filter((r) => r.source === 'kvs').map((r) => [Date.parse(r.start), Date.parse(r.end)]);
        const last = kvs[kvs.length - 1];
        at = last ? Math.max(last[0] + 3_000, now - 90_000) : NaN;
        if (Number.isFinite(at) && at <= now - 20_000) break;
        if (Date.now() > deadline) throw new Error(`KVS 録画の時間帯が短い(処理済み位置 ${tl.video_processed_until}、kvs の最後 ${last ? new Date(last[0]).toISOString() : 'なし'})`);
        await page.waitForTimeout(3_000);
      }
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

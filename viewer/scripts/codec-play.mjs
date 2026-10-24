/* global document, window, MediaSource */
// コーデックの切り替えを跨いだ録画(vod.m3u8)が、本物の Chrome(hls.js)で再生できるかを確かめる(docs/codec.md)。
// 使い方(viewer/ で):
//   node scripts/codec-play.mjs <CloudFront の URL> <camera_id> <from ISO> <to ISO> [出力ディレクトリ]
// デプロイ済みのビューワーのページを開き(同一オリジンで /api を呼ぶため)、hls.js で vod.m3u8 を最後まで再生する。
// hls.js のイベント(EXT-X-MAP の切り替えで出る BUFFER_CODECS、エラー)と、再生した時間、途中のスクリーンショットを残す。
// Google Chrome(channel: 'chrome')を使う。Playwright 同梱の Chromium は H.265 を再生できないため。

import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { chromium } from 'playwright';

const [base, cameraId, from, to, out = 'codec-play'] = process.argv.slice(2);
if (!base || !cameraId || !from || !to) {
  console.error('使い方: node scripts/codec-play.mjs <URL> <camera_id> <from> <to> [出力ディレクトリ]');
  process.exit(2);
}
mkdirSync(out, { recursive: true });
const require = createRequire(import.meta.url);
const hlsPath = require.resolve('hls.js/dist/hls.min.js');

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 960, height: 600 } });
const consoleLines = [];
page.on('console', (m) => consoleLines.push(`${m.type()}: ${m.text()}`));
await page.goto(base, { waitUntil: 'domcontentloaded' });
await page.addScriptTag({ path: hlsPath });

const src = `/api/cameras/${encodeURIComponent(cameraId)}/vod.m3u8?from=${from}&to=${to}`;
const result = await page.evaluate(async (src) => {
  document.body.innerHTML = '<video id="v" muted playsinline style="width:960px;height:540px;background:#000"></video>';
  const video = document.getElementById('v');
  const events = [];
  const support = {
    hevc: MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.B0"'),
    avc: MediaSource.isTypeSupported('video/mp4; codecs="avc1.42c01f"'),
  };
  const hls = new window.Hls({ enableWorker: true });
  hls.on(window.Hls.Events.BUFFER_CODECS, (_e, d) => events.push({ t: video.currentTime, ev: 'BUFFER_CODECS', codec: d.video?.codec }));
  hls.on(window.Hls.Events.FRAG_CHANGED, (_e, d) => {
    if (d.frag.initSegment) events.push({ t: video.currentTime, ev: 'FRAG_CHANGED', sn: d.frag.sn, init: d.frag.initSegment.relurl });
  });
  hls.on(window.Hls.Events.ERROR, (_e, d) => events.push({ t: video.currentTime, ev: 'ERROR', fatal: d.fatal, details: d.details }));
  hls.loadSource(src);
  hls.attachMedia(video);
  await new Promise((r) => hls.on(window.Hls.Events.MANIFEST_PARSED, r));
  const total = hls.levels[0]?.details?.totalduration ?? null;
  video.playbackRate = 4;
  await video.play();
  const t0 = performance.now();
  await new Promise((resolve) => {
    const tick = () => {
      const fatal = events.some((e) => e.ev === 'ERROR' && e.fatal);
      if (video.ended || fatal || performance.now() - t0 > 120_000) return resolve();
      setTimeout(tick, 250);
    };
    tick();
  });
  // FRAG_CHANGED は init の URL が変わったときだけ残す
  const inits = [];
  for (const e of events.filter((x) => x.ev === 'FRAG_CHANGED')) {
    if (!inits.length || inits[inits.length - 1].init !== e.init) inits.push(e);
  }
  return {
    support,
    total,
    played: video.currentTime,
    ended: video.ended,
    decodedFrames: video.getVideoPlaybackQuality?.().totalVideoFrames ?? null,
    droppedFrames: video.getVideoPlaybackQuality?.().droppedVideoFrames ?? null,
    codecs: events.filter((e) => e.ev === 'BUFFER_CODECS'),
    initSwitches: inits,
    errors: events.filter((e) => e.ev === 'ERROR'),
  };
}, src);
console.log(JSON.stringify(result, null, 1));

// 切り替えの前後の絵(init が変わった時刻の前後 2 秒)
for (const [i, sw] of result.initSwitches.entries()) {
  for (const dt of [-2, 2]) {
    const t = Math.max(0, sw.t + dt);
    await page.evaluate(async (t) => {
      const v = document.getElementById('v');
      v.pause();
      v.currentTime = t;
      await new Promise((r) => v.addEventListener('seeked', r, { once: true }));
    }, t);
    await page.waitForTimeout(300);
    await page.screenshot({ path: join(out, `switch-${i}-${dt > 0 ? 'after' : 'before'}.png`) });
  }
}
if (consoleLines.length) console.error(consoleLines.slice(-20).join('\n'));
await browser.close();

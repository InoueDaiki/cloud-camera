// エミュレーターの操作画面を Chromium で開き、偽のカメラ(--use-fake-device-for-media-stream)を WHIP で送り込む。
//   1. 「開始」で送信中になる(WebRTC が Docker のポート越しにつながる)
//   2. カメラの live: が ready になり、FFmpeg が代替映像からライブの入力に切り替わってフレームが進む
//   3. 「停止」で NO SIGNAL(代替映像)に戻る
// スナップショットと画面を results/ に保存する。終わったらカメラの映像ソースを元に戻す。
// 使い方: npm run check [-- --url http://localhost:8090] [-- --camera 1] [-- --headed] [-- --keep]
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const ROOT = fileURLToPath(new URL('.', import.meta.url));
const args = process.argv.slice(2);
const opt = (name, def) => (args.includes(name) ? args[args.indexOf(name) + 1] : def);
const BASE = opt('--url', 'http://localhost:8090');
const INDEX = Number(opt('--camera', '1'));
const headed = args.includes('--headed');
const keep = args.includes('--keep');
const OUT = join(ROOT, 'results');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const camState = async () => (await (await fetch(`${BASE}/api/state`)).json()).cameras.find((c) => c.index === INDEX);
async function waitFor(what, pred, timeoutMs) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    last = await camState();
    if (pred(last)) return last;
    await sleep(500);
  }
  throw new Error(`${what} になりません: ${JSON.stringify(last)}`);
}
async function saveSnapshot(name) {
  const r = await fetch(`${BASE}/api/cameras/${INDEX}/snapshot.jpg?t=${Date.now()}`);
  await writeFile(join(OUT, name), Buffer.from(await r.arrayBuffer()));
}
async function setSource(source) {
  await fetch(`${BASE}/api/cameras/${INDEX}/source`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source }),
  });
}

await mkdir(OUT, { recursive: true });
const before = await camState();
const live = `live:${before.path}`;
const browser = await chromium.launch({
  headless: !headed,
  args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const steps = [];
const t0 = Date.now();
const step = (name, extra = {}) => { steps.push({ step: name, s: (Date.now() - t0) / 1000, ...extra }); console.log(`${((Date.now() - t0) / 1000).toFixed(1)}s  ${name}`, extra); };
let ok = false;
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('console', (m) => { if (m.type() === 'error') console.log('[console]', m.text()); });
  await page.goto(BASE);
  await page.selectOption('#target', String(INDEX));
  await page.click('#start');
  await page.waitForFunction(() => document.querySelector('#pubstate').textContent === '送信中', null, { timeout: 30000 });
  step('WebRTC 接続(送信中)');

  let c = await waitFor('live の受信', (s) => s.source === live && s.live?.ready && s.active_source === live, 30000);
  step('FFmpeg がライブの入力に切り替え', { restarts: c.ffmpeg_restarts });
  c = await waitFor('フレームの進行', (s) => s.active_source === live && Number(s.frame) > 0, 30000);
  const f0 = Number(c.frame);
  await sleep(6000);
  c = await camState();
  if (!(Number(c.frame) > f0) || c.active_source !== live) throw new Error(`ライブの入力でフレームが進みません: ${JSON.stringify(c)}`);
  step('フレームが進む', { fps: c.fps, frame: c.frame });
  await saveSnapshot('live.jpg');
  await page.screenshot({ path: join(OUT, 'ui-live.png'), fullPage: true });

  if (!keep) {
    await page.click('#stop');
    c = await waitFor('代替映像', (s) => s.live && !s.live.ready && s.active_source !== live && s.ffmpeg_running, 30000);
    step('停止 → 代替映像', { active_source: c.active_source });
    await sleep(3000);
    await saveSnapshot('fallback.jpg');
    await page.screenshot({ path: join(OUT, 'ui-stopped.png'), fullPage: true });
  }
  ok = true;
} catch (e) {
  step('失敗', { error: e.message });
} finally {
  await browser.close();
  if (!keep) await setSource(before.source);
  await writeFile(join(OUT, 'result.json'), JSON.stringify({ ok, base: BASE, camera: INDEX, steps }, null, 2));
}
console.log(ok ? 'OK' : 'NG');
process.exit(ok ? 0 : 1);

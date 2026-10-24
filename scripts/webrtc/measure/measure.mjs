// KVS WebRTC の確認ビューワーを Playwright で開いて、遅延のスクショと統計を取る。
//
//   cd scripts/webrtc/measure && mise exec -- npm install
//   mise exec -- node measure.mjs --browser chrome --codec h264 [--relay] [--headed] [--shots 5] [--out ../../../docs/webrtc-shots]
//
// 遅延はスクショの「映像に焼き込まれた UTC(エミュレーターがフレームを作った時刻)」と
// 「その下のブラウザの時計」の差で読む(エンコード、RTSP、エッジ、ネットワーク、ジッタバッファ、デコード、描画をすべて含む)。
// 同時に getStats の値(候補の種類、コーデック、受信ビットレートなど)を JSON に残す。
import { chromium, webkit } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';

const { values: a } = parseArgs({
  options: {
    browser: { type: 'string', default: 'chrome' },
    codec: { type: 'string', default: 'h264' },
    camera: { type: 'string', default: 'cam-001' },
    relay: { type: 'boolean', default: false },
    headed: { type: 'boolean', default: false },
    // Chrome の host 候補を mDNS(.local)で隠さず、実 IP で出す。Docker Desktop のコンテナ(エッジ)から
    // Mac の LAN の IP には届くので、同じ LAN の視聴者として P2P を試せる
    'expose-host': { type: 'boolean', default: false },
    shots: { type: 'string', default: '5' },
    interval: { type: 'string', default: '2000' },
    warmup: { type: 'string', default: '8000' },
    url: { type: 'string', default: 'http://127.0.0.1:8765/' },
    out: { type: 'string', default: 'out' },
    label: { type: 'string', default: '' },
    // 計測のあと、この秒数だけ視聴を続ける(エッジの負荷と帯域を docker_stats.py で測る間)
    hold: { type: 'string', default: '0' },
  },
});

const label = a.label || `${a.browser}-${a.codec}-${a.relay ? 'relay' : 'all'}`;
await mkdir(a.out, { recursive: true });

const browser = a.browser === 'webkit'
  ? await webkit.launch({ headless: !a.headed })
  : await chromium.launch({
    channel: 'chrome',
    headless: !a.headed,
    args: a['expose-host'] ? ['--disable-features=WebRtcHideLocalIpsWithMdns'] : [],
  });
const page = await browser.newPage({ viewport: { width: 1100, height: 640 } });
page.on('console', (m) => console.log(`[page] ${m.text()}`));
const q = new URLSearchParams({ camera: a.camera, codec: a.codec, autostart: '1', ...(a.relay ? { relay: '1' } : {}) });
await page.goto(`${a.url}?${q}`);

const result = { label, browser: a.browser, version: browser.version(), codec_pref: a.codec, relay: a.relay, shots: [], samples: [] };
try {
  await page.waitForFunction(() => {
    const s = window.ccStats;
    // 'failed' はトリクル ICE の途中で一瞬出ることがあるので待ち続ける
    return s.state === 'unsupported' || s.state === 'error' || (s.frames_decoded || 0) > 15;
  }, null, { timeout: 45000 });
} catch (e) {
  result.error = `映像が来ない: ${e.message.split('\n')[0]}`;
}
let st = await page.evaluate(() => window.ccStats);
if (!result.error && (st.frames_decoded || 0) > 15) {
  await page.waitForTimeout(Number(a.warmup));
  for (let i = 0; i < Number(a.shots); i++) {
    const path = `${a.out}/${label}-${i + 1}.png`;
    await page.locator('.stage').screenshot({ path });
    st = await page.evaluate(() => window.ccStats);
    result.shots.push(path);
    result.samples.push({
      kbps: st.kbps, fps: st.fps, jitter_buffer_ms: st.jitter_buffer_ms, processing_ms: st.processing_ms, decode_ms: st.decode_ms, rtt_ms: st.rtt_ms,
    });
    await page.waitForTimeout(Number(a.interval));
  }
}
if (Number(a.hold) > 0) await page.waitForTimeout(Number(a.hold) * 1000);
st = await page.evaluate(() => window.ccStats);
const { log, ...rest } = st;
result.final = rest;
result.log = log;
await writeFile(`${a.out}/${label}.json`, JSON.stringify(result, null, 2));
console.log(JSON.stringify({ label, version: result.version, error: result.error, ...rest, samples: result.samples }, null, 2));
await browser.close();

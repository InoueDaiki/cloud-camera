// 確認ページ(index.html)を Playwright の各ブラウザで開き、結果を results/<名前>.json に保存して要約を出す。
// 使い方: npm run check [-- --headed] [-- --only chrome,webkit]
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, webkit, firefox } from 'playwright';

const ROOT = fileURLToPath(new URL('.', import.meta.url));
const args = process.argv.slice(2);
const headed = args.includes('--headed');
const onlyIdx = args.indexOf('--only');
const only = onlyIdx >= 0 ? args[onlyIdx + 1].split(',') : null;

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mp4': 'video/mp4', '.json': 'application/json' };
// Range 要求に対応した最小の静的サーバー(WebKit の <video src> は Range を使う)
const server = createServer(async (req, res) => {
  const path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^\/+/, '') || 'index.html';
  if (path.startsWith('..')) { res.writeHead(403).end(); return; }
  try {
    const body = await readFile(join(ROOT, path));
    const type = TYPES[extname(path)] || 'application/octet-stream';
    const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || '');
    if (m) {
      const start = m[1] ? +m[1] : 0; const end = m[2] ? Math.min(+m[2], body.length - 1) : body.length - 1;
      res.writeHead(206, { 'Content-Type': type, 'Content-Range': `bytes ${start}-${end}/${body.length}`, 'Accept-Ranges': 'bytes', 'Content-Length': end - start + 1 });
      res.end(body.subarray(start, end + 1));
    } else {
      res.writeHead(200, { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Content-Length': body.length });
      res.end(body);
    }
  } catch { res.writeHead(404).end(); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/index.html`;

const TARGETS = [
  { name: 'chromium', type: chromium, opts: {} },
  { name: 'chrome', type: chromium, opts: { channel: 'chrome' } },
  { name: 'webkit', type: webkit, opts: {} },
  { name: 'firefox', type: firefox, opts: {} },
];

await mkdir(join(ROOT, 'results'), { recursive: true });
const rows = [];
for (const t of TARGETS) {
  if (only && !only.includes(t.name)) continue;
  const name = `${t.name}${headed ? '-headed' : ''}`;
  let browser;
  try {
    browser = await t.type.launch({ headless: !headed, ...t.opts });
    const page = await browser.newPage();
    await page.goto(url);
    await page.waitForFunction(() => window.__done === true, null, { timeout: 90_000 });
    const res = await page.evaluate(() => window.__result);
    res.browser = { name, version: browser.version() };
    await writeFile(join(ROOT, 'results', `${name}.json`), JSON.stringify(res, null, 2) + '\n');
    await page.screenshot({ path: join(ROOT, 'results', `${name}.png`), fullPage: true });
    const p = (id, via) => res.play?.find((x) => x.id === id && x.via === via)?.ok;
    const yn = (v) => (v === true ? 'o' : v === false ? 'x' : '-');
    rows.push({
      browser: `${name} ${browser.version()}`,
      'MSE hevc': yn(p('hevc-main', 'MSE')), 'MSE hevc10': yn(p('hevc-main10', 'MSE')), 'src= hevc': yn(p('hevc-main', 'src=')), 'MSE h264': yn(p('h264-main', 'MSE')),
      'RTC recv H265': yn(res.rtc?.receiverH265?.length > 0), 'RTC send H265': yn(res.rtc?.senderH265?.length > 0), 'RTC loopback': yn(res.rtc?.loopback?.ok), 'WebCodecs hevc': yn(res.webCodecs?.find((w) => w.codec.startsWith('hvc1.1') && w.hardwareAcceleration === 'no-preference')?.supported),
    });
  } catch (err) {
    rows.push({ browser: name, error: String(err.message || err).split('\n')[0] });
  } finally {
    await browser?.close();
  }
}
server.close();
console.table(rows);

// モックモードの画面を Playwright(ヘッドレス)で撮る。
// 使い方: npm run dev:mock を起動しておき、
//   node scripts/screenshots.mjs <出力ディレクトリ> [URL]
// ライト/ダーク × デスクトップ/スマホ幅で、ライブ、録画の再生(ホバーでサムネイル)、検索結果を撮る。

import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { chromium } from 'playwright';

const out = process.argv[2] ?? 'screenshots';
const url = process.argv[3] ?? 'http://localhost:5173/';
mkdirSync(out, { recursive: true });

const viewports = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
};

const browser = await chromium.launch();
const logs = [];
for (const scheme of ['light', 'dark']) {
  for (const [vpName, viewport] of Object.entries(viewports)) {
    const ctx = await browser.newContext({
      viewport,
      colorScheme: scheme,
      deviceScaleFactor: 2,
      locale: 'ja-JP',
      hasTouch: vpName === 'mobile',
      isMobile: vpName === 'mobile',
    });
    const page = await ctx.newPage();
    page.on('console', (m) => {
      if (m.type() === 'error' || m.type() === 'warning') logs.push(`[${scheme}/${vpName}] ${m.type()}: ${m.text()}`);
    });
    page.on('pageerror', (e) => logs.push(`[${scheme}/${vpName}] pageerror: ${e.message}`));
    const shot = async (name) => {
      const file = join(out, `${vpName}-${scheme}-${name}.png`);
      await page.screenshot({ path: file, fullPage: vpName === 'mobile' });
      console.log(file);
    };

    await page.goto(url);
    await page.getByRole('slider', { name: '再生位置' }).waitFor();
    await page.waitForTimeout(3500);
    await shot('1-live');

    // PTZ: 右を押している間(continuous)と、プリセットの一覧
    const right = page.getByRole('button', { name: '右へ' });
    await right.scrollIntoViewIfNeeded();
    const rb = await right.boundingBox();
    await page.mouse.move(rb.x + rb.width / 2, rb.y + rb.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(1600);
    await shot('1b-ptz-moving');
    await page.mouse.up();
    await page.getByRole('button', { name: /^プリセット/ }).click();
    await page.waitForTimeout(500);
    await shot('1c-ptz-presets');
    await page.keyboard.press('Escape');
    await page.evaluate(() => globalThis.scrollTo(0, 0));

    // 録画の再生: シークバーの 40% の位置をクリック
    const track = page.getByRole('slider', { name: '再生位置' });
    const box = await track.boundingBox();
    await page.mouse.click(box.x + box.width * 0.4, box.y + box.height / 2);
    await page.waitForTimeout(4000);
    if (vpName === 'desktop') {
      // ホバーでサムネイル
      await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
      await page.waitForTimeout(1200);
    }
    await shot('2-playback');

    // ズーム 1 時間
    await page.locator('label', { hasText: '1時間' }).click();
    await page.waitForTimeout(800);
    if (vpName === 'desktop') {
      const b2 = await track.boundingBox();
      await page.mouse.move(b2.x + b2.width * 0.3, b2.y + b2.height / 2);
      await page.waitForTimeout(1200);
    }
    await shot('3-zoom');

    // 検索
    await page.getByRole('textbox', { name: '質問' }).fill('昨日の夕方に玄関に来た人は?');
    await page.getByRole('button', { name: '質問する' }).click();
    await page.getByText('該当', { exact: false }).first().waitFor({ timeout: 10000 });
    await page.getByRole('button', { name: '生成された SQL' }).click();
    await page.waitForTimeout(600);
    if (vpName === 'mobile') await page.getByText('該当', { exact: false }).first().scrollIntoViewIfNeeded();
    await shot('4-search');

    // 検索結果をクリックして再生ジャンプ
    await page.getByRole('button', { name: /\d{4}\/\d{2}\/\d{2}/ }).first().click();
    await page.waitForTimeout(4000);
    await page.evaluate(() => globalThis.scrollTo(0, 0));
    await shot('5-jump');

    // 未退避(KVS ON_DEMAND)の区間: 駐車場は処理済み位置が 15 分ほど遅れている
    if (vpName === 'desktop') {
      await page.getByText('駐車場').first().click();
      await page.waitForTimeout(2500);
      const b3 = await track.boundingBox();
      // 1 時間表示で、現在は右から 35% のところ。そのすぐ左(未退避の区間)
      await page.mouse.click(b3.x + b3.width * 0.75, b3.y + b3.height / 2);
      await page.waitForTimeout(4000);
      await shot('5b-kvs');
    }

    // オフラインのカメラ(スマホはメニューから)
    if (vpName === 'mobile') await page.getByRole('button', { name: 'カメラ一覧' }).click();
    await page.getByText('倉庫').first().click();
    await page.waitForTimeout(2500);
    await shot('6-offline');

    if (vpName === 'mobile') {
      await page.getByRole('button', { name: 'カメラ一覧' }).click();
      await page.waitForTimeout(500);
      await shot('7-camera-list');
    }
    await ctx.close();
  }
}
await browser.close();
if (logs.length) {
  console.log('\n--- console ---');
  for (const l of logs) console.log(l);
}

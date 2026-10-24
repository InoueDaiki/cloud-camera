// モックのスプライト画像を canvas で描く(1 コマ 160×90、横 10 × 縦 6)

import { SPRITE } from '../lib/sprite';
import { formatHms, SECOND } from '../lib/time';
import { SLOT, slotState } from './data';

const PALETTE: Record<string, [string, string]> = {
  'cam-001': ['#3b4a5a', '#7b8a7a'],
  'cam-002': ['#2f3b4b', '#5b6470'],
  'cam-003': ['#4a4036', '#6e665c'],
};

const cache = new Map<string, Promise<Blob>>();

export function spriteBlob(cameraId: string, minuteStart: number, now: number): Promise<Blob> {
  const key = `${cameraId}/${minuteStart}`;
  let p = cache.get(key);
  if (!p) {
    p = draw(cameraId, minuteStart, now);
    cache.set(key, p);
  }
  return p;
}

async function draw(cameraId: string, minuteStart: number, now: number): Promise<Blob> {
  const { frameWidth: w, frameHeight: h, cols, rows } = SPRITE;
  const canvas = new OffscreenCanvas(w * cols, h * rows);
  const ctx = canvas.getContext('2d')!;
  const [c1, c2] = PALETTE[cameraId] ?? ['#333', '#666'];

  for (let i = 0; i < cols * rows; i++) {
    const x = (i % cols) * w;
    const y = Math.floor(i / cols) * h;
    const t = minuteStart + i * SECOND;
    const rec = slotState(cameraId, Math.floor(t / SLOT) * SLOT, now);
    if (rec !== 's3') {
      // 録画がない秒は黒
      ctx.fillStyle = '#000';
      ctx.fillRect(x, y, w, h);
      continue;
    }
    const g = ctx.createLinearGradient(x, y, x, y + h);
    g.addColorStop(0, c1);
    g.addColorStop(1, c2);
    ctx.fillStyle = g;
    ctx.fillRect(x, y, w, h);
    // 背景の建物(ドアや区画線のつもり)
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.fillRect(x + 20, y + 22, 34, 58);
    ctx.fillRect(x + 100, y + 40, 50, 40);
    // 動く物体(人物や車のつもり)
    const pos = ((minuteStart / 60000) * 7 + i * 1.6) % (w - 24);
    ctx.fillStyle = 'rgba(255, 190, 90, 0.85)';
    ctx.fillRect(x + 6 + pos, y + 46, 14, 30);
    // 焼き込みの時刻
    ctx.fillStyle = 'rgba(0,0,0,0.55)';
    ctx.fillRect(x, y, 62, 14);
    ctx.fillStyle = '#fff';
    ctx.font = '10px monospace';
    ctx.fillText(formatHms(t), x + 4, y + 10);
  }
  return canvas.convertToBlob({ type: 'image/jpeg', quality: 0.7 });
}

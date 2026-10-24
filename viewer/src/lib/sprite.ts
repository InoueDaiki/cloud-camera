// スプライト(sprite.jpg)のコマ位置
// 1 分ごとに 1 枚。1 コマ 160×90、横 10 × 縦 6、左上から 1 秒ずつ時刻順(docs/contracts.md)。

import { floorTo, MINUTE, SECOND } from './time';

export const SPRITE = { frameWidth: 160, frameHeight: 90, cols: 10, rows: 6 } as const;

export interface SpriteFrame {
  url: string;
  /** 区間の始まりからの秒(0〜59) */
  second: number;
  col: number;
  row: number;
  /** 等倍での左上のピクセル位置 */
  x: number;
  y: number;
}

export function frameIndex(second: number): { col: number; row: number } {
  const s = Math.min(SPRITE.cols * SPRITE.rows - 1, Math.max(0, Math.floor(second)));
  return { col: s % SPRITE.cols, row: Math.floor(s / SPRITE.cols) };
}

/** 時刻 t のコマ。その分のスプライトがなければ null */
export function spriteFrameAt(
  minutes: { start: number; sprite: string }[],
  t: number,
): SpriteFrame | null {
  const minuteStart = floorTo(t, MINUTE);
  const m = minutes.find((x) => x.start === minuteStart);
  if (!m) return null;
  const second = Math.floor((t - minuteStart) / SECOND);
  const { col, row } = frameIndex(second);
  return {
    url: m.sprite,
    second,
    col,
    row,
    x: col * SPRITE.frameWidth,
    y: row * SPRITE.frameHeight,
  };
}

/** コマを width px の幅で切り出す CSS(背景画像の位置と大きさ) */
export function spriteStyle(frame: SpriteFrame, width: number = SPRITE.frameWidth) {
  const scale = width / SPRITE.frameWidth;
  return {
    width: `${width}px`,
    height: `${SPRITE.frameHeight * scale}px`,
    backgroundImage: `url("${frame.url}")`,
    backgroundRepeat: 'no-repeat',
    backgroundSize: `${SPRITE.frameWidth * SPRITE.cols * scale}px ${SPRITE.frameHeight * SPRITE.rows * scale}px`,
    backgroundPosition: `-${frame.x * scale}px -${frame.y * scale}px`,
  } as const;
}

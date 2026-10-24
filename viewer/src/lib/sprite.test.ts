import { describe, expect, it } from 'vitest';
import { frameIndex, spriteFrameAt, spriteStyle } from './sprite';

const m0 = Date.parse('2026-10-24T10:20:00Z');
const minutes = [{ start: m0, sprite: '/media/cam-001/2026/10/24/10/20/sprite.jpg' }];

describe('frameIndex', () => {
  it('左上から横 10 × 縦 6 の順', () => {
    expect(frameIndex(0)).toEqual({ col: 0, row: 0 });
    expect(frameIndex(9)).toEqual({ col: 9, row: 0 });
    expect(frameIndex(10)).toEqual({ col: 0, row: 1 });
    expect(frameIndex(37)).toEqual({ col: 7, row: 3 });
    expect(frameIndex(59)).toEqual({ col: 9, row: 5 });
  });
  it('範囲外は端に丸める', () => {
    expect(frameIndex(75)).toEqual({ col: 9, row: 5 });
    expect(frameIndex(-1)).toEqual({ col: 0, row: 0 });
  });
});

describe('spriteFrameAt', () => {
  it('その秒のコマの位置', () => {
    const f = spriteFrameAt(minutes, m0 + 37_900);
    expect(f).toMatchObject({ second: 37, col: 7, row: 3, x: 1120, y: 270 });
    expect(f?.url).toBe(minutes[0].sprite);
  });
  it('スプライトがない分は null', () => {
    expect(spriteFrameAt(minutes, m0 + 60_000)).toBeNull();
    expect(spriteFrameAt(minutes, m0 - 1)).toBeNull();
  });
});

describe('spriteStyle', () => {
  it('等倍', () => {
    const s = spriteStyle(spriteFrameAt(minutes, m0 + 12_000)!);
    expect(s.backgroundPosition).toBe('-320px -90px');
    expect(s.backgroundSize).toBe('1600px 540px');
  });
  it('縮小すると位置も同じ比率で縮む', () => {
    const s = spriteStyle(spriteFrameAt(minutes, m0 + 12_000)!, 80);
    expect(s.width).toBe('80px');
    expect(s.height).toBe('45px');
    expect(s.backgroundPosition).toBe('-160px -45px');
    expect(s.backgroundSize).toBe('800px 270px');
  });
});

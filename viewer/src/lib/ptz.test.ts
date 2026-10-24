import { describe, expect, it } from 'vitest';
import type { PtzCommand } from '../api/types';
import {
  CONTINUOUS_TIMEOUT_MS,
  cropRect,
  formatAxis,
  keyToDir,
  moveCommand,
  nextPresetName,
  stepCommand,
  PtzQueue,
  velocityOf,
} from './ptz';

describe('velocityOf / moveCommand', () => {
  it('押している方向から速度を作る', () => {
    expect(velocityOf(['right', 'up'])).toEqual({ pan: 0.5, tilt: 0.5, zoom: 0 });
    expect(velocityOf(['left', 'right', 'out'])).toEqual({ pan: 0, tilt: 0, zoom: -0.5 });
  });

  it('何も押していなければ stop、押していれば自動停止付きの continuous', () => {
    expect(moveCommand(velocityOf([]))).toEqual({ op: 'stop' });
    expect(moveCommand(velocityOf(['down']))).toEqual({
      op: 'continuous',
      pan: 0,
      tilt: -0.5,
      zoom: 0,
      timeout_ms: CONTINUOUS_TIMEOUT_MS,
    });
  });
});

describe('stepCommand', () => {
  it('タップは一定量の relative', () => {
    expect(stepCommand(['left'])).toEqual({ op: 'relative', pan: -0.1, tilt: 0 });
    expect(stepCommand(['in'])).toEqual({ op: 'relative', zoom: 0.1 });
    expect(stepCommand(['up', 'out'])).toEqual({ op: 'relative', pan: 0, tilt: 0.1, zoom: -0.1 });
  });
});

describe('keyToDir', () => {
  it('矢印と + / − を割り当てる', () => {
    expect(keyToDir({ key: 'ArrowLeft' })).toBe('left');
    expect(keyToDir({ key: '+' })).toBe('in');
    expect(keyToDir({ key: '=' })).toBe('in');
    expect(keyToDir({ key: '-' })).toBe('out');
    expect(keyToDir({ key: '1', code: 'NumpadAdd' })).toBe('in');
    expect(keyToDir({ key: 'a' })).toBeNull();
  });
});

describe('PtzQueue', () => {
  function setup() {
    const sent: PtzCommand[] = [];
    const resolvers: (() => void)[] = [];
    const q = new PtzQueue((c) => {
      sent.push(c);
      return new Promise<void>((r) => resolvers.push(r));
    });
    const flush = async () => {
      resolvers.shift()?.();
      await new Promise((r) => setTimeout(r, 0));
    };
    return { q, sent, flush };
  }

  it('前の送信が終わってから次を送る。待っている間の continuous / stop は最後の 1 つだけ', async () => {
    const { q, sent, flush } = setup();
    q.push({ op: 'continuous', pan: 0.5 });
    q.push({ op: 'continuous', pan: -0.5 });
    q.push({ op: 'stop' });
    expect(sent.map((c) => c.op)).toEqual(['continuous']);
    await flush();
    expect(sent.map((c) => c.op)).toEqual(['continuous', 'stop']);
    await flush();
    expect(q.size).toBe(0);
  });

  it('プリセットやホームは捨てない', async () => {
    const { q, sent, flush } = setup();
    q.push({ op: 'continuous', pan: 0.5 });
    q.push({ op: 'goto_preset', preset_token: '1' });
    q.push({ op: 'stop' });
    q.push({ op: 'home' });
    await flush();
    await flush();
    await flush();
    expect(sent.map((c) => c.op)).toEqual(['continuous', 'goto_preset', 'stop', 'home']);
  });

  it('失敗しても次を送る', async () => {
    const errors: string[] = [];
    const sent: string[] = [];
    const q = new PtzQueue(
      async (c) => {
        sent.push(c.op);
        if (c.op === 'home') throw new Error('x');
      },
      (_e, c) => errors.push(c.op),
    );
    q.push({ op: 'home' });
    q.push({ op: 'stop' });
    await new Promise((r) => setTimeout(r, 0));
    expect(sent).toEqual(['home', 'stop']);
    expect(errors).toEqual(['home']);
  });
});

describe('cropRect', () => {
  it('等倍は全体、最大倍率は 1/4 の窓を pan / tilt で動かす', () => {
    expect(cropRect(0, 0, 0)).toEqual({ left: 0, top: 0, width: 1, height: 1 });
    const r = cropRect(1, 1, 1);
    expect(r.width).toBeCloseTo(0.25);
    expect(r.left).toBeCloseTo(0.75); // 右端
    expect(r.top).toBeCloseTo(0); // 上端(tilt 1)
    expect(cropRect(-1, -1, 1).top).toBeCloseTo(0.75); // 下端
  });
});

describe('formatAxis / nextPresetName', () => {
  it('符号付きの 2 桁', () => {
    expect(formatAxis(0.25)).toBe('+0.25');
    expect(formatAxis(-0.1)).toBe('−0.10');
    expect(formatAxis(0.001)).toBe('0.00');
    expect(formatAxis(null)).toBe('—');
    expect(formatAxis(0.5, false)).toBe('0.50');
  });

  it('使われていない番号', () => {
    expect(nextPresetName([])).toBe('プリセット 1');
    expect(nextPresetName(['玄関', 'プリセット 2'])).toBe('プリセット 3');
  });
});

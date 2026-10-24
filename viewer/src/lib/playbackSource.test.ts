import { describe, expect, it } from 'vitest';
import { decideSource, playbackWindow, windowCovers } from './playbackSource';

const T = (s: string) => Date.parse(s);
const now = T('2026-10-24T10:30:00Z');
const processed = T('2026-10-24T10:25:00Z');

describe('decideSource', () => {
  it('ライブの要求はライブ', () => {
    expect(decideSource({ kind: 'live' }, processed, now)).toBe('live');
  });

  it('処理済み位置より前は S3', () => {
    expect(decideSource({ kind: 'time', at: T('2026-10-24T10:24:59Z') }, processed, now)).toBe('s3');
    expect(decideSource({ kind: 'time', at: T('2026-10-23T03:00:00Z') }, processed, now)).toBe('s3');
  });

  it('処理済み位置ちょうどと、それより後は KVS の ON_DEMAND', () => {
    expect(decideSource({ kind: 'time', at: processed }, processed, now)).toBe('kvs');
    expect(decideSource({ kind: 'time', at: T('2026-10-24T10:29:00Z') }, processed, now)).toBe('kvs');
  });

  it('現在にごく近い時刻と未来はライブ', () => {
    expect(decideSource({ kind: 'time', at: now - 5_000 }, processed, now)).toBe('live');
    expect(decideSource({ kind: 'time', at: now + 60_000 }, processed, now)).toBe('live');
  });

  it('処理済み位置がまだない(一度も処理していない)なら KVS', () => {
    expect(decideSource({ kind: 'time', at: T('2026-10-24T10:00:00Z') }, null, now)).toBe('kvs');
  });
});

describe('playbackWindow', () => {
  it('S3 は時の境界で 1 時間ぶん。処理済み位置で打ち切る', () => {
    expect(playbackWindow({ kind: 'time', at: T('2026-10-24T08:40:10Z') }, processed, now)).toEqual({
      kind: 's3',
      from: T('2026-10-24T08:00:00Z'),
      to: T('2026-10-24T09:00:00Z'),
    });
    expect(playbackWindow({ kind: 'time', at: T('2026-10-24T10:10:00Z') }, processed, now)).toEqual({
      kind: 's3',
      from: T('2026-10-24T10:00:00Z'),
      to: processed,
    });
  });

  it('KVS は分の頭から現在まで', () => {
    expect(playbackWindow({ kind: 'time', at: T('2026-10-24T10:27:30Z') }, processed, now)).toEqual({
      kind: 'kvs',
      from: T('2026-10-24T10:27:00Z'),
      to: now,
    });
  });

  it('ライブは範囲なし', () => {
    expect(playbackWindow({ kind: 'live' }, processed, now)).toEqual({ kind: 'live', from: null, to: null });
  });
});

describe('windowCovers', () => {
  const win = { kind: 's3' as const, from: T('2026-10-24T08:00:00Z'), to: T('2026-10-24T09:00:00Z') };
  it('同じ再生元で範囲内ならそのままシークできる', () => {
    expect(windowCovers(win, 's3', T('2026-10-24T08:30:00Z'))).toBe(true);
  });
  it('範囲外や再生元が違えば読み直す', () => {
    expect(windowCovers(win, 's3', T('2026-10-24T09:00:00Z'))).toBe(false);
    expect(windowCovers(win, 'kvs', T('2026-10-24T08:30:00Z'))).toBe(false);
  });
});

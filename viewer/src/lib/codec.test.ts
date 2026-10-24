import { describe, expect, it } from 'vitest';
import { mayBeHevc, normalizeCodec } from './codec';

describe('normalizeCodec', () => {
  it('シャドウの表記ゆれを揃える', () => {
    expect(normalizeCodec('H265')).toBe('H.265');
    expect(normalizeCodec('H.265')).toBe('H.265');
    expect(normalizeCodec('hevc')).toBe('H.265');
    expect(normalizeCodec('H264')).toBe('H.264');
    expect(normalizeCodec('h.264')).toBe('H.264');
    expect(normalizeCodec('AVC')).toBe('H.264');
    expect(normalizeCodec('JPEG')).toBeNull();
    expect(normalizeCodec(null)).toBeNull();
    expect(normalizeCodec(undefined)).toBeNull();
  });
});

describe('mayBeHevc', () => {
  it('H.264 と分かっているときだけ false', () => {
    expect(mayBeHevc('H264')).toBe(false);
    expect(mayBeHevc('H265')).toBe(true);
    expect(mayBeHevc(null)).toBe(true);
    expect(mayBeHevc('???')).toBe(true);
  });
});

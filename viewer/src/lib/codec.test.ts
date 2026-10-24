import { describe, expect, it } from 'vitest';
import { isCodecSwitching, mayBeHevc, normalizeCodec } from './codec';

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

describe('isCodecSwitching', () => {
  const base = { codec: 'H265', desired_codec: 'H264', desired_streaming: true, online: true };
  it('指示と実際が違えば切り替え中', () => {
    expect(isCodecSwitching(base)).toBe(true);
    expect(isCodecSwitching({ ...base, codec: 'H264' })).toBe(false);
    expect(isCodecSwitching({ ...base, desired_codec: 'H.264', codec: 'h264' })).toBe(false);
  });
  it('指示がない・配信停止・オフラインなら切り替え中としない', () => {
    expect(isCodecSwitching({ ...base, desired_codec: null })).toBe(false);
    expect(isCodecSwitching({ ...base, desired_streaming: false })).toBe(false);
    expect(isCodecSwitching({ ...base, online: false })).toBe(false);
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

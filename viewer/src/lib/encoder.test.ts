import { describe, expect, it } from 'vitest';
import { formatVolume, isEncoderApplying, monthlyGbAt } from './encoder';

describe('monthlyGbAt', () => {
  it('4 Mbps で 30 日は約 1.3 TB', () => {
    expect(monthlyGbAt(4000)).toBeCloseTo(1296, 0);
    expect(formatVolume(monthlyGbAt(4000))).toBe('1.3 TB');
    expect(formatVolume(monthlyGbAt(500))).toBe('162 GB');
  });
});

describe('isEncoderApplying', () => {
  const base = {
    encoder: { quality: 60, bitrate_limit_kbps: 1024, rate_control: 'VBR' as const },
    desired_encoder: { quality: 70, bitrate_limit_kbps: 4000 },
    encoder_error: null,
    desired_streaming: true,
    online: true,
  };
  it('指示と今の値が違う間だけ true', () => {
    expect(isEncoderApplying(base)).toBe(true);
    expect(isEncoderApplying({ ...base, encoder: { ...base.encoder, quality: 70, bitrate_limit_kbps: 4000 } })).toBe(false);
    // 指示していない項目(rate_control)は比べない
    expect(isEncoderApplying({ ...base, desired_encoder: { quality: 60 } })).toBe(false);
  });
  it('指示がない・失敗した・止めている・オフラインなら false', () => {
    expect(isEncoderApplying({ ...base, desired_encoder: null })).toBe(false);
    expect(isEncoderApplying({ ...base, encoder_error: 'カメラが拒否' })).toBe(false);
    expect(isEncoderApplying({ ...base, desired_streaming: false })).toBe(false);
    expect(isEncoderApplying({ ...base, online: false })).toBe(false);
  });
});

import { describe, expect, it } from 'vitest';
import { normalizeCamera } from './client';

describe('normalizeCamera', () => {
  it('シャドウの reported / desired から画面の項目を作る', () => {
    const c = normalizeCamera({
      camera_id: 'cam-001',
      gateway_id: 'gw-001',
      stream_name: 'cloud-camera-cam-001',
      reported: { streaming: true, codec: 'H.265', resolution: '1920x1080', error: null },
      desired: { streaming: false, password: '***' },
      reported_at: '2026-10-24T10:20:00Z',
      video_processed_until: '2026-10-24T10:25:00Z',
      detect_processed_until: null,
    });
    expect(c).toMatchObject({
      name: null,
      online: null,
      streaming: true,
      desired_streaming: false,
      codec: 'H.265',
      error: null,
    });
  });

  it('PTZ の対応は reported.ptz.supported で決める', () => {
    expect(normalizeCamera({ camera_id: 'a', reported: { ptz: { supported: true, pan: 0 } } }).ptz_supported).toBe(true);
    expect(normalizeCamera({ camera_id: 'b', reported: { ptz: { supported: false } } }).ptz_supported).toBe(false);
    expect(normalizeCamera({ camera_id: 'c', reported: { streaming: true } }).ptz_supported).toBe(false);
  });

  it('エンコーダーの設定は reported / desired の encoder', () => {
    const c = normalizeCamera({
      camera_id: 'a',
      reported: { encoder: { quality: 60, bitrate_limit_kbps: 4000, rate_control: 'VBR' }, encoder_error: 'x' },
      desired: { encoder: { quality: 70 } },
    });
    expect(c.encoder).toMatchObject({ quality: 60, bitrate_limit_kbps: 4000 });
    expect(c.desired_encoder).toEqual({ quality: 70 });
    expect(c.encoder_error).toBe('x');
    expect(normalizeCamera({ camera_id: 'b', reported: { encoder: null } }).encoder).toBeNull();
  });

  it('シャドウがまだない(reported も desired も null)', () => {
    const c = normalizeCamera({ camera_id: 'cam-002', reported: null, desired: null });
    expect(c.streaming).toBe(false);
    expect(c.desired_streaming).toBe(false);
    expect(c.online).toBeNull();
  });
});

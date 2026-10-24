// ブラウザの再生能力の判定。H.265(HEVC)はブラウザ、OS、端末で対応が分かれる(docs/browser-hevc.md)。

import Hls from 'hls.js';

export type Engine = 'hls.js' | 'native' | 'none';

export interface PlaybackSupport {
  engine: Engine;
  /** H.265 を再生できる見込みがあるか */
  hevc: boolean;
}

// Main プロファイルの代表的な codecs 文字列。hvc1 / hev1 のどちらかが通れば再生できる見込み。
const HEVC_CODECS = ['hvc1.1.6.L93.B0', 'hev1.1.6.L93.B0', 'hvc1.1.6.L120.90', 'hev1.1.6.L120.90'];

/** Safari(iOS の各ブラウザを含む WebKit)はネイティブの HLS を使う */
export function prefersNativeHls(ua: string, canPlayHls: boolean): boolean {
  if (!canPlayHls) return false;
  if (/iPhone|iPad|iPod/.test(ua)) return true;
  return /Safari/.test(ua) && !/Chrome|Chromium|Edg|OPR|Android|Firefox/.test(ua);
}

type MseCtor = { isTypeSupported(type: string): boolean };

export function detectSupport(): PlaybackSupport {
  if (typeof document === 'undefined') return { engine: 'none', hevc: false };
  const video = document.createElement('video');
  const canPlayHls = video.canPlayType('application/vnd.apple.mpegurl') !== '';
  const w = window as unknown as { ManagedMediaSource?: MseCtor; MediaSource?: MseCtor };
  const mse = w.ManagedMediaSource ?? w.MediaSource;

  if (prefersNativeHls(navigator.userAgent, canPlayHls)) {
    const hevc = HEVC_CODECS.some((c) => video.canPlayType(`video/mp4; codecs="${c}"`) !== '');
    return { engine: 'native', hevc };
  }
  if (Hls.isSupported() && mse) {
    const hevc = HEVC_CODECS.some((c) => mse.isTypeSupported(`video/mp4; codecs="${c}"`));
    return { engine: 'hls.js', hevc };
  }
  if (canPlayHls) {
    const hevc = HEVC_CODECS.some((c) => video.canPlayType(`video/mp4; codecs="${c}"`) !== '');
    return { engine: 'native', hevc };
  }
  return { engine: 'none', hevc: false };
}

export type VideoCodec = 'H.265' | 'H.264';

/** シャドウの reported.codec("H265"、"H.265"、"HEVC"、"H264" など)を表示用の名前にする。不明なら null */
export function normalizeCodec(codec: string | null | undefined): VideoCodec | null {
  const c = (codec ?? '').toUpperCase().replace(/[.\-\s]/g, '');
  if (c === 'H265' || c === 'HEVC') return 'H.265';
  if (c === 'H264' || c === 'AVC') return 'H.264';
  return null;
}

/**
 * H.265 の可能性があるか(H.265 か、コーデックが分からない)。H.264 と分かっているカメラだけ false。
 * 録画は撮ったときのコーデックのままなので、H.264 に切り替える前の録画は H.265 のことがある(そのときは再生時のエラーで案内する)。
 */
export function mayBeHevc(codec: string | null | undefined): boolean {
  return normalizeCodec(codec) !== 'H.264';
}

/**
 * 同じ画質(VMAF)で H.264 は H.265 の約何倍のビットレートになるか(docs/codec.md の測定)。
 * 帯域、KVS の取り込みと保存、S3 の保存はビットレートに比例する。
 */
export const H264_BITRATE_RATIO = 1.6;

interface CodecState {
  codec: string | null;
  desired_codec: string | null;
  desired_streaming: boolean;
  online: boolean | null;
}

/** 指示した映像形式と実際の形式が違う(エッジが切り替え中)。配信を止めている・オフラインのときは false */
export function isCodecSwitching(c: CodecState): boolean {
  const want = normalizeCodec(c.desired_codec);
  if (!want || !c.desired_streaming || c.online === false) return false;
  return normalizeCodec(c.codec) !== want;
}

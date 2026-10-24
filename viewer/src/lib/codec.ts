// ブラウザの再生能力の判定。H.265(HEVC)はブラウザ、OS、端末で対応が分かれる(構想.md「調査結果」)。

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

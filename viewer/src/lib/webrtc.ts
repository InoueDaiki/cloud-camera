// KVS WebRTC のライブの純粋なロジック(コーデックの優先順位、answer の読み取り、URL での上書き)。
// 接続そのものは src/player/webrtcLive.ts。
//
// - H.265 は RTCRtpReceiver.getCapabilities('video') に video/H265 があるときだけ優先する
//   (Chrome 136+ でハードウェアデコーダーがある環境、Safari 18+。Firefox は受けられない。docs/browser-hevc.md)
// - master(エッジ)は offer の m=video の並びを見て、H.265 が先なら main(H.265)、そうでなければ sub(H.264)を流す

export type RtcCodec = 'H.265' | 'H.264';

/** ライブの方式 */
export type LiveTransport = 'webrtc' | 'hls';

/** 最初の映像が出るまで待つ時間。過ぎたら HLS に切り替える */
export const WEBRTC_CONNECT_TIMEOUT_MS = 10_000;
/** 再生中に connectionState が failed / disconnected のまま続いたら諦める時間 */
export const WEBRTC_FAILED_GRACE_MS = 5_000;

export interface CodecCapability {
  mimeType: string;
  clockRate?: number;
  sdpFmtpLine?: string;
}

const isH265 = (c: CodecCapability) => /^video\/(h265|hevc)$/i.test(c.mimeType);
const isH264 = (c: CodecCapability) => /^video\/h264$/i.test(c.mimeType);
const isRtx = (c: CodecCapability) => /^video\/rtx$/i.test(c.mimeType);

/** このブラウザが WebRTC で H.265 を受けられるか */
export function canReceiveH265(caps: readonly CodecCapability[] | null | undefined): boolean {
  return !!caps?.some(isH265);
}

/** ブラウザの受信コーデック(取れなければ null) */
export function receiverCapabilities(): CodecCapability[] | null {
  if (typeof RTCRtpReceiver === 'undefined' || typeof RTCRtpReceiver.getCapabilities !== 'function') return null;
  return RTCRtpReceiver.getCapabilities('video')?.codecs ?? null;
}

export interface CodecPlan<T extends CodecCapability> {
  /** setCodecPreferences に渡す並び(H.265 → H.264 → 再送) */
  prefs: T[];
  /** 受けられる見込みのコーデック(優先する方) */
  expected: RtcCodec;
}

/**
 * 受信コーデックの優先順位を決める。
 * - H.265 を受けられて、カメラ(エッジ)が H.265 を出せて、上書きで止めていなければ H.265 → H.264
 * - それ以外は H.264 だけ
 * available は API の codecs(エッジが出せるコーデック。空なら不明として両方を候補にする)。
 */
export function planCodecs<T extends CodecCapability>(
  caps: readonly T[] | null | undefined,
  opts: { preferH265: boolean; available?: readonly string[] | null },
): CodecPlan<T> | null {
  if (!caps || caps.length === 0) return null;
  const avail = opts.available && opts.available.length ? opts.available : ['H265', 'H264'];
  const h265 = caps.filter(isH265);
  const h264 = caps.filter(isH264);
  const rtx = caps.filter(isRtx);
  const useH265 = opts.preferH265 && h265.length > 0 && avail.includes('H265');
  const useH264 = h264.length > 0 && avail.includes('H264');
  if (!useH265 && !useH264) return null;
  const prefs = [...(useH265 ? h265 : []), ...(useH264 ? h264 : []), ...rtx];
  return { prefs, expected: useH265 ? 'H.265' : 'H.264' };
}

/** answer の SDP から、映像のコーデックを読む(master は 1 つだけ載せる) */
export function answerCodec(sdp: string | null | undefined): RtcCodec | null {
  if (!sdp) return null;
  let inVideo = false;
  for (const line of sdp.split(/\r?\n/)) {
    if (line.startsWith('m=')) inVideo = line.startsWith('m=video');
    else if (inVideo) {
      const m = /^a=rtpmap:\d+ ([A-Za-z0-9]+)\//.exec(line);
      if (m) {
        const name = m[1].toUpperCase();
        if (name === 'H265' || name === 'HEVC') return 'H.265';
        if (name === 'H264') return 'H.264';
      }
    }
  }
  return null;
}

/** inbound-rtp の codec の mimeType から(getStats 用) */
export function codecFromMime(mime: string | null | undefined): RtcCodec | null {
  if (!mime) return null;
  if (/h265|hevc/i.test(mime)) return 'H.265';
  if (/h264/i.test(mime)) return 'H.264';
  return null;
}

/** URL の ?live= で方式を上書きする(検証用)。hls = WebRTC を使わない、h264 = WebRTC で H.264 だけ */
export type LiveOverride = 'auto' | 'hls' | 'h264';

export function liveOverride(search: string): LiveOverride {
  const v = new URLSearchParams(search).get('live');
  return v === 'hls' || v === 'h264' ? v : 'auto';
}

/** ライブの表示用の短い説明(例 "WebRTC · H.265") */
export function liveLabel(transport: LiveTransport | null, codec: RtcCodec | null): string | null {
  if (!transport) return null;
  const t = transport === 'webrtc' ? 'WebRTC' : 'HLS';
  return codec ? `${t} · ${codec}` : t;
}

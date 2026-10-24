import { describe, expect, it } from 'vitest';
import { answerCodec, canReceiveH265, codecFromMime, liveLabel, liveOverride, planCodecs } from './webrtc';

// Chrome 155(Mac)の RTCRtpReceiver.getCapabilities('video') を短くしたもの
const CHROME = [
  { mimeType: 'video/VP8', clockRate: 90000 },
  { mimeType: 'video/rtx', clockRate: 90000, sdpFmtpLine: 'apt=96' },
  { mimeType: 'video/H264', clockRate: 90000, sdpFmtpLine: 'level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f' },
  { mimeType: 'video/H264', clockRate: 90000, sdpFmtpLine: 'level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=640034' },
  { mimeType: 'video/AV1', clockRate: 90000 },
  { mimeType: 'video/H265', clockRate: 90000, sdpFmtpLine: 'level-id=180;profile-id=1;tier-flag=0;tx-mode=SRST' },
  { mimeType: 'video/red', clockRate: 90000 },
];
// Firefox は H.265 を受けられない
const FIREFOX = CHROME.filter((c) => c.mimeType !== 'video/H265');

describe('planCodecs', () => {
  it('H.265 を受けられるブラウザは H.265 → H.264 → 再送の順にする', () => {
    const plan = planCodecs(CHROME, { preferH265: true, available: ['H265', 'H264'] })!;
    expect(plan.expected).toBe('H.265');
    expect(plan.prefs.map((c) => c.mimeType)).toEqual(['video/H265', 'video/H264', 'video/H264', 'video/rtx']);
  });

  it('H.265 を受けられないブラウザは H.264 だけ', () => {
    const plan = planCodecs(FIREFOX, { preferH265: true, available: ['H265', 'H264'] })!;
    expect(plan.expected).toBe('H.264');
    expect(plan.prefs.map((c) => c.mimeType)).toEqual(['video/H264', 'video/H264', 'video/rtx']);
    expect(canReceiveH265(FIREFOX)).toBe(false);
    expect(canReceiveH265(CHROME)).toBe(true);
  });

  it('H.265 を止めた(?live=h264)ときと、カメラが H.264 だけのときは H.264', () => {
    expect(planCodecs(CHROME, { preferH265: false, available: ['H265', 'H264'] })!.expected).toBe('H.264');
    expect(planCodecs(CHROME, { preferH265: true, available: ['H264'] })!.expected).toBe('H.264');
  });

  it('カメラが H.265 だけ(sub がない)なら H.265 だけ。受けられなければ null(HLS へ)', () => {
    expect(planCodecs(CHROME, { preferH265: true, available: ['H265'] })!.prefs.map((c) => c.mimeType)).toEqual([
      'video/H265',
      'video/rtx',
    ]);
    expect(planCodecs(FIREFOX, { preferH265: true, available: ['H265'] })).toBeNull();
  });

  it('コーデックの一覧が不明なら両方を候補にする。受信能力が取れなければ null', () => {
    expect(planCodecs(CHROME, { preferH265: true, available: [] })!.expected).toBe('H.265');
    expect(planCodecs(null, { preferH265: true })).toBeNull();
  });
});

describe('answerCodec', () => {
  it('m=video の rtpmap から読む(音声の行は見ない)', () => {
    const sdp = [
      'v=0',
      'm=audio 9 UDP/TLS/RTP/SAVPF 111',
      'a=rtpmap:111 opus/48000/2',
      'm=video 9 UDP/TLS/RTP/SAVPF 127',
      'a=rtpmap:127 H265/90000',
      'a=fmtp:127 profile-id=2;level-id=180',
    ].join('\r\n');
    expect(answerCodec(sdp)).toBe('H.265');
    expect(answerCodec('m=video 9 UDP 125\na=rtpmap:125 H264/90000')).toBe('H.264');
    expect(answerCodec('m=video 9 UDP 96\na=rtpmap:96 VP8/90000')).toBeNull();
    expect(answerCodec(undefined)).toBeNull();
  });
});

describe('表示と上書き', () => {
  it('liveLabel', () => {
    expect(liveLabel('webrtc', 'H.265')).toBe('WebRTC · H.265');
    expect(liveLabel('hls', null)).toBe('HLS');
    expect(liveLabel(null, 'H.264')).toBeNull();
  });

  it('codecFromMime', () => {
    expect(codecFromMime('video/H265')).toBe('H.265');
    expect(codecFromMime('video/H264')).toBe('H.264');
    expect(codecFromMime('video/VP8')).toBeNull();
  });

  it('liveOverride は ?live=hls / h264 だけ受け付ける', () => {
    expect(liveOverride('?live=hls')).toBe('hls');
    expect(liveOverride('?camera=x&live=h264')).toBe('h264');
    expect(liveOverride('?live=foo')).toBe('auto');
    expect(liveOverride('')).toBe('auto');
  });
});

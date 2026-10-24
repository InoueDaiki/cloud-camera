// KVS WebRTC のライブ(viewer 役)。
//
// 1. API(GET /api/cameras/{id}/webrtc)から署名付き WSS URL と ICE サーバーをもらう(AWS の認証情報はブラウザに置かない)
// 2. KVS の JS SDK の SignalingClient に、その URL を返すだけの requestSigner を渡してつなぐ
// 3. 映像は受信だけ(recvonly)。setCodecPreferences で H.265 → H.264 の順に並べ(H.265 を受けられるときだけ)、
//    master(エッジ)が offer の並びを見て main(H.265)か sub(H.264)を流す
// 4. 最初の映像が WEBRTC_CONNECT_TIMEOUT_MS 以内に出なければ失敗(呼び出し側が HLS に切り替える)。
//    再生中に切れたら onLost を呼ぶ
//
// モック(npm run dev:mock)では API が mock: true を返し、ブラウザ内の疑似 master(src/mocks/rtcMaster.ts)につなぐ。

import { api, ApiError } from '../api/client';
import type { WebRtcSessionInfo } from '../api/types';
import {
  answerCodec,
  codecFromMime,
  planCodecs,
  receiverCapabilities,
  WEBRTC_CONNECT_TIMEOUT_MS,
  WEBRTC_FAILED_GRACE_MS,
  type RtcCodec,
} from '../lib/webrtc';

/** SignalingClient(と疑似 master)の使う部分だけ */
export interface SignalingLike {
  on(event: 'open' | 'close', cb: () => void): unknown;
  on(event: 'sdpAnswer', cb: (d: RTCSessionDescriptionInit) => void): unknown;
  on(event: 'iceCandidate', cb: (c: RTCIceCandidateInit) => void): unknown;
  on(event: 'error', cb: (e: unknown) => void): unknown;
  open(): void;
  close(): void;
  sendSdpOffer(d: RTCSessionDescription): void;
  sendIceCandidate(c: RTCIceCandidate): void;
}

export interface RtcStats {
  codec: RtcCodec | null;
  framesDecoded: number | null;
  fps: number | null;
  width: number | null;
  height: number | null;
  /** 選ばれた候補(host / srflx / prflx / relay)。relay なら TURN 経由 */
  candidateType: string | null;
  rttMs: number | null;
  jitterBufferMs: number | null;
}

export interface RtcLive {
  /** answer で決まったコーデック(getStats で確かめたらそちらに置き換える) */
  codec: RtcCodec | null;
  clientId: string;
  /** API の呼び出しから最初の映像まで(ms) */
  firstFrameMs: number;
  stats(): Promise<RtcStats>;
  close(): void;
}

/** WebRTC で見られなかった(HLS に切り替える)。reason は表示とログ用 */
export class WebRtcUnavailable extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export interface RtcStartOptions {
  cameraId: string;
  video: HTMLVideoElement;
  preferH265: boolean;
  timeoutMs?: number;
  /** 中止(別のカメラや録画に切り替えたとき) */
  signal?: AbortSignal;
  /** 再生が始まったあとに切れた・映像が止まった */
  onLost: (reason: string) => void;
}

type KvsSdk = typeof import('amazon-kinesis-video-streams-webrtc');

let sdkPromise: Promise<KvsSdk> | null = null;

// npm の lib/ は Node の events と ws を require するので、ブラウザ向けの dist(window.KVSWebRTC を作る)を遅延読み込みする
function loadSdk(): Promise<KvsSdk> {
  sdkPromise ??= import('amazon-kinesis-video-streams-webrtc/dist/kvs-webrtc.min.js').then(
    () => (window as unknown as { KVSWebRTC: KvsSdk }).KVSWebRTC,
  );
  return sdkPromise;
}

async function kvsSignaling(info: WebRtcSessionInfo): Promise<SignalingLike> {
  const sdk = await loadSdk();
  return new sdk.SignalingClient({
    channelARN: info.channel_arn,
    channelEndpoint: info.wss_endpoint,
    clientId: info.client_id,
    role: sdk.Role.VIEWER,
    region: info.region,
    // 署名は API(Lambda)で済んでいる。SDK にはその URL を返すだけの signer を渡す
    requestSigner: { getSignedURL: async () => info.wss_url },
  }) as unknown as SignalingLike;
}

async function signalingFor(info: WebRtcSessionInfo): Promise<SignalingLike> {
  if (import.meta.env.VITE_MOCK === 'true' && info.mock) {
    const { createMockSignaling } = await import('../mocks/rtcMaster');
    return createMockSignaling(info);
  }
  return kvsSignaling(info);
}

function apiReason(e: unknown): WebRtcUnavailable {
  if (e instanceof ApiError) return new WebRtcUnavailable(e.code ?? `HTTP_${e.status}`, e.message);
  return new WebRtcUnavailable('API', e instanceof Error ? e.message : String(e));
}

export async function readStats(pc: RTCPeerConnection): Promise<RtcStats> {
  const report = await pc.getStats();
  const byId = new Map<string, Record<string, unknown>>();
  report.forEach((s: Record<string, unknown>) => byId.set(String(s.id), s));
  let inbound: Record<string, unknown> | undefined;
  let pair: Record<string, unknown> | undefined;
  report.forEach((s: Record<string, unknown>) => {
    if (s.type === 'inbound-rtp' && s.kind === 'video') inbound = s;
    if (s.type === 'transport' && s.selectedCandidatePairId) pair = byId.get(String(s.selectedCandidatePairId));
  });
  if (!pair) {
    report.forEach((s: Record<string, unknown>) => {
      if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded') pair = s;
    });
  }
  const local = pair ? byId.get(String(pair.localCandidateId)) : undefined;
  const codec = inbound ? byId.get(String(inbound.codecId)) : undefined;
  const num = (v: unknown) => (typeof v === 'number' ? v : null);
  const emitted = num(inbound?.jitterBufferEmittedCount);
  const delay = num(inbound?.jitterBufferDelay);
  return {
    codec: codecFromMime(codec?.mimeType as string | undefined),
    framesDecoded: num(inbound?.framesDecoded),
    fps: num(inbound?.framesPerSecond),
    width: num(inbound?.frameWidth),
    height: num(inbound?.frameHeight),
    candidateType: (local?.candidateType as string | undefined) ?? null,
    rttMs: num(pair?.currentRoundTripTime) !== null ? Math.round((pair!.currentRoundTripTime as number) * 1000) : null,
    jitterBufferMs: emitted && delay !== null ? Math.round((delay / emitted) * 1000) : null,
  };
}

export async function startWebRtc(o: RtcStartOptions): Promise<RtcLive> {
  if (typeof RTCPeerConnection === 'undefined') {
    throw new WebRtcUnavailable('NO_WEBRTC', 'このブラウザは WebRTC を使えません');
  }
  const t0 = performance.now();
  let info: WebRtcSessionInfo;
  try {
    info = await api.webrtc(o.cameraId);
  } catch (e) {
    throw apiReason(e);
  }
  if (o.signal?.aborted) throw new WebRtcUnavailable('ABORTED', '中止しました');
  const plan = planCodecs(receiverCapabilities(), { preferH265: o.preferH265, available: info.codecs });
  if (!plan) throw new WebRtcUnavailable('NO_CODEC', 'WebRTC で受けられる映像形式がありません');
  const signaling = await signalingFor(info);
  const pc = new RTCPeerConnection({ iceServers: info.ice_servers });
  const tr = pc.addTransceiver('video', { direction: 'recvonly' });
  if (typeof tr.setCodecPreferences === 'function') tr.setCodecPreferences(plan.prefs as RTCRtpCodec[]);
  const video = o.video;

  return new Promise<RtcLive>((resolve, reject) => {
    let closed = false;
    let playing = false;
    let stream: MediaStream | null = null;
    let failedTimer: number | undefined;
    let watchdog: number | undefined;
    let lastFrames = -1;
    let stalledSince = 0;

    const close = () => {
      if (closed) return;
      closed = true;
      window.clearTimeout(timer);
      window.clearTimeout(failedTimer);
      window.clearInterval(watchdog);
      video.removeEventListener('loadeddata', onFirstFrame);
      try {
        signaling.close();
      } catch {
        // 閉じ済み
      }
      pc.close();
      if (stream && video.srcObject === stream) video.srcObject = null;
    };
    const fail = (code: string, reason: string) => {
      if (closed) return;
      close();
      if (playing) {
        if (code !== 'ABORTED') o.onLost(reason);
      } else reject(new WebRtcUnavailable(code, reason));
    };
    const timer = window.setTimeout(
      () => fail('TIMEOUT', `${Math.round((o.timeoutMs ?? WEBRTC_CONNECT_TIMEOUT_MS) / 1000)} 秒以内に映像が届きませんでした`),
      o.timeoutMs ?? WEBRTC_CONNECT_TIMEOUT_MS,
    );

    const handle: RtcLive = {
      codec: null,
      clientId: info.client_id,
      firstFrameMs: 0,
      stats: () => readStats(pc),
      close,
    };

    function onFirstFrame() {
      if (closed || playing) return;
      playing = true;
      window.clearTimeout(timer);
      handle.firstFrameMs = Math.round(performance.now() - t0);
      resolve(handle);
      // 映像が止まったまま(master の RTSP が切れたなど)なら諦める。一時停止中は数えない
      watchdog = window.setInterval(() => {
        void readStats(pc)
          .then((st) => {
            if (closed) return;
            if (st.codec) handle.codec = st.codec;
            const f = st.framesDecoded ?? 0;
            if (video.paused || f !== lastFrames) {
              lastFrames = f;
              stalledSince = 0;
            } else if (!stalledSince) {
              stalledSince = Date.now();
            } else if (Date.now() - stalledSince > 8000) {
              fail('STALLED', '映像が止まりました');
            }
          })
          .catch(() => undefined);
      }, 2000);
    }
    video.addEventListener('loadeddata', onFirstFrame);
    o.signal?.addEventListener('abort', () => fail('ABORTED', '中止しました'), { once: true });

    pc.addEventListener('track', (ev) => {
      if (closed) return;
      stream = ev.streams[0] ?? new MediaStream([ev.track]);
      video.srcObject = stream;
      void video.play().catch(() => undefined);
    });
    pc.addEventListener('connectionstatechange', () => {
      const st = pc.connectionState;
      if (st === 'connected') {
        window.clearTimeout(failedTimer);
        failedTimer = undefined;
      } else if ((st === 'failed' || st === 'disconnected') && failedTimer === undefined) {
        // トリクル ICE の途中で一瞬 failed になってから connected になることがあるので、すぐには諦めない
        failedTimer = window.setTimeout(() => fail('ICE', `接続が切れました(${st})`), WEBRTC_FAILED_GRACE_MS);
      } else if (st === 'closed') {
        fail('CLOSED', '接続が閉じられました');
      }
    });
    pc.addEventListener('icecandidate', ({ candidate }) => {
      if (candidate && !closed) signaling.sendIceCandidate(candidate);
    });

    signaling.on('open', () => {
      void (async () => {
        try {
          await pc.setLocalDescription(await pc.createOffer());
          if (!closed && pc.localDescription) signaling.sendSdpOffer(pc.localDescription);
        } catch (e) {
          fail('OFFER', `offer を作れませんでした: ${e instanceof Error ? e.message : String(e)}`);
        }
      })();
    });
    signaling.on('sdpAnswer', (answer) => {
      handle.codec = answerCodec(answer.sdp);
      pc.setRemoteDescription(answer).catch((e: unknown) =>
        fail('ANSWER', `answer を受け付けられませんでした: ${e instanceof Error ? e.message : String(e)}`),
      );
    });
    signaling.on('iceCandidate', (c) => {
      if (!closed) pc.addIceCandidate(c).catch(() => undefined);
    });
    signaling.on('error', (e) => {
      if (!playing) fail('SIGNALING', `シグナリングにつながりません(${e instanceof Error ? e.message : 'error'})`);
    });
    signaling.open();
  });
}

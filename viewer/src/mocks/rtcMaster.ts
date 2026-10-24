// モック用の疑似 master。KVS のシグナリングの代わりに、同じページ内の RTCPeerConnection が
// canvas に描いたテストパターン(カメラ名と時刻)を送る。ビューワーの WebRTC の経路(コーデックの並び、
// answer、ICE、最初の映像、表示)はそのまま通る。npm run dev:mock のときだけ読み込む。

import type { WebRtcSessionInfo } from '../api/types';
import type { SignalingLike } from '../player/webrtcLive';

type Handler = (...args: never[]) => void;

const W = 640;
const H = 360;

function draw(ctx: CanvasRenderingContext2D, name: string, frame: number) {
  const g = ctx.createLinearGradient(0, 0, W, H);
  g.addColorStop(0, '#1b2733');
  g.addColorStop(1, '#2c3e50');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  // ゆっくり動く帯(映像が動いていることが分かるように)
  const x = (frame * 4) % (W + 120);
  ctx.fillStyle = 'rgba(75, 136, 227, 0.35)';
  ctx.fillRect(x - 120, 0, 120, H);
  ctx.fillStyle = '#fff';
  ctx.font = '600 28px system-ui, sans-serif';
  ctx.fillText(name, 24, H / 2 - 8);
  ctx.font = '22px ui-monospace, Menlo, monospace';
  const now = new Date();
  ctx.fillText(now.toISOString().replace('T', ' ').replace('Z', ' UTC'), 24, H / 2 + 28);
  ctx.font = '16px system-ui, sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.75)';
  ctx.fillText('WebRTC(モック)— ブラウザ内の疑似 master', 24, H - 24);
}

export function createMockSignaling(info: WebRtcSessionInfo): SignalingLike {
  const handlers = new Map<string, Handler[]>();
  const emit = (ev: string, ...args: unknown[]) =>
    handlers.get(ev)?.forEach((f) => (f as (...a: unknown[]) => void)(...args));
  let master: RTCPeerConnection | null = null;
  let tick: number | undefined;
  let closed = false;

  const sig = {
    on(event: string, cb: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), cb]);
      return sig;
    },
    open() {
      window.setTimeout(() => !closed && emit('open'), 80);
    },
    sendSdpOffer(offer: RTCSessionDescription) {
      void (async () => {
        const canvas = document.createElement('canvas');
        canvas.width = W;
        canvas.height = H;
        const ctx = canvas.getContext('2d')!;
        let frame = 0;
        draw(ctx, info.camera_id ?? info.channel_name, frame);
        tick = window.setInterval(() => draw(ctx, info.camera_id ?? info.channel_name, ++frame), 1000 / 15);
        const stream = canvas.captureStream(15);
        master = new RTCPeerConnection();
        master.addEventListener('icecandidate', ({ candidate }) => {
          if (candidate && !closed) emit('iceCandidate', candidate.toJSON());
        });
        await master.setRemoteDescription(offer);
        for (const t of stream.getVideoTracks()) master.addTrack(t, stream);
        const answer = await master.createAnswer();
        await master.setLocalDescription(answer);
        // 実際の KVS と同じく、少し遅れて answer が届く
        window.setTimeout(() => !closed && emit('sdpAnswer', master!.localDescription!.toJSON()), 120);
      })();
    },
    sendIceCandidate(c: RTCIceCandidate) {
      void master?.addIceCandidate(c).catch(() => undefined);
    },
    close() {
      closed = true;
      window.clearInterval(tick);
      master?.close();
      master = null;
    },
  };
  return sig as unknown as SignalingLike;
}

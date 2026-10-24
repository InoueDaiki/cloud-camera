// KVS WebRTC の確認用ビューワー(viewer 役)。
// 1. API(試作では scripts/webrtc/serve.py)から署名付き WSS URL と ICE サーバーをもらう(AWS の認証情報はブラウザに置かない)
// 2. JS SDK の SignalingClient に、署名済み URL を返すだけの requestSigner を渡して接続する
// 3. 映像は受信だけ(recvonly)。コーデックは setCodecPreferences で選ぶ
// 比較用に、KVS の HLS(LIVE)も同じページで再生できる(mode=hls。hls.js を使う)
// URL の引数: ?camera=cam-001&mode=webrtc|hls&codec=h264|h265|any&relay=1&autostart=1
'use strict';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
let pc = null;
let signaling = null;
let statsTimer = null;
let prevInbound = null;
let hls = null;

// Playwright から読む計測値
window.ccStats = { state: 'idle', log: [] };

function log(msg, isErr) {
  const line = `${new Date().toISOString()} ${msg}`;
  window.ccStats.log.push(line);
  const div = document.createElement('div');
  div.textContent = line;
  if (isErr) div.className = 'err';
  $('log').prepend(div);
  (isErr ? console.error : console.log)(line);
}

// 映像の焼き込み時刻と同じ形式(UTC、ミリ秒)
function tick() {
  $('clock').textContent = new Date().toISOString();
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function videoCodecs() {
  const caps = (window.RTCRtpReceiver && RTCRtpReceiver.getCapabilities) ? RTCRtpReceiver.getCapabilities('video') : null;
  return caps ? caps.codecs : [];
}

function showH265Capability() {
  const h265 = videoCodecs().filter((c) => /h265|hevc/i.test(c.mimeType));
  window.ccStats.h265_receive_capable = h265.length > 0;
  window.ccStats.receive_codecs = [...new Set(videoCodecs().map((c) => c.mimeType))];
  $('h265cap').textContent = h265.length ? 'このブラウザは H.265 を受信できる' : 'このブラウザは H.265 を受信できない';
}

// --- 映像の変化の検出(PTZ の操作が画面に出るまでの計測用) ------------------------------
// テストパターンで毎フレーム動くもの(左上の焼き込み、右の数字、下の虹色の帯)を避け、
// 左から中央の静止した領域(横 2〜62%、縦 30〜72%)を小さく縮小して比べる。
// armChangeDetector() の時点のフレームを基準にし、基準との差が閾値を超えた最初のフレームの表示時刻を記録する。
const det = { canvas: document.createElement('canvas'), base: null, armed: false };
det.canvas.width = 64;
det.canvas.height = 36;
function frameSignature() {
  const v = $('video');
  if (!v.videoWidth) return null;
  const ctx = det.canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(v, v.videoWidth * 0.02, v.videoHeight * 0.3, v.videoWidth * 0.6, v.videoHeight * 0.42, 0, 0, 64, 36);
  return ctx.getImageData(0, 0, 64, 36).data;
}
function diff(a, b) {
  let d = 0;
  for (let i = 0; i < a.length; i += 4) d += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]);
  return d / (a.length / 4) / 3;
}
function onFrame(now, meta) {
  const v = $('video');
  if (det.armed) {
    const sig = frameSignature();
    if (sig && !det.base) det.base = sig;
    else if (sig) {
      const d = diff(sig, det.base);
      if (d > det.threshold) {
        det.armed = false;
        // expectedDisplayTime は合成器に渡る予定時刻(performance 時刻)。壁時計に直す
        const shown = meta && meta.expectedDisplayTime ? Date.now() + (meta.expectedDisplayTime - performance.now()) : Date.now();
        window.ccStats.change = { t_ms: Math.round(shown), diff: Math.round(d * 10) / 10 };
      }
    }
  }
  if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(onFrame);
}
window.armChangeDetector = (threshold = 6) => {
  det.base = null;
  det.threshold = threshold;
  window.ccStats.change = null;
  det.armed = true;
};

// --- HLS(比較用) ---------------------------------------------------------------------
async function startHls() {
  const camera = $('camera').value;
  window.ccStats = { ...window.ccStats, state: 'issuing', camera, mode: 'hls', t_start: performance.now() };
  const res = await fetch(`/api/cameras/${encodeURIComponent(camera)}/kvs-hls`);
  const info = await res.json();
  if (!res.ok) { log(`発行に失敗: ${JSON.stringify(info)}`, true); window.ccStats.state = 'error'; return; }
  const v = $('video');
  v.addEventListener('loadeddata', () => {
    window.ccStats.t_first_frame_ms = Math.round(performance.now() - window.ccStats.t_start);
    log(`最初のフレーム: ${window.ccStats.t_first_frame_ms}ms(接続ボタンから)`);
    window.ccStats.state = 'connected';
  }, { once: true });
  if (window.Hls && Hls.isSupported()) {
    // KVS の LIVE は断片 1 秒。既定(liveSyncDurationCount=3)のまま、なるべく端に寄せる設定だけ足す
    hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3, maxLiveSyncPlaybackRate: 1.2 });
    hls.loadSource(info.url);
    hls.attachMedia(v);
  } else {
    v.src = info.url;
  }
  v.play().catch(() => {});
  statsTimer = setInterval(() => {
    window.ccStats.frames_decoded = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames : null;
    window.ccStats.resolution = v.videoWidth ? `${v.videoWidth}x${v.videoHeight}` : null;
    window.ccStats.buffer_ahead_s = v.buffered.length ? Math.round((v.buffered.end(v.buffered.length - 1) - v.currentTime) * 100) / 100 : null;
    $('stats').textContent = `mode               hls\nresolution         ${window.ccStats.resolution}\nframes             ${window.ccStats.frames_decoded}\nbuffer_ahead_s     ${window.ccStats.buffer_ahead_s}`;
  }, 1000);
}

async function start() {
  stop();
  if ($('mode').value === 'hls') return startHls();
  const camera = $('camera').value;
  const codec = $('codec').value;
  const relay = $('relay').checked;
  window.ccStats = { ...window.ccStats, state: 'issuing', camera, codec_pref: codec, relay, t_start: performance.now() };
  const res = await fetch(`/api/cameras/${encodeURIComponent(camera)}/webrtc`);
  const info = await res.json();
  if (!res.ok) { log(`発行に失敗: ${JSON.stringify(info)}`, true); window.ccStats.state = 'error'; return; }
  log(`発行: channel=${info.channel_name} client=${info.client_id} expires=${info.expires_at} (${info.issue_ms}ms)`);

  pc = new RTCPeerConnection({ iceServers: info.ice_servers, iceTransportPolicy: relay ? 'relay' : 'all' });
  const tr = pc.addTransceiver('video', { direction: 'recvonly' });
  if (codec !== 'any' && tr.setCodecPreferences) {
    const want = codec === 'h265' ? /video\/(h265|hevc)/i : /video\/h264/i;
    const prefs = videoCodecs().filter((c) => want.test(c.mimeType) || /rtx/i.test(c.mimeType));
    if (!prefs.some((c) => want.test(c.mimeType))) {
      log(`このブラウザは ${codec} を受信できない`, true);
      window.ccStats.state = 'unsupported';
      return;
    }
    tr.setCodecPreferences(prefs);
  }
  pc.addEventListener('track', (ev) => {
    $('video').srcObject = ev.streams[0] || new MediaStream([ev.track]);
  });
  pc.addEventListener('connectionstatechange', () => {
    log(`peer: ${pc.connectionState}`);
    window.ccStats.state = pc.connectionState;
  });
  $('video').addEventListener('loadeddata', () => {
    window.ccStats.t_first_frame_ms = Math.round(performance.now() - window.ccStats.t_start);
    log(`最初のフレーム: ${window.ccStats.t_first_frame_ms}ms(接続ボタンから)`);
  }, { once: true });

  signaling = new KVSWebRTC.SignalingClient({
    channelARN: info.channel_arn,
    channelEndpoint: info.wss_endpoint,
    clientId: info.client_id,
    role: KVSWebRTC.Role.VIEWER,
    region: info.region,
    // 署名はサーバー側で済んでいる。SDK にはその URL を返すだけの signer を渡す
    requestSigner: { getSignedURL: async () => info.wss_url },
  });
  signaling.on('open', async () => {
    log('signaling: open');
    await pc.setLocalDescription(await pc.createOffer());
    signaling.sendSdpOffer(pc.localDescription);
  });
  signaling.on('sdpAnswer', async (answer) => {
    await pc.setRemoteDescription(answer);
    const m = /a=rtpmap:\d+ (H26[45])\/90000/.exec(answer.sdp || '');
    log(`answer: 映像コーデック ${m ? m[1] : '?'}`);
  });
  signaling.on('iceCandidate', (c) => pc.addIceCandidate(c));
  signaling.on('close', () => log('signaling: close'));
  signaling.on('error', (e) => log(`signaling error: ${e && (e.message || e.type || e)}`, true));
  pc.addEventListener('icecandidate', ({ candidate }) => {
    if (candidate) {
      if (candidate.candidate) log(`local candidate: ${candidate.type || '?'} ${candidate.protocol || ''} ${candidate.relayProtocol || ''}`.trim());
      signaling.sendIceCandidate(candidate);
    }
  });
  pc.addEventListener('icecandidateerror', (e) => log(`ICE candidate error: ${e.url} ${e.errorCode} ${e.errorText}`, true));
  pc.addEventListener('icegatheringstatechange', () => log(`ice gathering: ${pc.iceGatheringState}`));
  signaling.open();
  statsTimer = setInterval(collectStats, 1000);
}

function stop() {
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = null;
  prevInbound = null;
  if (hls) hls.destroy();
  hls = null;
  if (signaling) signaling.close();
  if (pc) pc.close();
  signaling = null;
  pc = null;
  $('video').srcObject = null;
}

async function collectStats() {
  if (!pc) return;
  const report = await pc.getStats();
  const byId = new Map();
  report.forEach((s) => byId.set(s.id, s));
  let inbound = null;
  let pair = null;
  report.forEach((s) => {
    if (s.type === 'inbound-rtp' && s.kind === 'video') inbound = s;
    if (s.type === 'transport' && s.selectedCandidatePairId) pair = byId.get(s.selectedCandidatePairId);
  });
  if (!pair) report.forEach((s) => { if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded') pair = s; });
  const local = pair && byId.get(pair.localCandidateId);
  const remote = pair && byId.get(pair.remoteCandidateId);
  const codec = inbound && byId.get(inbound.codecId);
  const out = {
    connection: pc.connectionState,
    local_candidate: local ? `${local.candidateType}/${local.protocol}${local.relayProtocol ? '/' + local.relayProtocol : ''}` : null,
    remote_candidate: remote ? `${remote.candidateType}/${remote.protocol}` : null,
    rtt_ms: pair && pair.currentRoundTripTime != null ? Math.round(pair.currentRoundTripTime * 1000) : null,
    codec: codec ? `${codec.mimeType} ${codec.sdpFmtpLine || ''}`.trim() : null,
    decoder: inbound ? inbound.decoderImplementation : null,
    resolution: inbound && inbound.frameWidth ? `${inbound.frameWidth}x${inbound.frameHeight}` : null,
    fps: inbound ? inbound.framesPerSecond : null,
    frames_decoded: inbound ? inbound.framesDecoded : null,
    frames_dropped: inbound ? inbound.framesDropped : null,
    packets_lost: inbound ? inbound.packetsLost : null,
    nack: inbound ? inbound.nackCount : null,
    pli: inbound ? inbound.pliCount : null,
    kbps: null,
    jitter_buffer_ms: null,
    processing_ms: null,
    decode_ms: null,
  };
  if (inbound && prevInbound) {
    const dt = (inbound.timestamp - prevInbound.timestamp) / 1000;
    out.kbps = Math.round(((inbound.bytesReceived - prevInbound.bytesReceived) * 8) / 1000 / dt);
    const de = inbound.jitterBufferEmittedCount - prevInbound.jitterBufferEmittedCount;
    if (de > 0) out.jitter_buffer_ms = Math.round(((inbound.jitterBufferDelay - prevInbound.jitterBufferDelay) / de) * 1000);
    // 受信(最初のパケット)からデコード完了まで(ジッタバッファ + デコード)と、デコードだけの時間
    const df = inbound.framesDecoded - prevInbound.framesDecoded;
    if (df > 0 && inbound.totalProcessingDelay != null) {
      out.processing_ms = Math.round(((inbound.totalProcessingDelay - prevInbound.totalProcessingDelay) / df) * 1000);
    }
    if (df > 0 && inbound.totalDecodeTime != null) {
      out.decode_ms = Math.round(((inbound.totalDecodeTime - prevInbound.totalDecodeTime) / df) * 1000);
    }
  }
  prevInbound = inbound;
  Object.assign(window.ccStats, out);
  $('stats').textContent = Object.entries(out).map(([k, v]) => `${k.padEnd(18)} ${v == null ? '-' : v}`).join('\n');
}

$('start').addEventListener('click', () => start().catch((e) => log(`接続に失敗: ${e}`, true)));
$('stop').addEventListener('click', stop);
if (params.get('camera')) $('camera').value = params.get('camera');
if (params.get('mode')) $('mode').value = params.get('mode');
if ($('video').requestVideoFrameCallback) $('video').requestVideoFrameCallback(onFrame);
if (params.get('codec')) $('codec').value = params.get('codec');
if (params.get('relay') === '1') $('relay').checked = true;
showH265Capability();
if (params.get('autostart') === '1') start().catch((e) => log(`接続に失敗: ${e}`, true));

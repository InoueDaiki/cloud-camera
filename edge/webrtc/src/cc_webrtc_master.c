/*
 * cc_webrtc_master: カメラの RTSP を再エンコードせずに KVS WebRTC(シグナリングチャネルの master)へ流す。
 * エッジのエージェント(edge/agent/cc_edge/webrtc.py)がカメラごとに 1 プロセス起動する。
 *
 * C SDK のサンプル共通処理(samples/common/Common.c: シグナリング、ピア接続、ICE、TURN、IoT 認証)をそのまま使い、
 * 映像の取り出しと「視聴者ごとのコーデックの選択」を自前にしている。
 *
 *   main(H.265 など): rtspsrc ! rtph265depay ! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au ! appsink
 *   sub (H.264)     : rtspsrc ! rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! appsink
 *
 * 視聴者ごとのコーデック: 視聴者の offer の m=video の並び(ブラウザの setCodecPreferences の順)を見て、
 *   H.265 が H.264 より前にあり H.265 のストリームがあれば H.265(main)、そうでなければ H.264(sub)を選ぶ。
 *   その視聴者のトランシーバーは選んだコーデックだけで作り、answer にもそのコーデックだけが載る。
 *   H.265 の視聴者と H.264 の視聴者が同時にいれば、main と sub の両方の RTSP を読む。
 * RTSP は視聴者がいるストリームだけ読み、そのストリームの視聴者が 0 人になって CC_IDLE_STOP_S 秒たったら閉じる。
 *
 * 環境変数(エージェントが組み立てる):
 *   CC_WEBRTC_CHANNEL    シグナリングチャネル名(必須)
 *   CC_MAIN_RTSP_URL     main の RTSP URL / CC_MAIN_CODEC h265(既定)/ h264
 *   CC_SUB_RTSP_URL      sub の RTSP URL  / CC_SUB_CODEC  h264(既定)/ h265
 *                        (どちらか一方は必須。ないストリームのコーデックは選ばれない)
 *   CC_RTSP_USER / CC_RTSP_PASS  RTSP の認証(任意)
 *   CC_RTSP_LATENCY_MS   rtspsrc の latency(既定 0)
 *   CC_IDLE_STOP_S       視聴者が 0 人になってから RTSP を止めるまでの秒数(既定 10)
 *   CC_STALL_TIMEOUT_S   視聴者がいるのにこの秒数フレームが来なければ RTSP をつなぎ直す(既定 10)
 *   CC_STATS_INTERVAL_S  統計(JSON 1 行)を出す間隔(既定 10)
 *   KVS_ICE_TRANSPORT_POLICY=relay  TURN を強制(サンプル共通処理の機能)
 *   AWS_IOT_CORE_*       IoT 認証情報プロバイダー(IOT_CORE_ENABLE_CREDENTIALS でビルド)
 *
 * 標準出力(JSON 1 行ずつ。エージェントが読む):
 *   {"cc_event": "signaling", "state": "connected" | ...}     シグナリングの状態が変わったとき
 *   {"cc_event": "ready", "channel": "..."}                   シグナリングにつながり、視聴者を待てるようになった
 *   {"cc_event": "viewer", "peer": "...", "stream": "main", "codec": "H265"}   offer を受けて視聴者のコーデックを決めた
 *   {"cc_event": "reject", "peer": "...", "reason": "..."}    受けられる映像がない offer
 *   {"cc_stats": true, "viewers": n, "streams": {"main": {...}, "sub": {...}}, "sent_kbps": x, ...}
 */
#include "../common/Samples.h"
#include <stdarg.h>
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

extern PSampleConfiguration gSampleConfiguration;

#define CC_STREAM_MAIN  0
#define CC_STREAM_SUB   1
#define CC_STREAM_COUNT 2
#define CC_MAX_PEERS    (DEFAULT_MAX_CONCURRENT_STREAMING_SESSION * 2)
#define CC_MAX_PTS      64

typedef struct {
    PCHAR name;  // "main" / "sub"
    PCHAR rtspUrl;
    BOOL h265;
    // 以下はパイプラインを管理するスレッドだけが触る
    GstElement* pipeline;
    UINT64 startedAt;
    UINT64 idleSince;
    UINT64 nextStartAt;
    UINT32 backoffSec;
    UINT32 restarts;
    // appsink のスレッドが書く
    volatile SIZE_T lastSampleAt; // 100ns 単位(GETTIME)
    volatile SIZE_T framesIn;
    volatile SIZE_T bytesIn;
    volatile SIZE_T keyFrames;
    volatile SIZE_T framesSent;
    volatile SIZE_T bytesSent;
    volatile SIZE_T waitKeyDrops;
    // 統計の前回値(統計スレッドだけが触る)
    SIZE_T prevBytesIn;
    SIZE_T prevBytesSent;
} CcStream;

typedef struct {
    BOOL used;
    INT32 stream;
    UINT64 addedAt; // offer を受けた時刻。セッション一覧に載る前に消さないため
    CHAR peerId[MAX_SIGNALING_CLIENT_ID_LEN + 1];
} CcPeer;

typedef struct {
    CcStream streams[CC_STREAM_COUNT];
    PCHAR rtspUser;
    PCHAR rtspPass;
    UINT32 latencyMs;
    UINT32 idleStopSec;
    UINT32 stallSec;
    UINT32 statsIntervalSec;
    MUTEX offerLock; // offer の処理(videoCodec の差し替え + セッションの作成)を 1 つずつにする
    MUTEX peersLock;
    CcPeer peers[CC_MAX_PEERS];
} CcState;

static CcState gCc;

static UINT32 envU32(PCHAR name, UINT32 def)
{
    PCHAR v = GETENV(name);
    UINT32 out;
    if (IS_NULL_OR_EMPTY_STRING(v) || STRTOUI32(v, NULL, 10, &out) != STATUS_SUCCESS) {
        return def;
    }
    return out;
}

static PCHAR envStr(PCHAR name)
{
    PCHAR v = GETENV(name);
    return IS_NULL_OR_EMPTY_STRING(v) ? NULL : v;
}

static PCHAR codecName(BOOL h265)
{
    return h265 ? "H265" : "H264";
}

static VOID emitJson(PCHAR fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
    fflush(stdout);
}

// JSON の文字列に入れても壊れないよう、peer ID の英数字と -_. 以外を _ にした写しを作る(ログ用)
static VOID safeId(PCHAR src, PCHAR dst, UINT32 len)
{
    UINT32 i;
    for (i = 0; i + 1 < len && src[i] != '\0'; ++i) {
        CHAR c = src[i];
        dst[i] = ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.') ? c : '_';
    }
    dst[i] = '\0';
}

/* ---------------------------------------------------------------------------------------------
 * 視聴者 → ストリームの対応
 * ------------------------------------------------------------------------------------------- */

static INT32 peerStreamLocked(PCHAR peerId)
{
    UINT32 i;
    for (i = 0; i < CC_MAX_PEERS; ++i) {
        if (gCc.peers[i].used && STRCMP(gCc.peers[i].peerId, peerId) == 0) {
            return gCc.peers[i].stream;
        }
    }
    return -1;
}

static VOID setPeerStream(PCHAR peerId, INT32 stream)
{
    UINT32 i;
    INT32 freeIdx = -1;
    MUTEX_LOCK(gCc.peersLock);
    for (i = 0; i < CC_MAX_PEERS; ++i) {
        if (gCc.peers[i].used && STRCMP(gCc.peers[i].peerId, peerId) == 0) {
            gCc.peers[i].stream = stream;
            MUTEX_UNLOCK(gCc.peersLock);
            return;
        }
        if (!gCc.peers[i].used && freeIdx < 0) {
            freeIdx = (INT32) i;
        }
    }
    if (freeIdx >= 0) {
        gCc.peers[freeIdx].used = TRUE;
        gCc.peers[freeIdx].stream = stream;
        gCc.peers[freeIdx].addedAt = GETTIME();
        STRNCPY(gCc.peers[freeIdx].peerId, peerId, MAX_SIGNALING_CLIENT_ID_LEN);
        gCc.peers[freeIdx].peerId[MAX_SIGNALING_CLIENT_ID_LEN] = '\0';
    } else {
        DLOGW("[cc] peer table is full");
    }
    MUTEX_UNLOCK(gCc.peersLock);
}

// 今のセッション一覧にない視聴者の対応を消し(offer の処理中のものは消さない)、ストリームごとの視聴者数を数える
static VOID countViewers(PSampleConfiguration pConf, UINT32 counts[CC_STREAM_COUNT], PUINT32 pTotal)
{
    UINT32 i, j;
    INT32 s;
    BOOL alive;
    PSampleStreamingSession pSession;

    counts[CC_STREAM_MAIN] = counts[CC_STREAM_SUB] = 0;
    *pTotal = 0;
    MUTEX_LOCK(pConf->streamingSessionListReadLock);
    MUTEX_LOCK(gCc.peersLock);
    for (i = 0; i < pConf->streamingSessionCount; ++i) {
        pSession = pConf->sampleStreamingSessionList[i];
        if (ATOMIC_LOAD_BOOL(&pSession->terminateFlag)) {
            continue;
        }
        (*pTotal)++;
        s = peerStreamLocked(pSession->peerId);
        if (s >= 0) {
            counts[s]++;
        }
    }
    for (j = 0; j < CC_MAX_PEERS; ++j) {
        if (!gCc.peers[j].used || GETTIME() - gCc.peers[j].addedAt < 30 * HUNDREDS_OF_NANOS_IN_A_SECOND) {
            continue;
        }
        alive = FALSE;
        for (i = 0; i < pConf->streamingSessionCount; ++i) {
            if (STRCMP(pConf->sampleStreamingSessionList[i]->peerId, gCc.peers[j].peerId) == 0) {
                alive = TRUE;
                break;
            }
        }
        if (!alive) {
            gCc.peers[j].used = FALSE;
        }
    }
    MUTEX_UNLOCK(gCc.peersLock);
    MUTEX_UNLOCK(pConf->streamingSessionListReadLock);
}

/* ---------------------------------------------------------------------------------------------
 * offer の SDP から、視聴者が受けたいコーデックの順を読む
 * ------------------------------------------------------------------------------------------- */

// m=video の並びでの H.265 / H.264 の最初の位置(なければ -1)
static VOID videoCodecOrder(PCHAR sdp, PINT32 pPosH265, PINT32 pPosH264)
{
    UINT32 pts[CC_MAX_PTS];
    UINT32 ptCount = 0, i, pt;
    BOOL inVideo = FALSE;
    PCHAR line = sdp, next, p;
    CHAR name[16];
    UINT32 n;

    *pPosH265 = *pPosH264 = -1;
    while (line != NULL && *line != '\0') {
        next = STRCHR(line, '\n');
        if (STRNCMP(line, "m=", 2) == 0) {
            inVideo = STRNCMP(line, "m=video ", 8) == 0;
            if (inVideo) {
                // m=video <port> <proto> <pt> <pt> ...
                ptCount = 0;
                p = line + 8;
                for (i = 0; i < 2 && p != NULL; ++i) {
                    p = STRCHR(p, ' ');
                    if (p != NULL) {
                        p++;
                    }
                }
                while (p != NULL && *p >= '0' && *p <= '9' && ptCount < CC_MAX_PTS) {
                    pts[ptCount++] = (UINT32) strtoul(p, &p, 10);
                    while (*p == ' ') {
                        p++;
                    }
                }
            }
        } else if (inVideo && STRNCMP(line, "a=rtpmap:", 9) == 0) {
            // a=rtpmap:<pt> <name>/<clock>
            pt = (UINT32) strtoul(line + 9, &p, 10);
            while (*p == ' ') {
                p++;
            }
            for (n = 0; n + 1 < SIZEOF(name) && p[n] != '\0' && p[n] != '/' && p[n] != '\r' && p[n] != '\n'; ++n) {
                name[n] = p[n];
            }
            name[n] = '\0';
            for (i = 0; i < ptCount; ++i) {
                if (pts[i] != pt) {
                    continue;
                }
                if ((STRCMPI(name, "H265") == 0 || STRCMPI(name, "HEVC") == 0) && (*pPosH265 < 0 || (INT32) i < *pPosH265)) {
                    *pPosH265 = (INT32) i;
                } else if (STRCMPI(name, "H264") == 0 && (*pPosH264 < 0 || (INT32) i < *pPosH264)) {
                    *pPosH264 = (INT32) i;
                }
            }
        }
        line = next == NULL ? NULL : next + 1;
    }
}

// コーデックを持つストリーム。H.265 は main を、H.264 は sub を優先する(なければ -1)
static INT32 streamWithCodec(BOOL h265)
{
    INT32 order[CC_STREAM_COUNT];
    UINT32 i;
    order[0] = h265 ? CC_STREAM_MAIN : CC_STREAM_SUB;
    order[1] = h265 ? CC_STREAM_SUB : CC_STREAM_MAIN;
    for (i = 0; i < CC_STREAM_COUNT; ++i) {
        CcStream* s = &gCc.streams[order[i]];
        if (s->rtspUrl != NULL && s->h265 == h265) {
            return order[i];
        }
    }
    return -1;
}

// 視聴者に送るストリームを決める。H.265 が H.264 より前(か H.265 だけ)なら H.265、そうでなければ H.264
static INT32 chooseStream(PSignalingMessage pMsg)
{
    PRtcSessionDescriptionInit pInit = NULL;
    INT32 posH265 = -1, posH264 = -1, s265, s264, chosen = -1;
    BOOL wantH265;

    pInit = (PRtcSessionDescriptionInit) MEMCALLOC(1, SIZEOF(RtcSessionDescriptionInit));
    if (pInit == NULL) {
        return -1;
    }
    if (STATUS_FAILED(deserializeSessionDescriptionInit(pMsg->payload, pMsg->payloadLen, pInit))) {
        DLOGW("[cc] failed to parse offer");
        SAFE_MEMFREE(pInit);
        return -1;
    }
    videoCodecOrder(pInit->sdp, &posH265, &posH264);
    SAFE_MEMFREE(pInit);

    s265 = streamWithCodec(TRUE);
    s264 = streamWithCodec(FALSE);
    wantH265 = posH265 >= 0 && (posH264 < 0 || posH265 < posH264);
    if (wantH265 && s265 >= 0) {
        chosen = s265;
    } else if (posH264 >= 0 && s264 >= 0) {
        chosen = s264;
    } else if (posH265 >= 0 && s265 >= 0) {
        chosen = s265;
    }
    DLOGI("[cc] offer from %s: pos(H265)=%d pos(H264)=%d -> %s", pMsg->peerClientId, posH265, posH264,
          chosen < 0 ? "none" : gCc.streams[chosen].name);
    return chosen;
}

/* ---------------------------------------------------------------------------------------------
 * シグナリングのコールバック(サンプル共通処理の前に挟む)
 * ------------------------------------------------------------------------------------------- */

static STATUS ccMessageReceived(UINT64 customData, PReceivedSignalingMessage pReceived)
{
    PSampleConfiguration pConf = (PSampleConfiguration) customData;
    PSignalingMessage pMsg = &pReceived->signalingMessage;
    STATUS status;
    INT32 stream, existing;
    CHAR peer[MAX_SIGNALING_CLIENT_ID_LEN + 1];

    if (pMsg->messageType != SIGNALING_MESSAGE_TYPE_OFFER) {
        return signalingMessageReceived(customData, pReceived);
    }

    safeId(pMsg->peerClientId, peer, SIZEOF(peer));
    MUTEX_LOCK(gCc.offerLock);
    // 同じ視聴者の再 offer(再交渉)は、今のトランシーバーのコーデックのまま
    MUTEX_LOCK(gCc.peersLock);
    existing = peerStreamLocked(pMsg->peerClientId);
    MUTEX_UNLOCK(gCc.peersLock);
    stream = existing >= 0 ? existing : chooseStream(pMsg);
    if (stream < 0) {
        MUTEX_UNLOCK(gCc.offerLock);
        emitJson("{\"cc_event\": \"reject\", \"peer\": \"%s\", \"reason\": \"no matching codec\"}", peer);
        return STATUS_SUCCESS;
    }
    setPeerStream(pMsg->peerClientId, stream);
    // サンプル共通処理は pConf->videoCodec で addSupportedCodec とトランシーバーを作る
    pConf->videoCodec = gCc.streams[stream].h265 ? RTC_CODEC_H265 : RTC_CODEC_H264_PROFILE_42E01F_LEVEL_ASYMMETRY_ALLOWED_PACKETIZATION_MODE;
    status = signalingMessageReceived(customData, pReceived);
    MUTEX_UNLOCK(gCc.offerLock);
    emitJson("{\"cc_event\": \"viewer\", \"peer\": \"%s\", \"stream\": \"%s\", \"codec\": \"%s\", \"ok\": %s}", peer, gCc.streams[stream].name,
             codecName(gCc.streams[stream].h265), STATUS_SUCCEEDED(status) ? "true" : "false");
    return status;
}

static STATUS ccStateChanged(UINT64 customData, SIGNALING_CLIENT_STATE state)
{
    PCHAR pStateStr = NULL;
    UNUSED_PARAM(customData);
    signalingClientGetStateString(state, &pStateStr);
    emitJson("{\"cc_event\": \"signaling\", \"state\": \"%s\", \"code\": %d}", pStateStr == NULL ? "" : pStateStr, (INT32) state);
    return STATUS_SUCCESS;
}

// サンプル共通処理の initSignaling と同じ。messageReceivedFn を ccMessageReceived にする(initSignaling は上書きするため)
static STATUS ccInitSignaling(PSampleConfiguration pConf)
{
    STATUS retStatus = STATUS_SUCCESS;
    pConf->signalingClientCallbacks.messageReceivedFn = ccMessageReceived;
    pConf->signalingClientCallbacks.stateChangeFn = ccStateChanged;
    STRCPY(pConf->clientInfo.clientId, SAMPLE_MASTER_CLIENT_ID);
    CHK_STATUS(createSignalingClientSync(&pConf->clientInfo, &pConf->channelInfo, &pConf->signalingClientCallbacks, pConf->pCredentialProvider,
                                         &pConf->signalingClientHandle));
    CHK_STATUS(signalingClientFetchSync(pConf->signalingClientHandle));
#ifdef ENABLE_DATA_CHANNEL
    pConf->onDataChannel = onDataChannel;
#endif
    CHK_STATUS(signalingClientConnectSync(pConf->signalingClientHandle));
    gSampleConfiguration = pConf;
CleanUp:
    return retStatus;
}

/* ---------------------------------------------------------------------------------------------
 * 映像(appsink → writeFrame)
 * ------------------------------------------------------------------------------------------- */

typedef struct {
    PSampleConfiguration pConf;
    INT32 stream;
} CcSinkCtx;

static CcSinkCtx gSinkCtx[CC_STREAM_COUNT];

static GstFlowReturn onNewSample(GstElement* sink, gpointer data)
{
    CcSinkCtx* ctx = (CcSinkCtx*) data;
    PSampleConfiguration pConf = ctx->pConf;
    CcStream* st = &gCc.streams[ctx->stream];
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(sink));
    GstBuffer* buffer;
    GstMapInfo info;
    GstClockTime pts;
    Frame frame;
    BOOL key;
    UINT32 i;
    INT32 s;
    STATUS status;
    PSampleStreamingSession pSession;

    if (sample == NULL) {
        return GST_FLOW_EOS;
    }
    buffer = gst_sample_get_buffer(sample);
    pts = gst_segment_to_running_time(gst_sample_get_segment(sample), GST_FORMAT_TIME, GST_BUFFER_PTS(buffer));
    if (!GST_CLOCK_TIME_IS_VALID(pts) || !gst_buffer_map(buffer, &info, GST_MAP_READ)) {
        gst_sample_unref(sample);
        return GST_FLOW_OK;
    }
    key = !GST_BUFFER_FLAG_IS_SET(buffer, GST_BUFFER_FLAG_DELTA_UNIT);
    ATOMIC_STORE(&st->lastSampleAt, (SIZE_T) GETTIME());
    ATOMIC_INCREMENT(&st->framesIn);
    ATOMIC_ADD(&st->bytesIn, info.size);
    if (key) {
        ATOMIC_INCREMENT(&st->keyFrames);
    }

    MEMSET(&frame, 0, SIZEOF(Frame));
    frame.version = FRAME_CURRENT_VERSION;
    frame.trackId = DEFAULT_VIDEO_TRACK_ID;
    frame.flags = key ? FRAME_FLAG_KEY_FRAME : FRAME_FLAG_NONE;
    frame.size = (UINT32) info.size;
    frame.frameData = (PBYTE) info.data;
    // RTP の時刻はカメラの PTS(100ns 単位)。サンプルは 25fps 固定で時刻を進めるが、カメラのフレーム間隔に合わせる
    frame.presentationTs = pts / DEFAULT_TIME_UNIT_IN_NANOS;
    frame.decodingTs = frame.presentationTs;

    MUTEX_LOCK(pConf->streamingSessionListReadLock);
    for (i = 0; i < pConf->streamingSessionCount; ++i) {
        pSession = pConf->sampleStreamingSessionList[i];
        MUTEX_LOCK(gCc.peersLock);
        s = peerStreamLocked(pSession->peerId);
        MUTEX_UNLOCK(gCc.peersLock);
        if (s != ctx->stream) {
            continue;
        }
        // 新しい視聴者には、最初のキーフレームから送る(途中の P フレームはデコードできない)
        if (ATOMIC_LOAD_BOOL(&pSession->firstFrame) && !key) {
            ATOMIC_INCREMENT(&st->waitKeyDrops);
            continue;
        }
        frame.index = (UINT32) ATOMIC_INCREMENT(&pSession->frameIndex);
        status = writeFrame(pSession->pVideoRtcRtpTransceiver, &frame);
        if (status == STATUS_SUCCESS) {
            ATOMIC_INCREMENT(&st->framesSent);
            ATOMIC_ADD(&st->bytesSent, info.size);
            if (ATOMIC_LOAD_BOOL(&pSession->firstFrame)) {
                PROFILE_WITH_START_TIME(pSession->offerReceiveTime, "Time to first frame");
                ATOMIC_STORE_BOOL(&pSession->firstFrame, FALSE);
            }
        }
    }
    MUTEX_UNLOCK(pConf->streamingSessionListReadLock);

    gst_buffer_unmap(buffer, &info);
    gst_sample_unref(sample);
    return ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag) ? GST_FLOW_EOS : GST_FLOW_OK;
}

static GstElement* buildPipeline(INT32 stream)
{
    CHAR desc[1024];
    GError* err = NULL;
    GstElement* pipeline;
    GstElement* el;
    CcStream* st = &gCc.streams[stream];
    PCHAR codec = st->h265 ? "h265" : "h264";

    SNPRINTF(desc, SIZEOF(desc),
             "rtspsrc name=src protocols=tcp latency=%u ! "
             "rtp%sdepay ! %sparse config-interval=-1 ! "
             "video/x-%s,stream-format=byte-stream,alignment=au ! "
             "appsink name=sink sync=false emit-signals=true max-buffers=60 drop=true",
             gCc.latencyMs, codec, codec, codec);
    pipeline = gst_parse_launch(desc, &err);
    if (err != NULL) {
        DLOGE("[cc] pipeline error: %s", err->message);
        g_clear_error(&err);
        if (pipeline != NULL) {
            gst_object_unref(pipeline);
        }
        return NULL;
    }
    // URL と認証情報は文字列の解析を通さずにプロパティで渡す(引用符などを含んでもよい)
    el = gst_bin_get_by_name(GST_BIN(pipeline), "src");
    g_object_set(el, "location", st->rtspUrl, NULL);
    if (gCc.rtspUser != NULL) {
        g_object_set(el, "user-id", gCc.rtspUser, "user-pw", gCc.rtspPass == NULL ? "" : gCc.rtspPass, NULL);
    }
    gst_object_unref(el);
    el = gst_bin_get_by_name(GST_BIN(pipeline), "sink");
    g_signal_connect(el, "new-sample", G_CALLBACK(onNewSample), (gpointer) &gSinkCtx[stream]);
    gst_object_unref(el);
    return pipeline;
}

static VOID stopPipeline(CcStream* st)
{
    if (st->pipeline != NULL) {
        gst_element_set_state(st->pipeline, GST_STATE_NULL);
        gst_object_unref(st->pipeline);
        st->pipeline = NULL;
    }
    st->startedAt = 0;
}

static VOID failPipeline(CcStream* st, UINT64 now, PCHAR why)
{
    DLOGW("[cc] %s: %s, retry in %us", st->name, why, st->backoffSec);
    emitJson("{\"cc_event\": \"rtsp_error\", \"stream\": \"%s\", \"message\": \"%s\", \"retry_in_s\": %u}", st->name, why, st->backoffSec);
    stopPipeline(st);
    st->restarts++;
    st->nextStartAt = now + (UINT64) st->backoffSec * HUNDREDS_OF_NANOS_IN_A_SECOND;
    st->backoffSec = MIN(st->backoffSec * 2, 30);
}

/*
 * パイプラインの管理(1 スレッド)。0.2 秒ごとにストリームごとの視聴者数を見て、
 * 視聴者がいれば RTSP を読み、いなくなって CC_IDLE_STOP_S 秒たったら閉じる。エラーやフレームの途絶はバックオフ付きでつなぎ直す。
 */
static PVOID pipelineManager(PVOID args)
{
    PSampleConfiguration pConf = (PSampleConfiguration) args;
    UINT32 counts[CC_STREAM_COUNT], total, i;
    UINT64 now, last;
    GstBus* bus;
    GstMessage* msg;
    GError* err;
    gchar* dbg;
    CHAR why[256];

    while (!ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag) && !ATOMIC_LOAD_BOOL(&pConf->interrupted)) {
        countViewers(pConf, counts, &total);
        now = GETTIME();
        for (i = 0; i < CC_STREAM_COUNT; ++i) {
            CcStream* st = &gCc.streams[i];
            if (st->rtspUrl == NULL) {
                continue;
            }
            if (counts[i] > 0) {
                st->idleSince = 0;
                if (st->pipeline == NULL && now >= st->nextStartAt) {
                    st->pipeline = buildPipeline((INT32) i);
                    if (st->pipeline == NULL || gst_element_set_state(st->pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
                        failPipeline(st, now, "failed to start pipeline");
                        continue;
                    }
                    st->startedAt = now;
                    ATOMIC_STORE(&st->lastSampleAt, (SIZE_T) now);
                    DLOGI("[cc] %s: RTSP started (%u viewers)", st->name, counts[i]);
                }
            } else if (st->pipeline != NULL) {
                if (st->idleSince == 0) {
                    st->idleSince = now;
                } else if (now - st->idleSince > (UINT64) gCc.idleStopSec * HUNDREDS_OF_NANOS_IN_A_SECOND) {
                    DLOGI("[cc] %s: no viewers for %us, stopping RTSP", st->name, gCc.idleStopSec);
                    stopPipeline(st);
                    st->idleSince = 0;
                }
            }
            if (st->pipeline == NULL) {
                continue;
            }
            // エラー / EOS
            bus = gst_element_get_bus(st->pipeline);
            msg = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR | GST_MESSAGE_EOS);
            gst_object_unref(bus);
            if (msg != NULL) {
                if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
                    err = NULL;
                    dbg = NULL;
                    gst_message_parse_error(msg, &err, &dbg);
                    SNPRINTF(why, SIZEOF(why), "GStreamer error: %s", err == NULL ? "?" : err->message);
                    DLOGE("[cc] %s: %s (%s)", st->name, why, dbg == NULL ? "" : dbg);
                    g_clear_error(&err);
                    g_free(dbg);
                } else {
                    SNPRINTF(why, SIZEOF(why), "RTSP EOS");
                }
                gst_message_unref(msg);
                // JSON に入れるので引用符を消す
                for (dbg = why; *dbg != '\0'; ++dbg) {
                    if (*dbg == '"' || *dbg == '\\' || *dbg < ' ') {
                        *dbg = '\'';
                    }
                }
                failPipeline(st, now, why);
                continue;
            }
            // 視聴者がいるのにフレームが来ない(カメラ側の固まり)
            last = (UINT64) ATOMIC_LOAD(&st->lastSampleAt);
            if (counts[i] > 0 && now > last && now - last > (UINT64) gCc.stallSec * HUNDREDS_OF_NANOS_IN_A_SECOND) {
                failPipeline(st, now, "no frames from RTSP");
                continue;
            }
            // 30 秒以上動いたらバックオフを戻す
            if (now - st->startedAt > 30 * HUNDREDS_OF_NANOS_IN_A_SECOND) {
                st->backoffSec = 1;
            }
        }
        THREAD_SLEEP(200 * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
    }
    for (i = 0; i < CC_STREAM_COUNT; ++i) {
        stopPipeline(&gCc.streams[i]);
    }
    return NULL;
}

// 統計を JSON 1 行で出す(送信量はアプリが writeFrame に渡した量で、RTP/SRTP/UDP のヘッダーは含まない)
static PVOID statsLoop(PVOID args)
{
    PSampleConfiguration pConf = (PSampleConfiguration) args;
    UINT32 interval = MAX(gCc.statsIntervalSec, 1), counts[CC_STREAM_COUNT], total, i, elapsed;
    SIZE_T bs, bi;
    DOUBLE inKbps[CC_STREAM_COUNT], sentKbps[CC_STREAM_COUNT];
    SIZE_T sentTotal;

    while (!ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
        // 終了をすぐ検知できるよう 0.5 秒ずつ眠る
        for (elapsed = 0; elapsed < interval * 2 && !ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag); ++elapsed) {
            THREAD_SLEEP(500 * HUNDREDS_OF_NANOS_IN_A_MILLISECOND);
        }
        if (ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
            break;
        }
        countViewers(pConf, counts, &total);
        sentTotal = 0;
        for (i = 0; i < CC_STREAM_COUNT; ++i) {
            CcStream* st = &gCc.streams[i];
            bs = ATOMIC_LOAD(&st->bytesSent);
            bi = ATOMIC_LOAD(&st->bytesIn);
            inKbps[i] = (bi - st->prevBytesIn) * 8.0 / 1000.0 / interval;
            sentKbps[i] = (bs - st->prevBytesSent) * 8.0 / 1000.0 / interval;
            st->prevBytesIn = bi;
            st->prevBytesSent = bs;
            sentTotal += bs;
        }
        printf("{\"cc_stats\": true, \"viewers\": %u, \"sent_kbps\": %.1f, \"sent_bytes\": %zu, \"streams\": {", total,
               sentKbps[0] + sentKbps[1], (size_t) sentTotal);
        for (i = 0; i < CC_STREAM_COUNT; ++i) {
            CcStream* st = &gCc.streams[i];
            printf("%s\"%s\": {\"codec\": \"%s\", \"available\": %s, \"running\": %s, \"viewers\": %u, \"in_kbps\": %.1f, \"sent_kbps\": %.1f, "
                   "\"frames_in\": %zu, \"key_frames\": %zu, \"frames_sent\": %zu, \"wait_key_drops\": %zu, \"restarts\": %u}",
                   i == 0 ? "" : ", ", st->name, codecName(st->h265), st->rtspUrl != NULL ? "true" : "false",
                   st->pipeline != NULL ? "true" : "false", counts[i], inKbps[i], sentKbps[i], (size_t) ATOMIC_LOAD(&st->framesIn),
                   (size_t) ATOMIC_LOAD(&st->keyFrames), (size_t) ATOMIC_LOAD(&st->framesSent), (size_t) ATOMIC_LOAD(&st->waitKeyDrops),
                   st->restarts);
        }
        printf("}}\n");
        fflush(stdout);
    }
    return NULL;
}

static VOID initStream(INT32 idx, PCHAR name, PCHAR urlEnv, PCHAR codecEnv, BOOL defH265)
{
    CcStream* st = &gCc.streams[idx];
    PCHAR c = envStr(codecEnv);
    st->name = name;
    st->rtspUrl = envStr(urlEnv);
    st->h265 = c == NULL ? defH265 : (STRCMPI(c, "h265") == 0 || STRCMPI(c, "hevc") == 0);
    st->backoffSec = 1;
    gSinkCtx[idx].stream = idx;
}

INT32 main(INT32 argc, CHAR* argv[])
{
    STATUS retStatus = STATUS_SUCCESS;
    PSampleConfiguration pConf = NULL;
    PCHAR pChannelName;
    TID statsTid = INVALID_TID_VALUE, managerTid = INVALID_TID_VALUE;
    UINT32 logLevel, i;
    INT32 first;

    setvbuf(stdout, NULL, _IOLBF, 0);
    logLevel = setLogLevel();
    signal(SIGINT, sigintHandler);
    signal(SIGTERM, sigintHandler);

    CHK_ERR((pChannelName = envStr("CC_WEBRTC_CHANNEL")) != NULL, STATUS_INVALID_OPERATION, "CC_WEBRTC_CHANNEL must be set");
    initStream(CC_STREAM_MAIN, "main", "CC_MAIN_RTSP_URL", "CC_MAIN_CODEC", TRUE);
    initStream(CC_STREAM_SUB, "sub", "CC_SUB_RTSP_URL", "CC_SUB_CODEC", FALSE);
    CHK_ERR(gCc.streams[CC_STREAM_MAIN].rtspUrl != NULL || gCc.streams[CC_STREAM_SUB].rtspUrl != NULL, STATUS_INVALID_OPERATION,
            "CC_MAIN_RTSP_URL or CC_SUB_RTSP_URL must be set");
    gCc.rtspUser = envStr("CC_RTSP_USER");
    gCc.rtspPass = envStr("CC_RTSP_PASS");
    gCc.latencyMs = envU32("CC_RTSP_LATENCY_MS", 0);
    gCc.idleStopSec = envU32("CC_IDLE_STOP_S", 10);
    gCc.stallSec = MAX(envU32("CC_STALL_TIMEOUT_S", 10), 2);
    gCc.statsIntervalSec = envU32("CC_STATS_INTERVAL_S", 10);
    gCc.offerLock = MUTEX_CREATE(FALSE);
    gCc.peersLock = MUTEX_CREATE(FALSE);

    // trickle ICE と TURN を有効にする(視聴者の NAT 越しに届かないときは TURN にフォールバック)
    CHK_STATUS(createSampleConfiguration(pChannelName, SIGNALING_CHANNEL_ROLE_TYPE_MASTER, TRUE, TRUE, logLevel, &pConf));
    for (i = 0; i < CC_STREAM_COUNT; ++i) {
        gSinkCtx[i].pConf = pConf;
    }
    first = gCc.streams[CC_STREAM_MAIN].rtspUrl != NULL ? CC_STREAM_MAIN : CC_STREAM_SUB;
    pConf->mediaType = SAMPLE_STREAMING_VIDEO_ONLY;
    // offer ごとに ccMessageReceived が差し替える
    pConf->videoCodec = gCc.streams[first].h265 ? RTC_CODEC_H265 : RTC_CODEC_H264_PROFILE_42E01F_LEVEL_ASYMMETRY_ALLOWED_PACKETIZATION_MODE;
    pConf->audioCodec = RTC_CODEC_OPUS;
    // 映像の送信はパイプライン管理のスレッドが行うので、サンプル共通処理の送信スレッドは使わない
    pConf->videoSource = NULL;
    pConf->audioSource = NULL;
    pConf->receiveAudioVideoSource = NULL;
    // 映像は送るだけ。音声は受けるだけ(送らないので帯域は使わない)
    pConf->addTransceiversCallback = addSendOnlyVideoRecvOnlyAudioTransceivers;
    // TWCC は帯域推定でエンコーダーのビットレートを変える仕組み。再エンコードしないので使わない
    pConf->enableTwcc = FALSE;
    pConf->customData = (UINT64) pConf;
    gst_init(&argc, &argv);

    DLOGI("[cc] channel=%s main=%s(%s) sub=%s(%s)", pChannelName, gCc.streams[0].rtspUrl == NULL ? "-" : gCc.streams[0].rtspUrl,
          codecName(gCc.streams[0].h265), gCc.streams[1].rtspUrl == NULL ? "-" : gCc.streams[1].rtspUrl, codecName(gCc.streams[1].h265));
    CHK_STATUS(initKvsWebRtc());
    CHK_STATUS(ccInitSignaling(pConf));
    emitJson("{\"cc_event\": \"ready\", \"channel\": \"%s\"}", pChannelName);
    THREAD_CREATE(&managerTid, pipelineManager, (PVOID) pConf);
    THREAD_CREATE(&statsTid, statsLoop, (PVOID) pConf);

    CHK_STATUS(sessionCleanupWait(pConf));

CleanUp:
    if (retStatus != STATUS_SUCCESS) {
        DLOGE("[cc] terminated with status 0x%08x", retStatus);
        emitJson("{\"cc_event\": \"exit\", \"status\": \"0x%08x\"}", retStatus);
    }
    if (pConf != NULL) {
        ATOMIC_STORE_BOOL(&pConf->appTerminateFlag, TRUE);
        if (managerTid != INVALID_TID_VALUE) {
            THREAD_JOIN(managerTid, NULL);
        }
        if (pConf->mediaSenderTid != INVALID_TID_VALUE) {
            THREAD_JOIN(pConf->mediaSenderTid, NULL);
        }
        if (statsTid != INVALID_TID_VALUE) {
            THREAD_JOIN(statsTid, NULL);
        }
        freeSignalingClient(&pConf->signalingClientHandle);
        freeSampleConfiguration(&pConf);
    }
    return STATUS_FAILED(retStatus) ? EXIT_FAILURE : EXIT_SUCCESS;
}

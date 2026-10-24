/*
 * cc_webrtc_master: カメラの RTSP を再エンコードせずに KVS WebRTC(シグナリングチャネルの master)へ流す。
 *
 * C SDK のサンプル共通処理(samples/common/Common.c: シグナリング、ピア接続、ICE、TWCC など)をそのまま使い、
 * 映像の取り出しだけを自前にしている。サンプルの kvsWebrtcClientMasterGstSample の rtspsrc は
 * uridecodebin ! x264enc で再エンコードするため使わない。
 *
 *   rtspsrc ! rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! appsink
 *   rtspsrc ! rtph265depay ! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au ! appsink
 *
 * 環境変数(entrypoint.sh が /certs/config.json から組み立てる):
 *   CC_WEBRTC_CHANNEL   シグナリングチャネル名(必須)
 *   CC_RTSP_URL         RTSP の URL(必須)
 *   CC_WEBRTC_CODEC     h264(既定)/ h265
 *   CC_RTSP_LATENCY_MS  rtspsrc の latency(既定 0)
 *   CC_IDLE_STOP_S      視聴者が 0 人になってから RTSP を止めるまでの秒数(既定 10)
 *   CC_STATS_INTERVAL_S 統計(JSON 1 行)を出す間隔(既定 10)
 *   CC_TIMESTAMP        pts(既定。カメラの PTS)/ arrival(エッジが受け取った時刻)
 *   KVS_ICE_TRANSPORT_POLICY=relay  TURN を強制(サンプル共通処理の機能)
 *   AWS_IOT_CORE_*      IoT 認証情報プロバイダー(IOT_CORE_ENABLE_CREDENTIALS でビルド)
 */
#include "../common/Samples.h"
#include <gst/gst.h>
#include <gst/app/gstappsink.h>

extern PSampleConfiguration gSampleConfiguration;

typedef struct {
    PCHAR rtspUrl;
    BOOL h265;
    UINT32 latencyMs;
    UINT32 idleStopSec;
    UINT32 statsIntervalSec;
    BOOL arrivalTs;               // TRUE: RTP の時刻に受け取った時刻を使う(CC_TIMESTAMP=arrival)
    volatile SIZE_T framesIn;     // RTSP から受け取ったフレーム数
    volatile SIZE_T bytesIn;      // 同バイト数(1 視聴者あたりの上り量の目安)
    volatile SIZE_T framesSent;   // writeFrame が成功した回数(視聴者数ぶん数える)
    volatile SIZE_T bytesSent;    // 同バイト数(RTP/SRTP のヘッダーは含まない)
    volatile SIZE_T keyFrames;
    volatile SIZE_T waitKeyDrops; // 新しい視聴者がキーフレームを待つ間に捨てたフレーム
    // 到着の揺らぎ: (受け取った時刻 − PTS) の区間内の最大 − 最小。ブラウザのジッタバッファが吸収すべき量の目安
    MUTEX jitterLock;
    INT64 offsetMin;
    INT64 offsetMax;
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

static GstFlowReturn onNewSample(GstElement* sink, gpointer data)
{
    PSampleConfiguration pConf = (PSampleConfiguration) data;
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(sink));
    GstBuffer* buffer;
    GstMapInfo info;
    GstClockTime pts;
    Frame frame;
    BOOL key;
    UINT32 i;
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

    {
        INT64 off = (INT64) GETTIME() - (INT64) (pts / DEFAULT_TIME_UNIT_IN_NANOS);
        MUTEX_LOCK(gCc.jitterLock);
        if (gCc.offsetMin == MAX_INT64 || off < gCc.offsetMin) {
            gCc.offsetMin = off;
        }
        if (gCc.offsetMax == MIN_INT64 || off > gCc.offsetMax) {
            gCc.offsetMax = off;
        }
        MUTEX_UNLOCK(gCc.jitterLock);
    }
    ATOMIC_INCREMENT(&gCc.framesIn);
    ATOMIC_ADD(&gCc.bytesIn, info.size);
    if (key) {
        ATOMIC_INCREMENT(&gCc.keyFrames);
    }

    MEMSET(&frame, 0, SIZEOF(Frame));
    frame.version = FRAME_CURRENT_VERSION;
    frame.trackId = DEFAULT_VIDEO_TRACK_ID;
    frame.flags = key ? FRAME_FLAG_KEY_FRAME : FRAME_FLAG_NONE;
    frame.size = (UINT32) info.size;
    frame.frameData = (PBYTE) info.data;
    // サンプルは 25fps 固定で時刻を進めるが、ここではカメラの PTS(100ns 単位)か、エッジが受け取った時刻を使う。
    // PTS はカメラのエンコード時刻なので映像はなめらかだが、RTSP の到着が揺らぐとブラウザのジッタバッファが伸びる。
    // 受け取った時刻にすると到着の揺らぎが RTP の時刻に吸収され、ジッタバッファが短くなる(そのぶん表示間隔は揺らぐ)
    frame.presentationTs = gCc.arrivalTs ? GETTIME() : pts / DEFAULT_TIME_UNIT_IN_NANOS;
    frame.decodingTs = frame.presentationTs;

    MUTEX_LOCK(pConf->streamingSessionListReadLock);
    for (i = 0; i < pConf->streamingSessionCount; ++i) {
        pSession = pConf->sampleStreamingSessionList[i];
        // 新しい視聴者には、最初のキーフレームから送る(途中の P フレームはデコードできない)
        if (ATOMIC_LOAD_BOOL(&pSession->firstFrame) && !key) {
            ATOMIC_INCREMENT(&gCc.waitKeyDrops);
            continue;
        }
        frame.index = (UINT32) ATOMIC_INCREMENT(&pSession->frameIndex);
        status = writeFrame(pSession->pVideoRtcRtpTransceiver, &frame);
        if (status == STATUS_SUCCESS) {
            ATOMIC_INCREMENT(&gCc.framesSent);
            ATOMIC_ADD(&gCc.bytesSent, info.size);
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

static GstElement* buildPipeline(PSampleConfiguration pConf)
{
    CHAR desc[2048];
    GError* err = NULL;
    GstElement* pipeline;
    GstElement* appsink;
    PCHAR codec = gCc.h265 ? "h265" : "h264";

    SNPRINTF(desc, SIZEOF(desc),
             "rtspsrc location=\"%s\" protocols=tcp latency=%u ! "
             "rtp%sdepay ! %sparse config-interval=-1 ! "
             "video/x-%s,stream-format=byte-stream,alignment=au ! "
             "appsink name=appsink-video sync=false emit-signals=true max-buffers=60 drop=true",
             gCc.rtspUrl, gCc.latencyMs, codec, codec, codec);
    DLOGI("[cc] pipeline: %s", desc);
    pipeline = gst_parse_launch(desc, &err);
    if (err != NULL) {
        DLOGE("[cc] pipeline error: %s", err->message);
        g_clear_error(&err);
        if (pipeline != NULL) {
            gst_object_unref(pipeline);
        }
        return NULL;
    }
    appsink = gst_bin_get_by_name(GST_BIN(pipeline), "appsink-video");
    g_signal_connect(appsink, "new-sample", G_CALLBACK(onNewSample), (gpointer) pConf);
    gst_object_unref(appsink);
    return pipeline;
}

/*
 * 映像の送信スレッド。最初の視聴者の offer で起動し(Common.c の mediaSenderRoutine)、
 * 視聴者が CC_IDLE_STOP_S 秒いなければ RTSP を止めて抜ける(次の視聴者で再び起動する)。
 * RTSP が切れたらバックオフ付きで張り直す。
 */
static PVOID sendRtspVideo(PVOID args)
{
    PSampleConfiguration pConf = (PSampleConfiguration) args;
    GstElement* pipeline = NULL;
    GstBus* bus;
    GstMessage* msg;
    GError* err = NULL;
    gchar* dbg = NULL;
    UINT64 idleSince = 0, now;
    UINT32 backoffSec = 1;
    BOOL stop = FALSE;

    while (!stop && !ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
        pipeline = buildPipeline(pConf);
        if (pipeline == NULL || gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
            DLOGE("[cc] failed to start pipeline");
        } else {
            DLOGI("[cc] RTSP pipeline started");
            bus = gst_element_get_bus(pipeline);
            for (;;) {
                msg = gst_bus_timed_pop_filtered(bus, 500 * GST_MSECOND, GST_MESSAGE_ERROR | GST_MESSAGE_EOS);
                if (msg != NULL) {
                    if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
                        gst_message_parse_error(msg, &err, &dbg);
                        DLOGE("[cc] GStreamer error: %s (%s)", err->message, dbg == NULL ? "" : dbg);
                        g_clear_error(&err);
                        g_free(dbg);
                        dbg = NULL;
                    } else {
                        DLOGW("[cc] RTSP EOS");
                    }
                    gst_message_unref(msg);
                    break;
                }
                backoffSec = 1;
                if (ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
                    stop = TRUE;
                    break;
                }
                now = GETTIME();
                if (pConf->streamingSessionCount == 0) {
                    if (idleSince == 0) {
                        idleSince = now;
                    } else if (now - idleSince > (UINT64) gCc.idleStopSec * HUNDREDS_OF_NANOS_IN_A_SECOND) {
                        DLOGI("[cc] no viewers for %us, stopping RTSP", gCc.idleStopSec);
                        stop = TRUE;
                        break;
                    }
                } else {
                    idleSince = 0;
                }
            }
            gst_object_unref(bus);
        }
        if (pipeline != NULL) {
            gst_element_set_state(pipeline, GST_STATE_NULL);
            gst_object_unref(pipeline);
            pipeline = NULL;
        }
        if (!stop && !ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
            THREAD_SLEEP((UINT64) backoffSec * HUNDREDS_OF_NANOS_IN_A_SECOND);
            backoffSec = MIN(backoffSec * 2, 30);
        }
    }
    // 次の offer で mediaSenderRoutine から再び起動できるようにする
    ATOMIC_STORE_BOOL(&pConf->connected, FALSE);
    return NULL;
}

// 統計を JSON 1 行で出す(計測用。sendrate はアプリが writeFrame に渡した量で、RTP/SRTP/UDP のヘッダーは含まない)
static PVOID statsLoop(PVOID args)
{
    PSampleConfiguration pConf = (PSampleConfiguration) args;
    SIZE_T prevBytesSent = 0, prevBytesIn = 0, bs, bi;
    DOUBLE spreadMs;
    UINT32 interval = MAX(gCc.statsIntervalSec, 1);
    while (!ATOMIC_LOAD_BOOL(&pConf->appTerminateFlag)) {
        THREAD_SLEEP((UINT64) interval * HUNDREDS_OF_NANOS_IN_A_SECOND);
        bs = ATOMIC_LOAD(&gCc.bytesSent);
        bi = ATOMIC_LOAD(&gCc.bytesIn);
        MUTEX_LOCK(gCc.jitterLock);
        spreadMs = gCc.offsetMin == MAX_INT64 ? 0.0 : (gCc.offsetMax - gCc.offsetMin) / (DOUBLE) HUNDREDS_OF_NANOS_IN_A_MILLISECOND;
        gCc.offsetMin = MAX_INT64;
        gCc.offsetMax = MIN_INT64;
        MUTEX_UNLOCK(gCc.jitterLock);
        printf("{\"cc_stats\": true, \"ts_ms\": %" PRIu64 ", \"viewers\": %u, \"frames_in\": %zu, \"key_frames\": %zu, "
               "\"frames_sent\": %zu, \"wait_key_drops\": %zu, \"in_kbps\": %.1f, \"sent_kbps\": %.1f, \"arrival_spread_ms\": %.1f}\n",
               GETTIME() / HUNDREDS_OF_NANOS_IN_A_MILLISECOND, pConf->streamingSessionCount, (size_t) ATOMIC_LOAD(&gCc.framesIn),
               (size_t) ATOMIC_LOAD(&gCc.keyFrames), (size_t) ATOMIC_LOAD(&gCc.framesSent), (size_t) ATOMIC_LOAD(&gCc.waitKeyDrops),
               (bi - prevBytesIn) * 8.0 / 1000.0 / interval, (bs - prevBytesSent) * 8.0 / 1000.0 / interval, spreadMs);
        fflush(stdout);
        prevBytesSent = bs;
        prevBytesIn = bi;
    }
    return NULL;
}

INT32 main(INT32 argc, CHAR* argv[])
{
    STATUS retStatus = STATUS_SUCCESS;
    PSampleConfiguration pConf = NULL;
    PCHAR pChannelName, pCodec;
    TID statsTid = INVALID_TID_VALUE;
    UINT32 logLevel;

    setvbuf(stdout, NULL, _IOLBF, 0);
    logLevel = setLogLevel();
    signal(SIGINT, sigintHandler);
    signal(SIGTERM, sigintHandler);

    CHK_ERR((pChannelName = GETENV("CC_WEBRTC_CHANNEL")) != NULL, STATUS_INVALID_OPERATION, "CC_WEBRTC_CHANNEL must be set");
    CHK_ERR((gCc.rtspUrl = GETENV("CC_RTSP_URL")) != NULL, STATUS_INVALID_OPERATION, "CC_RTSP_URL must be set");
    pCodec = GETENV("CC_WEBRTC_CODEC");
    gCc.h265 = pCodec != NULL && STRCMPI(pCodec, "h265") == 0;
    gCc.latencyMs = envU32("CC_RTSP_LATENCY_MS", 0);
    gCc.idleStopSec = envU32("CC_IDLE_STOP_S", 10);
    gCc.statsIntervalSec = envU32("CC_STATS_INTERVAL_S", 10);
    gCc.jitterLock = MUTEX_CREATE(FALSE);
    gCc.offsetMin = MAX_INT64;
    gCc.offsetMax = MIN_INT64;
    gCc.arrivalTs = GETENV("CC_TIMESTAMP") != NULL && STRCMPI(GETENV("CC_TIMESTAMP"), "arrival") == 0;

    // trickle ICE と TURN を有効にする(視聴者の NAT 越しに届かないときは TURN にフォールバック)
    CHK_STATUS(createSampleConfiguration(pChannelName, SIGNALING_CHANNEL_ROLE_TYPE_MASTER, TRUE, TRUE, logLevel, &pConf));
    pConf->mediaType = SAMPLE_STREAMING_VIDEO_ONLY;
    pConf->videoCodec = gCc.h265 ? RTC_CODEC_H265 : RTC_CODEC_H264_PROFILE_42E01F_LEVEL_ASYMMETRY_ALLOWED_PACKETIZATION_MODE;
    pConf->audioCodec = RTC_CODEC_OPUS;
    pConf->videoSource = sendRtspVideo;
    pConf->audioSource = NULL;
    pConf->receiveAudioVideoSource = NULL;
    // 映像は送るだけ。音声は受けるだけ(将来の双方向音声の余地。送らないので帯域は使わない)
    pConf->addTransceiversCallback = addSendOnlyVideoRecvOnlyAudioTransceivers;
    // TWCC は帯域推定でエンコーダーのビットレートを変える仕組み。再エンコードしないので使わない
    pConf->enableTwcc = FALSE;
    pConf->customData = (UINT64) pConf;
#ifdef ENABLE_DATA_CHANNEL
    pConf->onDataChannel = onDataChannel;
#endif
    gst_init(&argc, &argv);

    DLOGI("[cc] channel=%s codec=%s rtsp=%s", pChannelName, gCc.h265 ? "h265" : "h264", gCc.rtspUrl);
    CHK_STATUS(initKvsWebRtc());
    CHK_STATUS(initSignaling(pConf, SAMPLE_MASTER_CLIENT_ID));
    DLOGI("[cc] signaling connected, waiting for viewers");
    THREAD_CREATE(&statsTid, statsLoop, (PVOID) pConf);

    CHK_STATUS(sessionCleanupWait(pConf));

CleanUp:
    if (retStatus != STATUS_SUCCESS) {
        DLOGE("[cc] terminated with status 0x%08x", retStatus);
    }
    if (pConf != NULL) {
        ATOMIC_STORE_BOOL(&pConf->appTerminateFlag, TRUE);
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

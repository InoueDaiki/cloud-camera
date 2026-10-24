# syntax=docker/dockerfile:1
# エッジの KVS WebRTC master(サイドカー)。linux/arm64(Raspberry Pi 4、Apple Silicon の Docker)と linux/amd64 に対応。
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t cc-webrtc edge/webrtc/
#
# ステージ:
#   build    … amazon-kinesis-video-streams-webrtc-sdk-c をタグ固定でビルドし、自前の master(src/)をサンプル共通処理と一緒にリンク
#   plugins  … 必要な GStreamer プラグインの .so だけを集める(edge/Dockerfile と同じ方針)
#   runtime  … 最小限の共有ライブラリ + master

ARG DEBIAN_IMAGE=debian:trixie-slim
# WebRTC C SDK のタグ(固定)。H.265 の送信は v1.10.2 から、H.265 の RTX は v1.20.0 から
ARG KVS_WEBRTC_TAG=v1.20.0

############################################################
FROM ${DEBIAN_IMAGE} AS build
ARG KVS_WEBRTC_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git cmake build-essential pkg-config autoconf automake libtool \
      libssl-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${KVS_WEBRTC_TAG} \
      https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c.git kvs
# 自前の master をサンプルのディレクトリに置き、サンプル共通処理(common/Common.c)と一緒にビルドする。
# SDK 本体を先にビルドしてキャッシュし、src/ を変えたときは master だけをビルドし直す(空のファイルで構成してから差し替える)
RUN mkdir -p /src/kvs/samples/cc && touch /src/kvs/samples/cc/cc_webrtc_master.c \
    && printf '%s\n' \
      'if(GST_FOUND)' \
      '  add_executable(ccWebrtcMaster common/Common.c cc/cc_webrtc_master.c)' \
      '  target_link_libraries(ccWebrtcMaster kvsWebrtcClient kvsWebrtcSignalingClient ${EXTRA_DEPS} ${GST_SAMPLE_LIBRARIES} kvsCommonLws kvspicUtils websockets)' \
      'endif()' >> /src/kvs/samples/CMakeLists.txt
WORKDIR /src/kvs/build
# OpenSSL はディストリのもの(3.x)。libwebsockets / libsrtp / usrsctp は SDK が固定した版をソースからビルドする。
# IOT_CORE_ENABLE_CREDENTIALS: サンプル共通処理が AWS_IOT_CORE_* から IoT 認証情報プロバイダーを使う
RUN cmake .. -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_DEPENDENCIES=OFF -DBUILD_WEBSOCKETS=ON -DBUILD_SRTP=ON -DBUILD_USRSCTP=ON \
      -DIOT_CORE_ENABLE_CREDENTIALS=ON -DBUILD_SAMPLE=ON -DENABLE_DATA_CHANNEL=ON \
    && make -j"$(nproc)" kvsWebrtcClient kvsWebrtcSignalingClient
COPY src/cc_webrtc_master.c /src/kvs/samples/cc/cc_webrtc_master.c
RUN make -j"$(nproc)" ccWebrtcMaster \
    && mkdir -p /out/bin /out/lib \
    && cp samples/ccWebrtcMaster /out/bin/ \
    && find /src/kvs/build /src/kvs/open-source -name '*.so*' \( -type f -o -type l \) -exec cp -a {} /out/lib/ \; \
    && cp /src/kvs/certs/cert.pem /out/cert.pem \
    && { strip --strip-unneeded /out/bin/* /out/lib/*.so* 2>/dev/null || true; } \
    && ls -la /out/bin /out/lib

############################################################
FROM ${DEBIAN_IMAGE} AS plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    && rm -rf /var/lib/apt/lists/*
# rtspsrc(rtsp)、rtph264depay / rtph265depay(rtp)、rtpbin(rtpmanager)、udpsrc(udp)、h264parse / h265parse(videoparsersbad)、appsink(app)
RUN mkdir -p /out/gst /out/lib \
    && d="$(dirname "$(find /usr/lib -name libgstcoreelements.so | head -1)")" \
    && for p in rtsp rtp rtpmanager udp videoparsersbad app; do cp "$d/libgst$p.so" /out/gst/; done \
    && cp -a /usr/lib/*-linux-gnu/libgstcodecparsers-1.0.so.0* /out/lib/ \
    && ls -la /out/gst /out/lib

############################################################
FROM ${DEBIAN_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tini jq libssl3t64 \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-tools \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

ENV LD_LIBRARY_PATH=/opt/kvs/lib \
    GST_PLUGIN_PATH=/opt/kvs/gst \
    GST_REGISTRY=/tmp/gst-registry.bin \
    AWS_KVS_CACERT_PATH=/opt/kvs/cert.pem \
    AWS_KVS_LOG_LEVEL=3

COPY --from=build /out/lib/ /opt/kvs/lib/
COPY --from=build /out/bin/ /opt/kvs/bin/
COPY --from=build /out/cert.pem /opt/kvs/cert.pem
COPY --from=plugins /out/gst/ /opt/kvs/gst/
COPY --from=plugins /out/lib/ /opt/kvs/lib/
COPY entrypoint.sh /opt/kvs/entrypoint.sh

# ビルド時の検査: プラグインと共有ライブラリが揃っていること
RUN for e in rtspsrc rtpbin rtph264depay rtph265depay h264parse h265parse appsink; do \
      gst-inspect-1.0 "$e" >/dev/null || { echo "missing $e"; exit 1; }; done \
    && ! ldd /opt/kvs/bin/ccWebrtcMaster | grep 'not found' \
    && rm -f /tmp/gst-registry.bin

WORKDIR /tmp
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/kvs/entrypoint.sh"]

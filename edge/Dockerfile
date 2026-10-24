# syntax=docker/dockerfile:1
# エッジゲートウェイのコンテナ。linux/arm64(Raspberry Pi 4、Apple Silicon の Docker)と linux/amd64 に対応。
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t cc-edge edge/
#
# ステージ:
#   kvssink-build  … amazon-kinesis-video-streams-producer-sdk-cpp の kvssink プラグインをビルド
#   webrtc-build   … amazon-kinesis-video-streams-webrtc-sdk-c をビルドし、KVS WebRTC の master(webrtc/src/)をリンク
#   gst-plugins    … 必要な GStreamer プラグインの .so だけを集める(plugins-good/bad 一式は入れない。
#                    bad のライブラリは GL や mesa を引き込むため libgstcodecparsers だけを持ってくる)
#   py-deps        … awsiotsdk を /out/py に入れる(実行イメージに pip と venv を入れない)
#   runtime        … 最小限の共有ライブラリ + Python エージェント

ARG DEBIAN_IMAGE=debian:trixie-slim
# Producer SDK のタグ(固定)。producer-c は SDK 側の CMake が v1.6.2 に固定している
ARG KVS_SDK_TAG=v3.6.0
# WebRTC C SDK のタグ(固定)。H.265 の送信は v1.10.2 から、H.265 の RTX は v1.20.0 から(docs/webrtc.md)
ARG KVS_WEBRTC_TAG=v1.20.0

############################################################
FROM ${DEBIAN_IMAGE} AS kvssink-build
ARG KVS_SDK_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git cmake m4 build-essential pkg-config \
      libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${KVS_SDK_TAG} \
      https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git kvs
WORKDIR /src/kvs/build
# 依存はディストリのパッケージを使う(BUILD_DEPENDENCIES=OFF)。producer-c だけ CMake が取得する。
# GCC 14 では暗黙の関数宣言がエラーになるため、pthread_getname_np 用に _GNU_SOURCE を定義する
RUN cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=OFF -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_FLAGS="-D_GNU_SOURCE -Wno-error=implicit-function-declaration -Wno-error=incompatible-pointer-types" \
    && make -j"$(nproc)" gstkvssink \
    && mkdir -p /out/lib /out/gst \
    && cp libgstkvssink.so /out/gst/ \
    && find . -name 'libKinesisVideoProducer.so*' -exec cp -a {} /out/lib/ \; \
    && find . -name 'libcproducer.so*' -exec cp -a {} /out/lib/ \; \
    && find . -name 'libkvsCommonCurl.so*' -exec cp -a {} /out/lib/ \; \
    && find . -name 'libkvsCommonLws.so*' -exec cp -a {} /out/lib/ \; \
    && strip --strip-unneeded /out/gst/*.so /out/lib/*.so* || true \
    && ls -la /out/lib /out/gst

############################################################
FROM ${DEBIAN_IMAGE} AS webrtc-build
ARG KVS_WEBRTC_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git cmake build-essential pkg-config autoconf automake libtool \
      libssl-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${KVS_WEBRTC_TAG} \
      https://github.com/awslabs/amazon-kinesis-video-streams-webrtc-sdk-c.git kvs
# 自前の master をサンプルのディレクトリに置き、サンプル共通処理(common/Common.c)と一緒にビルドする。
# SDK 本体を先にビルドしてキャッシュし、webrtc/src/ を変えたときは master だけをビルドし直す(空のファイルで構成してから差し替える)
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
COPY webrtc/src/cc_webrtc_master.c /src/kvs/samples/cc/cc_webrtc_master.c
# kvssink(producer-c)の libkvsCommonLws.so と同じ名前の別の版を含むので、/opt/kvs/webrtc/lib に分けて置く
RUN make -j"$(nproc)" ccWebrtcMaster \
    && mkdir -p /out/bin /out/lib \
    && cp samples/ccWebrtcMaster /out/bin/ \
    && find /src/kvs/build /src/kvs/open-source -name '*.so*' \( -type f -o -type l \) -exec cp -a {} /out/lib/ \; \
    && cp /src/kvs/certs/cert.pem /out/cert.pem \
    && { strip --strip-unneeded /out/bin/* /out/lib/*.so* 2>/dev/null || true; } \
    && ls -la /out/bin /out/lib

############################################################
FROM ${DEBIAN_IMAGE} AS gst-plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    && rm -rf /var/lib/apt/lists/*
# rtspsrc(rtsp)、rtph265depay(rtp)、rtpbin(rtpmanager)、udpsrc(udp)、h265parse(videoparsersbad)、
# h265timestamper(codectimestamper。RTP には DTS がないため推定して付ける)、ドライランのファイル出力用 matroskamux(matroska)、
# WebRTC の master が映像を取り出す appsink(app)
RUN mkdir -p /out/gst \
    && d="$(pkg-config --variable=pluginsdir gstreamer-1.0 2>/dev/null || dirname "$(find /usr/lib -name libgstcoreelements.so | head -1)")" \
    && for p in rtsp rtp rtpmanager udp videoparsersbad codectimestamper matroska app; do cp "$d/libgst$p.so" /out/gst/; done \
    && mkdir -p /out/lib && cp -a /usr/lib/*-linux-gnu/libgstcodecparsers-1.0.so.0* /out/lib/ \
    && ls -la /out/gst /out/lib

############################################################
FROM ${DEBIAN_IMAGE} AS py-deps
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY agent/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /tmp/venv \
    && /tmp/venv/bin/pip install --no-cache-dir --no-compile --target /out/py -r /tmp/requirements.txt

############################################################
FROM ${DEBIAN_IMAGE} AS runtime
# 実行時に必要な共有ライブラリだけを入れる
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates tini \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
      gstreamer1.0-tools \
      libcurl4t64 liblog4cplus-2.0.5t64 libssl3t64 \
      python3 python3-gi gir1.2-gstreamer-1.0 \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/*

ENV KVS_LIB_DIR=/opt/kvs/lib \
    GST_PLUGIN_PATH=/opt/kvs/gst \
    LD_LIBRARY_PATH=/opt/kvs/lib \
    GST_REGISTRY=/tmp/gst-registry.bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/agent:/opt/py

COPY --from=kvssink-build /out/lib/ /opt/kvs/lib/
COPY --from=kvssink-build /out/gst/ /opt/kvs/gst/
COPY --from=gst-plugins /out/gst/ /opt/kvs/gst/
COPY --from=gst-plugins /out/lib/ /opt/kvs/lib/
COPY --from=py-deps /out/py/ /opt/py/
COPY kvs_log_configuration /opt/kvs/kvs_log_configuration
# KVS WebRTC の master(カメラごとにエージェントが子プロセスとして起動する。LD_LIBRARY_PATH は起動時に /opt/kvs/webrtc/lib にする)
COPY --from=webrtc-build /out/bin/ /opt/kvs/webrtc/bin/
COPY --from=webrtc-build /out/lib/ /opt/kvs/webrtc/lib/
COPY --from=webrtc-build /out/cert.pem /opt/kvs/webrtc/cert.pem

# Python エージェント(PyGObject はディストリのもの、awsiotsdk は py-deps ステージで入れたもの)
COPY agent/cc_edge /opt/agent/cc_edge

# 起動時にプラグインが読めるかを確かめる(ビルド時の検査)
RUN for e in kvssink rtspsrc rtpbin udpsrc rtph265depay rtph264depay h265parse h264parse h265timestamper h264timestamper matroskamux fakesink filesink appsink; do \
      gst-inspect-1.0 "$e" >/dev/null || { echo "missing $e"; exit 1; }; done \
    && ! LD_LIBRARY_PATH=/opt/kvs/webrtc/lib:/opt/kvs/lib ldd /opt/kvs/webrtc/bin/ccWebrtcMaster | grep 'not found' \
    && python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; import awsiot, awscrt" \
    && rm -f /tmp/gst-registry.bin

WORKDIR /var/lib/cc-edge
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "cc_edge"]

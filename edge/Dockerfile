# syntax=docker/dockerfile:1
# エッジゲートウェイのコンテナ。linux/arm64(Raspberry Pi 4、Apple Silicon の Docker)と linux/amd64 に対応。
#
#   docker buildx build --platform linux/arm64,linux/amd64 -t cc-edge edge/
#
# ステージ:
#   kvssink-build  … amazon-kinesis-video-streams-producer-sdk-cpp の kvssink プラグインをビルド
#   gst-plugins    … 必要な GStreamer プラグインの .so だけを集める(plugins-good/bad 一式は入れない。
#                    bad のライブラリは GL や mesa を引き込むため libgstcodecparsers だけを持ってくる)
#   py-deps        … awsiotsdk を /out/py に入れる(実行イメージに pip と venv を入れない)
#   runtime        … 最小限の共有ライブラリ + Python エージェント

ARG DEBIAN_IMAGE=debian:trixie-slim
# Producer SDK のタグ(固定)。producer-c は SDK 側の CMake が v1.6.2 に固定している
ARG KVS_SDK_TAG=v3.6.0

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
FROM ${DEBIAN_IMAGE} AS gst-plugins
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    && rm -rf /var/lib/apt/lists/*
# rtspsrc(rtsp)、rtph265depay(rtp)、rtpbin(rtpmanager)、udpsrc(udp)、h265parse(videoparsersbad)、
# h265timestamper(codectimestamper。RTP には DTS がないため推定して付ける)、ドライランのファイル出力用 matroskamux(matroska)
RUN mkdir -p /out/gst \
    && d="$(pkg-config --variable=pluginsdir gstreamer-1.0 2>/dev/null || dirname "$(find /usr/lib -name libgstcoreelements.so | head -1)")" \
    && for p in rtsp rtp rtpmanager udp videoparsersbad codectimestamper matroska; do cp "$d/libgst$p.so" /out/gst/; done \
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

# Python エージェント(PyGObject はディストリのもの、awsiotsdk は py-deps ステージで入れたもの)
COPY agent/cc_edge /opt/agent/cc_edge

# 起動時にプラグインが読めるかを確かめる(ビルド時の検査)
RUN for e in kvssink rtspsrc rtpbin udpsrc rtph265depay rtph264depay h265parse h264parse h265timestamper h264timestamper matroskamux fakesink filesink; do \
      gst-inspect-1.0 "$e" >/dev/null || { echo "missing $e"; exit 1; }; done \
    && python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; import awsiot, awscrt" \
    && rm -f /tmp/gst-registry.bin

WORKDIR /var/lib/cc-edge
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "cc_edge"]

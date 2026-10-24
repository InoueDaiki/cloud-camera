# 検証用: テストパターンを H.265 にエンコードして kvssink で KVS に送るコンテナ。
# エッジ(edge/)とは独立した、動画退避 Lambda の検証専用の道具。ffmpeg / ffprobe も入れておく。
FROM ubuntu:24.04

ARG SDK_VERSION=v3.6.0
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git pkg-config ca-certificates curl \
      libssl-dev libcurl4-openssl-dev liblog4cplus-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      ffmpeg python3 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${SDK_VERSION} https://github.com/awslabs/amazon-kinesis-video-streams-producer-sdk-cpp.git /sdk \
    && mkdir -p /sdk/build && cd /sdk/build \
    && cmake .. -DBUILD_GSTREAMER_PLUGIN=ON -DBUILD_DEPENDENCIES=OFF -DBUILD_TEST=OFF -DALIGNED_MEMORY_MODEL=ON \
    && make -j"$(nproc)" \
    && cp /sdk/build/libgstkvssink.so /usr/lib/$(uname -m)-linux-gnu/gstreamer-1.0/ \
    && cp -P /sdk/build/*.so* /usr/local/lib/ 2>/dev/null; ldconfig; true

# clockoverlay(時刻の焼き込み)は pango プラグイン
RUN apt-get update && apt-get install -y --no-install-recommends gstreamer1.0-x fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV GST_PLUGIN_PATH=/sdk/build
ENV LD_LIBRARY_PATH=/sdk/build:/usr/local/lib
COPY send.sh /send.sh
ENTRYPOINT ["/bin/bash", "/send.sh"]

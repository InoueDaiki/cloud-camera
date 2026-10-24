# コーデック比較の測定用イメージ(docs/codec.md)。
# Debian trixie の libx264 / libx265(エミュレーターのイメージと同じ版)に libvmaf を足して FFmpeg をビルドする。
# Debian には libvmaf のパッケージがなく、ffmpeg パッケージも libvmaf なしでビルドされているため、どちらもソースから作る。
#   docker build -t cc-codec-vmaf:1 tools/codec
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential nasm pkg-config curl ca-certificates xz-utils \
      meson ninja-build xxd libx264-dev libx265-dev libfreetype-dev libharfbuzz-dev libfontconfig-dev fonts-dejavu-core python3 \
 && rm -rf /var/lib/apt/lists/*
ARG VMAF_VERSION=3.0.0
RUN curl -fsSL https://github.com/Netflix/vmaf/archive/refs/tags/v${VMAF_VERSION}.tar.gz | tar -xz -C /tmp \
 && cd /tmp/vmaf-${VMAF_VERSION}/libvmaf \
 && meson setup build --buildtype=release --prefix=/usr/local -Denable_tests=false -Denable_docs=false \
 && ninja -C build install && ldconfig && rm -rf /tmp/vmaf-*
ARG FFMPEG_VERSION=7.1.1
RUN curl -fsSL https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz | tar -xJ -C /tmp \
 && cd /tmp/ffmpeg-${FFMPEG_VERSION} \
 && ./configure --prefix=/usr/local --enable-gpl --enable-version3 --enable-libx264 --enable-libx265 --enable-libvmaf \
      --enable-libfreetype --enable-libharfbuzz --enable-libfontconfig --disable-doc --disable-ffplay \
 && make -j"$(nproc)" && make install && ldconfig && cd / && rm -rf /tmp/ffmpeg-*
WORKDIR /work
ENTRYPOINT ["python3", "/opt/codec/measure.py"]

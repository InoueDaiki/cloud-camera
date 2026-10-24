# 最小構成の FFmpeg(LGPL 2.1+。HEVC / H.264 のデコーダ、mov デマクサー、swscale だけ)と、それにリンクした PyAV を
# Lambda の Python 3.13(arm64、Amazon Linux 2023)と同じイメージでビルドする。出力は /out:
#   /out/av/、/out/av-*.dist-info/  → zip の直下
#   /out/lib/*.so.N                 → zip の lib/(Lambda の LD_LIBRARY_PATH に /var/task/lib が入っている)
# PyPI の av の wheel は libx264 / libx265(GPL)などを同梱して 90 MB あるが、これは展開後 約 16 MB。
FROM public.ecr.aws/lambda/python:3.13-arm64
ARG FFMPEG_VERSION=8.1
ARG AV_VERSION=19.0.2
RUN dnf install -y gcc gcc-c++ make pkgconf-pkg-config tar xz findutils binutils >/dev/null
WORKDIR /build
RUN curl -sSfL https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz | tar xJ
# --disable-autodetect で外部ライブラリ(GPL のものを含む)を一切リンクしない。--enable-gpl も付けない
RUN cd ffmpeg-${FFMPEG_VERSION} && ./configure --prefix=/opt/ff --enable-shared --disable-static \
      --disable-programs --disable-doc --disable-everything --disable-network --disable-autodetect \
      --enable-decoder=hevc,h264 --enable-parser=hevc,h264 --enable-demuxer=mov --enable-protocol=file,pipe \
      --enable-swscale \
    && make -j"$(nproc)" >/dev/null && make install >/dev/null
RUN strip --strip-unneeded /opt/ff/lib/*.so.*
ENV PKG_CONFIG_PATH=/opt/ff/lib/pkgconfig LD_LIBRARY_PATH=/opt/ff/lib
RUN pip install -q cython setuptools wheel && \
    pip wheel -q --no-binary av --no-build-isolation av==${AV_VERSION} -w /wheels
# シンボリックリンクは zip で実体の複製になるので、SONAME(libavcodec.so.62 など)の実体だけを置く
RUN mkdir -p /out/lib && cd /out && python -m zipfile -e /wheels/av-*.whl . && \
    for f in /opt/ff/lib/lib*.so.*; do case "$f" in *.so.*.*.*) ;; *) cp -L "$f" lib/ ;; esac; done && \
    find av -name '*.so' -exec strip --strip-unneeded {} \; && \
    find av -type f \( -name '*.pyi' -o -name '*.pyx' -o -name '*.pxd' \) -delete && \
    strings lib/libavcodec.so.* | grep -m1 "license:" && du -sh /out/*

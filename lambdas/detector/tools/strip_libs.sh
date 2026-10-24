#!/bin/sh
# build.sh から docker(Amazon Linux 2023)の中で呼ぶ。ステージ /s の共有ライブラリのデバッグ情報を削る。
# onnxruntime は配布時点で strip 済み。numpy / Pillow の同梱ライブラリ(*.libs)は auditwheel の
# patchelf 済みで、strip すると読み込めなくなる(page-aligned エラー)ので pyarrow だけにする。
set -eu
dnf install -q -y binutils findutils >/dev/null 2>&1
cd /s
find pyarrow -type f -name '*.so*' | while read -r f; do
  strip --strip-unneeded "$f"
done

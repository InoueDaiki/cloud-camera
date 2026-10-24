#!/usr/bin/env bash
# テスト用の Media2 / PTZ WSDL 一式を tests/wsdl/ に用意する(リポジトリには生成済みのものをコミットしている)。
#
# onvif-zeep に同梱の WSDL は古く(スキーマ 2.4 系)、Media2(ver20/media)が含まれないため、
# ONVIF 公式(github.com/onvif/specs)の media2 WSDL と onvif.xsd を取り込み、
# 外部 URL を指す import をローカルの写しに書き換えて、オフラインで zeep が読めるようにする。
set -euo pipefail

SPECS_TAG="${SPECS_TAG:-26.06}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/tests/wsdl"
BASE="https://raw.githubusercontent.com/onvif/specs/${SPECS_TAG}/wsdl"

mkdir -p "$OUT/ver20/media/wsdl" "$OUT/ver20/ptz/wsdl" "$OUT/ver10/schema/ext"
curl -fsSL -o "$OUT/ver20/media/wsdl/media.wsdl" "$BASE/ver20/media/wsdl/media.wsdl"
# PTZ は onvif-zeep にも同梱されているが古いスキーマなので、strict な検証には公式の最新を使う
curl -fsSL -o "$OUT/ver20/ptz/wsdl/ptz.wsdl" "$BASE/ver20/ptz/wsdl/ptz.wsdl"
curl -fsSL -o "$OUT/ver10/schema/onvif.xsd" "$BASE/ver10/schema/onvif.xsd"
curl -fsSL -o "$OUT/ver10/schema/common.xsd" "$BASE/ver10/schema/common.xsd"

# W3C / OASIS の補助スキーマ
curl -fsSL -o "$OUT/ver10/schema/ext/soap-envelope.xsd" "https://www.w3.org/2003/05/soap-envelope/"
curl -fsSL -o "$OUT/ver10/schema/ext/xmlmime.xsd" "https://www.w3.org/2005/05/xmlmime"
curl -fsSL -o "$OUT/ver10/schema/ext/include.xsd" "https://www.w3.org/2004/08/xop/include"
curl -fsSL -o "$OUT/ver10/schema/ext/xml.xsd" "https://www.w3.org/2001/xml.xsd"
curl -fsSL -o "$OUT/ver10/schema/ext/b-2.xsd" "https://docs.oasis-open.org/wsn/b-2.xsd"
curl -fsSL -o "$OUT/ver10/schema/ext/t-1.xsd" "https://docs.oasis-open.org/wsn/t-1.xsd"
curl -fsSL -o "$OUT/ver10/schema/ext/bf-2.xsd" "https://docs.oasis-open.org/wsrf/bf-2.xsd"
curl -fsSL -o "$OUT/ver10/schema/ext/ws-addr.xsd" "https://www.w3.org/2006/03/addressing/ws-addr.xsd"

# 外部 URL の import をローカルに書き換える
python3 - "$OUT" <<'EOF'
import pathlib, re, sys
out = pathlib.Path(sys.argv[1]) / "ver10/schema"
repl = {
    "https://www.w3.org/2005/05/xmlmime": "ext/xmlmime.xsd",
    "http://www.w3.org/2005/05/xmlmime": "ext/xmlmime.xsd",
    "https://www.w3.org/2003/05/soap-envelope": "ext/soap-envelope.xsd",
    "http://docs.oasis-open.org/wsn/b-2.xsd": "ext/b-2.xsd",
    "https://www.w3.org/2004/08/xop/include": "ext/include.xsd",
}
for f in [out / "onvif.xsd", *sorted((out / "ext").glob("*.xsd"))]:
    s = f.read_text()
    for k, v in repl.items():
        v2 = v if f.parent.name == "schema" else v.removeprefix("ext/")
        s = s.replace(f'schemaLocation="{k}"', f'schemaLocation="{v2}"')
    # OASIS / W3C の補助スキーマ同士の参照
    s = re.sub(r'schemaLocation\s*=\s*"http://www\.w3\.org/200[0-9]/[0-9]+/addressing/ws-addr\.xsd"', 'schemaLocation="ws-addr.xsd"', s)
    s = re.sub(r'schemaLocation\s*=\s*"http://docs\.oasis-open\.org/wsrf/bf-2\.xsd"', 'schemaLocation="bf-2.xsd"', s)
    s = re.sub(r'schemaLocation\s*=\s*"http://docs\.oasis-open\.org/wsn/t-1\.xsd"', 'schemaLocation="t-1.xsd"', s)
    s = re.sub(r'schemaLocation\s*=\s*"http://www\.w3\.org/2001/xml\.xsd"', 'schemaLocation="xml.xsd"', s)
    f.write_text(s)
EOF
grep -rn 'schemaLocation="http' "$OUT" || echo "外部参照なし"

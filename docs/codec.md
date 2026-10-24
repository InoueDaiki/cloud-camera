# 映像コーデック: H.264 / H.265 の切り替えと比較測定

(下書き。測定結果は測定後に埋める)

## 切り替えの使い方

既定は H.265(ライブも録画も)。カメラごとに H.264 にできる。再エンコードはしないので、カメラ(エミュレーター)の出力と
エッジの選択、KVS ストリームの media type をそろえる。

| どこ | 設定 | 既定 |
| --- | --- | --- |
| エミュレーター | `main_codec`(YAML)/ `EMULATOR_MAIN_CODEC` / `EMULATOR_CAM{n}_MAIN_CODEC` = `H265` / `H264`。ONVIF の Media2 の Encoding も変わる。sub は常に H.264 | `H265` |
| エッジ | ONVIF の自動選択で優先するコーデック。全体は `CC_PREFER_CODEC`、カメラごとはシャドウの desired `prefer_codec`。優先のコーデックがなければもう一方 | `H265` |
| Terraform | `camera_codecs = { "cam-002" = "H264" }` → KVS ストリームの media type が `video/h264`(作り直しなしで更新) | 全カメラ `video/h265` |
| Lambda / API / ビューワー | 変更不要(下の「H.264 で動くことの確認」) | |

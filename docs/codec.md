# 映像コーデック: H.264 / H.265 の切り替えと比較測定

(下書き。測定結果は測定後に埋める)

## 切り替えの使い方

既定は H.265(ライブも録画も)。カメラごとに H.264 にできる。再エンコードはしない。

### ビューワーから(配信を動かしたまま)

プレーヤーのツールバーの「カメラ設定」(歯車)→「映像形式」で H.265(推奨)/ H.264(互換性重視)を選ぶ。

```
ビューワー ─PUT /api/cameras/{id}/codec {"codec": "H264"}─▶ API ─▶ シャドウ desired.cameras.{id}.codec
  ─▶ エッジ: パイプラインを止め、ONVIF Media2 の SetVideoEncoderConfiguration でカメラの Encoding を変える
     (変えられなければその形式のプロファイルを選ぶ)→ RTSP につなぎ直し、h26Xparse / h26Xtimestamper を組み直す
  ─▶ reported.cameras.{id}.codec に実際の形式(違えば codec_error に理由)
```

- 配信は数秒途切れる(エミュレーターの実測 約 5 秒: エンコーダーの作り直し + 3 秒の待ち + RTSP の接続)
- 以降の録画はその形式。それまでの録画は元の形式のまま(再生は混在してよい。下の「区間の途中の切り替え」)
- `desired.codec` と `reported.codec` が違う間、カメラ一覧に「形式を切替中…」、設定に「切り替え中…」を出す

### 設定で固定する

| どこ | 設定 | 既定 |
| --- | --- | --- |
| エミュレーター | `main_codec`(YAML)/ `EMULATOR_MAIN_CODEC` / `EMULATOR_CAM{n}_MAIN_CODEC` = `H265` / `H264`。Media2 の Encoding も変わる。実行中も Media2 の SetVideoEncoderConfiguration で変えられる。sub は常に H.264 | `H265` |
| エッジ | ONVIF の自動選択で優先する形式。全体は `CC_PREFER_CODEC`、カメラごとはシャドウの desired `prefer_codec`(カメラの設定は変えない)。desired `codec` はカメラの設定も変える | `H265` |
| Terraform | `camera_codecs = { "cam-002" = "H264" }` → KVS ストリームの media type が `video/h264`(作り直しなしで更新) | 全カメラ `video/h265` |

KVS ストリームの media type は参考情報で、PutMedia も HLS も実際の断片のコーデック(kvssink が caps から決める codec-id)で動く。
`video/h265` のストリームに H.264 を送っても、取り込み・HLS・GetClip は問題なかった(実測)。ビューワーからの切り替えでは変えない。

"""MediaMTX の設定と、カメラごとの FFmpeg コマンドを組み立てる。

FFmpeg は 1 台につき 1 プロセスで、1 つの映像ソースから次の 3 つを出す:
  - main: H.265(libx265、CRF + vbv-maxrate の VBR、固定 GOP)→ rtsp://127.0.0.1:8554/cam{n}/main
  - sub : H.264(libx264、低解像度)                              → rtsp://127.0.0.1:8554/cam{n}/sub
  - スナップショット: 1 秒ごとの JPEG(ファイルを上書き)

タイムスタンプ:
  ソースは fps フィルターで CFR にそろえたあと、いったん壁時計(RTCTIME)を pts に入れて時刻を焼き込み、
  最後に setpts=N で 0 から単調増加する pts に振り直す。動画ファイルのループ(-stream_loop -1)で
  入力側の pts が巻き戻っても、出力の pts は途切れず単調増加する。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import TEST_SOURCES, CameraConfig, EmulatorConfig


def mediamtx_config(cfg: EmulatorConfig) -> dict:
    users: list[dict] = [
        # 配信(publish)はコンテナ内の FFmpeg からだけ
        {"user": "any", "pass": "", "ips": ["127.0.0.1", "::1"], "permissions": [{"action": "publish"}, {"action": "api"}]},
    ]
    if cfg.rtsp_auth:
        for cam in cfg.cameras:
            users.append(
                {
                    "user": cam.username,
                    "pass": cam.password,
                    "ips": [],
                    "permissions": [{"action": "read", "path": f"~^{cam.path_prefix}/"}],
                }
            )
    else:
        users.append({"user": "any", "pass": "", "ips": [], "permissions": [{"action": "read"}]})
    return {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "readTimeout": "10s",
        "writeTimeout": "10s",
        "writeQueueSize": 1024,
        "authMethod": "internal",
        "authInternalUsers": users,
        "api": True,
        "apiAddress": "127.0.0.1:9997",
        "rtsp": True,
        "rtspTransports": ["udp", "tcp"],
        "rtspAddress": f":{cfg.rtsp_port}",
        "rtpAddress": f":{cfg.rtp_port}",
        "rtcpAddress": f":{cfg.rtp_port + 1}",
        "rtspAuthMethods": ["basic", "digest"],
        "rtmp": False,
        "hls": False,
        "webrtc": False,
        "srt": False,
        "moq": False,
        "paths": {"all_others": None},
    }


def write_mediamtx_config(cfg: EmulatorConfig) -> Path:
    path = cfg.runtime_dir / "mediamtx.yml"
    path.write_text(yaml.safe_dump(mediamtx_config(cfg), sort_keys=False))
    return path


def snapshot_path(cfg: EmulatorConfig, cam: CameraConfig) -> Path:
    return cfg.runtime_dir / f"{cam.path_prefix}.jpg"


def overlay_text_path(cfg: EmulatorConfig, cam: CameraConfig) -> Path:
    return cfg.runtime_dir / f"{cam.path_prefix}.overlay.txt"


def write_overlay_text(cfg: EmulatorConfig, cam: CameraConfig) -> Path:
    """drawtext の textfile。%{...} の展開は textfile の中身にも効く。

    1 フレームの pts(= 壁時計)から日付・時刻・ミリ秒を出すので、桁ごとにずれることがない。
    """
    name = cam.name.replace("\\", "\\\\").replace("%", "%%")
    text = (
        f"{name}  {cam.main.width}x{cam.main.height}@{cam.main.fps}\n"
        r"%{pts:gmtime:0:%Y-%m-%dT%H\:%M\:%S}.%{eif:mod(floor(t*1000)\,1000):d:3}Z" "\n"
        r"frame %{frame_num}"
    )
    path = overlay_text_path(cfg, cam)
    path.write_text(text)
    return path


def _input_args(cam: CameraConfig) -> list[str]:
    src = cam.source
    m = cam.main
    if src.startswith("file:"):
        file = src[len("file:") :]
        # -re で実時間に合わせて読み、-stream_loop -1 で無限ループ。音声は使わない
        return ["-re", "-stream_loop", "-1", "-i", file]
    if src not in TEST_SOURCES:
        raise ValueError(f"{cam.name}: 不明な映像ソース {src!r}(testsrc 系か file:/path を指定)")
    return ["-re", "-f", "lavfi", "-i", f"{src}=size={m.width}x{m.height}:rate={m.fps}"]


def ffmpeg_command(cfg: EmulatorConfig, cam: CameraConfig) -> list[str]:
    m, s = cam.main, cam.sub
    textfile = overlay_text_path(cfg, cam)
    font = cfg.font_file
    fontsize = max(16, m.height // 24)
    rtsp = f"rtsp://127.0.0.1:{cfg.rtsp_port}/{cam.path_prefix}"

    prep = (
        f"[0:v]fps={m.fps},"
        f"scale={m.width}:{m.height}:force_original_aspect_ratio=decrease,"
        f"pad={m.width}:{m.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
        # 壁時計を pts に入れて焼き込む(TB を 1µs にしてから)
        "settb=AVTB,setpts=RTCTIME,"
        f"drawtext=fontfile={font}:textfile={textfile}:expansion=normal:"
        f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8:"
        "line_spacing=6:x=16:y=16,"
        # 出力の pts は 0 からのフレーム番号(単調増加)
        f"settb=1/{m.fps},setpts=N,"
        "split=3[main][s][j];"
        f"[s]scale={s.width}:{s.height}[sub];"
        "[j]fps=1[snap]"
    )

    main_x265 = ":".join(
        [
            f"keyint={m.gov_length}",
            f"min-keyint={m.gov_length}",
            "scenecut=0",
            "open-gop=0",
            "repeat-headers=1",  # 途中から受信したクライアントでも VPS/SPS/PPS が届くように
            "aud=1",
            f"crf={m.crf}",
            f"vbv-maxrate={m.bitrate_kbps}",
            f"vbv-bufsize={m.bitrate_kbps * 2}",
            "log-level=warning",
        ]
    )

    return [
        cfg.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",  # スナップショットの JPEG を上書きする
        "-loglevel", "warning",
        "-progress", "pipe:1",
        "-stats_period", "5",
        *_input_args(cam),
        "-filter_complex", prep,
        # ---- main(H.265)----
        "-map", "[main]",
        "-an",
        "-c:v", "libx265",
        "-preset", cam.preset,
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-x265-params", main_x265,
        "-fps_mode", "passthrough",
        "-f", "rtsp", "-rtsp_transport", "tcp", f"{rtsp}/main",
        # ---- sub(H.264)----
        "-map", "[sub]",
        "-an",
        "-c:v", "libx264",
        "-preset", cam.preset,
        "-tune", "zerolatency",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-crf", str(s.crf),
        "-maxrate", f"{s.bitrate_kbps}k",
        "-bufsize", f"{s.bitrate_kbps * 2}k",
        "-g", str(s.gov_length),
        "-keyint_min", str(s.gov_length),
        "-sc_threshold", "0",
        "-bf", "0",
        "-fps_mode", "passthrough",
        "-f", "rtsp", "-rtsp_transport", "tcp", f"{rtsp}/sub",
        # ---- スナップショット ----
        "-map", "[snap]",
        "-an",
        "-c:v", "mjpeg",
        "-q:v", "5",
        "-f", "image2",
        "-update", "1",
        "-atomic_writing", "1",
        str(snapshot_path(cfg, cam)),
    ]

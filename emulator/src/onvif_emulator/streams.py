"""MediaMTX の設定と、カメラごとの FFmpeg コマンドを組み立てる。

FFmpeg は 1 台につき 1 プロセスで、1 つの映像ソースから次の 3 つを出す:
  - main: H.265(libx265、CRF + vbv-maxrate の VBR、固定 GOP)→ rtsp://127.0.0.1:8554/cam{n}/main
  - sub : H.264(libx264、低解像度)                              → rtsp://127.0.0.1:8554/cam{n}/sub
  - スナップショット: 1 秒ごとの JPEG(ファイルを上書き)

タイムスタンプ:
  ソースは fps フィルターで CFR にそろえたあと、いったん壁時計(RTCTIME)を pts に入れて時刻を焼き込み、
  最後に setpts=N で 0 から単調増加する pts に振り直す。動画ファイルのループ(-stream_loop -1)で
  入力側の pts が巻き戻っても、出力の pts は途切れず単調増加する。

疑似 PTZ(カメラの ptz.enabled が true のとき):
  元映像を main より高い解像度(テストパターンは main × source_scale、動画ファイルは元の解像度)で作り、
  zmq フィルターで受けたコマンドで crop の窓を動かしてから main の解像度に拡大縮小する。
  出力の解像度・コーデックは変わらず、FFmpeg も再起動しないので配信は途切れない。
  時刻とカメラ名の焼き込みは切り出しのあと(常に同じ位置に見える)。sub とスナップショットは main から作るので同じ窓になる。
  crop の式:
    w = iw/倍率、h = ow×(main の高さ / 幅)(w を変えるコマンド 1 つで h も変わり、縦横比が崩れない)
    x = (in_w-out_w)×fx、y = (in_h-out_h)×fy(フレームごとに評価されるので、倍率が変わっても位置の比率が保たれる)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
from pathlib import Path

import yaml

from .config import TEST_SOURCES, CameraConfig, EmulatorConfig
from .ptz import CropWindow

log = logging.getLogger(__name__)

PTZ_CROP_FILTER = "crop@ptz"  # zmq のコマンドの宛先


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


def ptz_zmq_path(cfg: EmulatorConfig, cam: CameraConfig) -> Path:
    """FFmpeg の zmq フィルターが bind する UNIX ドメインソケット(ホストのポートと衝突しない)。"""
    return cfg.runtime_dir / f"{cam.path_prefix}.ptz.sock"


def ptz_overlay_line(window: CropWindow, pan: float, tilt: float, zoom: float, moving: bool) -> str:
    return (
        f"PTZ pan {pan:+.3f} tilt {tilt:+.3f} zoom {zoom:.3f} ({window.magnification:.2f}x)"
        + ("  MOVING" if moving else "")
    )


def write_overlay_text(cfg: EmulatorConfig, cam: CameraConfig, ptz_line: str | None = None) -> Path:
    """drawtext の textfile。%{...} の展開は textfile の中身にも効く。

    1 フレームの pts(= 壁時計)から日付・時刻・ミリ秒を出すので、桁ごとにずれることがない。
    PTZ があるときは 4 行目に現在の pan / tilt / zoom を入れる。drawtext は毎フレーム読み直す(reload=1)ので、
    書き換えは一時ファイル + rename で行う(読みかけの中途半端な内容を出さない)。
    """
    name = cam.name.replace("\\", "\\\\").replace("%", "%%")
    text = (
        f"{name}  {cam.main.width}x{cam.main.height}@{cam.main.fps}\n"
        r"%{pts:gmtime:0:%Y-%m-%dT%H\:%M\:%S}.%{eif:mod(floor(t*1000)\,1000):d:3}Z" "\n"
        r"frame %{frame_num}"
    )
    if ptz_line is not None:
        text += "\n" + ptz_line.replace("%", "%%")
    path = overlay_text_path(cfg, cam)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


@functools.lru_cache(maxsize=32)
def probe_resolution(ffprobe_bin: str, file: str) -> tuple[int, int] | None:
    """動画ファイルの解像度(ffprobe)。取れなければ None。"""
    try:
        out = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "json", file],
            capture_output=True, text=True, timeout=15, check=True,
        )
        st = json.loads(out.stdout)["streams"][0]
        return int(st["width"]), int(st["height"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        log.warning("解像度を取得できません: %s", file)
        return None


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def ptz_source_size(cfg: EmulatorConfig, cam: CameraConfig) -> tuple[int, int]:
    """疑似 PTZ の元映像(切り出す前)の解像度。縦横比は main と同じ。

    テストパターンは main × source_scale。動画ファイルは元の解像度を main の縦横比に合わせて黒帯で広げた大きさ
    (main より小さければ main の大きさ)。
    """
    m = cam.main
    if cam.source.startswith("file:"):
        ffprobe = str(Path(cfg.ffmpeg_bin).with_name("ffprobe")) if "/" in cfg.ffmpeg_bin else "ffprobe"
        res = probe_resolution(ffprobe, cam.source[len("file:") :])
        if res is not None:
            fw, fh = res
            # 元の解像度を含む、main と同じ縦横比の最小の大きさ
            w = max(fw, fh * m.width / m.height, m.width)
            return _even(w), _even(w * m.height / m.width)
    k = cam.ptz.source_scale
    return _even(m.width * k), _even(m.height * k)


def _f(v: float) -> str:
    return f"{v:.5f}".rstrip("0").rstrip(".") or "0"


def ptz_crop_commands(window: CropWindow) -> dict[str, str]:
    """crop の w / x / y に送る式。h は w から決まる(コマンド行で h=ow*H/W にしてある)。"""
    return {
        "w": f"iw/{_f(window.magnification)}",
        "x": f"(in_w-out_w)*{_f(window.fx)}",
        "y": f"(in_h-out_h)*{_f(window.fy)}",
    }


def _input_args(cam: CameraConfig, size: tuple[int, int]) -> list[str]:
    src = cam.source
    m = cam.main
    if src.startswith("file:"):
        file = src[len("file:") :]
        # -re で実時間に合わせて読み、-stream_loop -1 で無限ループ。音声は使わない
        return ["-re", "-stream_loop", "-1", "-i", file]
    if src not in TEST_SOURCES:
        raise ValueError(f"{cam.name}: 不明な映像ソース {src!r}(testsrc 系か file:/path を指定)")
    return ["-re", "-f", "lavfi", "-i", f"{src}=size={size[0]}x{size[1]}:rate={m.fps}"]


def ffmpeg_command(cfg: EmulatorConfig, cam: CameraConfig, window: CropWindow | None = None) -> list[str]:
    """window は疑似 PTZ の現在の切り出し窓(FFmpeg の起動時の初期値。省略で等倍)。PTZ が無効なら使わない。"""
    m, s = cam.main, cam.sub
    textfile = overlay_text_path(cfg, cam)
    font = cfg.font_file
    fontsize = max(16, m.height // 24)
    rtsp = f"rtsp://127.0.0.1:{cfg.rtsp_port}/{cam.path_prefix}"

    if cam.ptz.enabled:
        sw, sh = ptz_source_size(cfg, cam)
        c = ptz_crop_commands(window or CropWindow(1.0, 0.5, 0.5))
        sock = ptz_zmq_path(cfg, cam)
        source = (
            f"[0:v]fps={m.fps},"
            f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
            f"pad={sw}:{sh}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            # PTZ のコマンドを受ける(フレームごとに溜まったコマンドをすべて処理してから次へ渡す)
            f"zmq=bind_address=ipc\\\\://{sock},"
            f"{PTZ_CROP_FILTER}=w={c['w']}:h=ow*{m.height}/{m.width}:x={c['x']}:y={c['y']},"
            # crop のサイズが変わったあとに scale が止まるのを避ける(FFmpeg 7.1 で crop と scale を直結すると、
            # コマンドで crop の出力サイズを変えた直後からフレームが流れなくなる。null を挟むと起きない)
            "null,"
            f"scale={m.width}:{m.height}:flags=bilinear,setsar=1,"
        )
        reload = ":reload=1"
    else:
        source = (
            f"[0:v]fps={m.fps},"
            f"scale={m.width}:{m.height}:force_original_aspect_ratio=decrease,"
            f"pad={m.width}:{m.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
        )
        sw, sh = m.width, m.height
        reload = ""

    prep = (
        source
        # 壁時計を pts に入れて焼き込む(TB を 1µs にしてから)
        + "settb=AVTB,setpts=RTCTIME,"
        f"drawtext=fontfile={font}:textfile={textfile}:expansion=normal{reload}:"
        f"fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=8:"
        "line_spacing=6:x=16:y=16,"
        # 出力の pts は 0 からのフレーム番号(単調増加)
        f"settb=1/{m.fps},setpts=N,"
        "split=3[main][s][j];"
        f"[s]scale={s.width}:{s.height}[sub];"
        # スナップショットは出力側の -r 1 で間引く。フィルター内で fps=1 にすると、FFmpeg 7.1 はフィルターグラフを
        # いちばん出力の少ない出口(スナップショット)に合わせて回すため、main / sub のフレームが 1 秒ごとにまとめて
        # 送り出される(RTSP がバースト到着になり、PTZ の反映や遅延の計測が最大 1 秒ぶれる)
        "[j]null[snap]"
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
        *_input_args(cam, (sw, sh)),
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
        "-r", "1",
        "-c:v", "mjpeg",
        "-q:v", "5",
        "-f", "image2",
        "-update", "1",
        "-atomic_writing", "1",
        str(snapshot_path(cfg, cam)),
    ]

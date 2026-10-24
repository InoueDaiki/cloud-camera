"""設定の読み込み。

優先順位(後ろほど強い):
  組み込みの既定値 < YAML の defaults < YAML の cameras[i] < 環境変数(全体) < 環境変数(カメラ別)

環境変数の一覧は README を参照。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass
class StreamSettings:
    """1 本の映像ストリーム(main / sub)の設定。実行中に SetVideoEncoderConfiguration で一部変わる。"""

    codec: str  # "H265" or "H264"
    width: int
    height: int
    fps: int
    bitrate_kbps: int  # VBR の上限(vbv-maxrate)
    gov_length: int  # キーフレーム間隔(フレーム数)
    quality: float  # ONVIF の Quality(0〜100)。大きいほど高画質(CRF に変換する)

    @property
    def keyframe_interval_s(self) -> float:
        return self.gov_length / self.fps

    @property
    def crf(self) -> int:
        """Quality 0〜100 を CRF 40〜20 に線形変換する。"""
        q = min(max(self.quality, 0.0), 100.0)
        return round(40 - q * 0.2)


@dataclass
class PtzSettings:
    """疑似 PTZ(元映像の切り出し)の設定。カメラごと。"""

    enabled: bool = True
    max_zoom: float = 4.0  # zoom = 1 のときの倍率(倍率 = 1 + zoom × (max_zoom − 1))
    pan_tilt_speed: float = 0.5  # 速度 1(最大)のときの pan / tilt の速さ(Generic 空間の単位 / 秒)
    zoom_speed: float = 0.5  # 速度 1 のときの zoom の速さ(単位 / 秒。0→1 に 2 秒)
    source_scale: float = 2.0  # テストパターンの元映像を main の何倍の解像度で作るか(動画ファイルは元の解像度)


@dataclass
class CameraConfig:
    index: int  # 1 始まり。RTSP パス cam{index}/... と ONVIF ポートに使う
    name: str
    username: str
    password: str
    source: str  # testsrc / testsrc2 / smptebars / file:/path/to.mp4
    main: StreamSettings
    sub: StreamSettings
    onvif_port: int
    manufacturer: str
    model: str
    firmware: str
    serial: str
    hardware_id: str
    location: str
    preset: str  # x264/x265 の preset
    ptz: PtzSettings = field(default_factory=PtzSettings)

    @property
    def path_prefix(self) -> str:
        return f"cam{self.index}"

    @property
    def uuid(self) -> str:
        """WS-Discovery の EndpointReference。名前から決まるので再起動しても変わらない。"""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"onvif-emulator:{self.name}"))

    @property
    def mac(self) -> str:
        h = hashlib.sha1(self.name.encode()).digest()
        # ローカル管理アドレス(02:...)にする
        return ":".join(f"{b:02x}" for b in (0x02, h[0], h[1], h[2], h[3], h[4]))


@dataclass
class EmulatorConfig:
    cameras: list[CameraConfig]
    onvif_port: int = 8080
    rtsp_port: int = 8554
    rtp_port: int = 8000  # MediaMTX の RTP/UDP(RTCP は +1)
    discovery_enabled: bool = True
    discovery_port: int = 3702
    advertise_host: str | None = None  # 未指定ならリクエストの Host ヘッダーや経路から決める
    rtsp_auth: bool = False  # true にすると RTSP の読み出しにカメラの認証情報が必要
    streams_enabled: bool = True  # false で MediaMTX と FFmpeg を起動しない(SOAP だけ)
    runtime_dir: Path = field(default_factory=lambda: Path("/tmp/onvif-emulator"))
    mediamtx_bin: str = "mediamtx"
    ffmpeg_bin: str = "ffmpeg"
    font_file: str = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
    auth_max_skew_s: int = 0  # UsernameToken の Created の許容ずれ(秒)。0 で検査しない
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# 既定値
# ---------------------------------------------------------------------------

CAMERA_DEFAULTS: dict[str, Any] = {
    "username": "admin",
    "password": "admin",
    "source": "testsrc",
    "width": 1280,
    "height": 720,
    "fps": 15,
    "bitrate_kbps": 1024,
    "keyframe_interval_s": 1.0,
    "quality": 60,
    "sub_width": 640,
    "sub_height": 360,
    "sub_bitrate_kbps": 384,
    "preset": "ultrafast",
    "manufacturer": "CloudCamera",
    "model": "ONVIF-Emulator",
    "firmware": "0.1.0",
    "location": "emulator",
    "ptz": True,
    "ptz_max_zoom": 4.0,
    "ptz_pan_tilt_speed": 0.5,
    "ptz_zoom_speed": 0.5,
    "ptz_source_scale": 2.0,
}

# 環境変数名の接尾辞 → 設定キーと型
_CAMERA_ENV_KEYS: dict[str, tuple[str, type]] = {
    "NAME": ("name", str),
    "USERNAME": ("username", str),
    "PASSWORD": ("password", str),
    "SOURCE": ("source", str),
    "WIDTH": ("width", int),
    "HEIGHT": ("height", int),
    "FPS": ("fps", int),
    "BITRATE_KBPS": ("bitrate_kbps", int),
    "KEYFRAME_INTERVAL_S": ("keyframe_interval_s", float),
    "QUALITY": ("quality", float),
    "SUB_WIDTH": ("sub_width", int),
    "SUB_HEIGHT": ("sub_height", int),
    "SUB_BITRATE_KBPS": ("sub_bitrate_kbps", int),
    "PRESET": ("preset", str),
    "MANUFACTURER": ("manufacturer", str),
    "MODEL": ("model", str),
    "FIRMWARE": ("firmware", str),
    "LOCATION": ("location", str),
    "ONVIF_PORT": ("onvif_port", int),
    "PTZ": ("ptz", bool),
    "PTZ_MAX_ZOOM": ("ptz_max_zoom", float),
    "PTZ_PAN_TILT_SPEED": ("ptz_pan_tilt_speed", float),
    "PTZ_ZOOM_SPEED": ("ptz_zoom_speed", float),
    "PTZ_SOURCE_SCALE": ("ptz_source_scale", float),
}

_GLOBAL_ENV_KEYS: dict[str, tuple[str, type]] = {
    "ONVIF_PORT": ("onvif_port", int),
    "RTSP_PORT": ("rtsp_port", int),
    "RTP_PORT": ("rtp_port", int),
    "DISCOVERY": ("discovery_enabled", bool),
    "DISCOVERY_PORT": ("discovery_port", int),
    "ADVERTISE_HOST": ("advertise_host", str),
    "RTSP_AUTH": ("rtsp_auth", bool),
    "STREAMS": ("streams_enabled", bool),
    "RUNTIME_DIR": ("runtime_dir", Path),
    "MEDIAMTX_BIN": ("mediamtx_bin", str),
    "FFMPEG_BIN": ("ffmpeg_bin", str),
    "FONT_FILE": ("font_file", str),
    "AUTH_MAX_SKEW_S": ("auth_max_skew_s", int),
    "LOG_LEVEL": ("log_level", str),
}

ENV_PREFIX = "EMULATOR_"
DEFAULT_CAMERA_COUNT = 2

# lavfi のテストパターン名
TEST_SOURCES = {"testsrc", "testsrc2", "smptebars", "smptehdbars", "rgbtestsrc", "mandelbrot", "life"}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _convert(v: Any, typ: type) -> Any:
    if typ is bool:
        return _to_bool(v)
    if typ is Path:
        return Path(v)
    return typ(v)


def _env_overrides(env: dict[str, str], prefix: str, keys: dict[str, tuple[str, type]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for suffix, (key, typ) in keys.items():
        name = prefix + suffix
        if name in env and env[name] != "":
            out[key] = _convert(env[name], typ)
    return out


def _build_camera(index: int, values: dict[str, Any], base_port: int) -> CameraConfig:
    v = dict(values)
    name = v.get("name") or f"cam-{index:03d}"
    fps = int(v["fps"])
    gov = max(1, round(fps * float(v["keyframe_interval_s"])))
    source = str(v["source"])
    if not source.startswith("file:") and source not in TEST_SOURCES:
        raise ValueError(f"{name}: 不明な映像ソース {source!r}({sorted(TEST_SOURCES)} か file:/path)")
    serial = hashlib.sha1(name.encode()).hexdigest()[:12].upper()
    ptz = PtzSettings(
        enabled=_to_bool(v["ptz"]),
        max_zoom=float(v["ptz_max_zoom"]),
        pan_tilt_speed=float(v["ptz_pan_tilt_speed"]),
        zoom_speed=float(v["ptz_zoom_speed"]),
        source_scale=float(v["ptz_source_scale"]),
    )
    if not 1.0 <= ptz.max_zoom <= 16.0:
        raise ValueError(f"{name}: ptz_max_zoom は 1〜16 にしてください({ptz.max_zoom})")
    if ptz.pan_tilt_speed <= 0 or ptz.zoom_speed <= 0:
        raise ValueError(f"{name}: PTZ の速度は正の値にしてください")
    if not 1.0 <= ptz.source_scale <= 4.0:
        raise ValueError(f"{name}: ptz_source_scale は 1〜4 にしてください({ptz.source_scale})")
    return CameraConfig(
        index=index,
        name=name,
        username=str(v["username"]),
        password=str(v["password"]),
        source=source,
        main=StreamSettings(
            codec="H265",
            width=int(v["width"]),
            height=int(v["height"]),
            fps=fps,
            bitrate_kbps=int(v["bitrate_kbps"]),
            gov_length=gov,
            quality=float(v["quality"]),
        ),
        sub=StreamSettings(
            codec="H264",
            width=int(v["sub_width"]),
            height=int(v["sub_height"]),
            fps=fps,
            bitrate_kbps=int(v["sub_bitrate_kbps"]),
            gov_length=gov,
            quality=float(v["quality"]),
        ),
        onvif_port=int(v.get("onvif_port") or base_port + index - 1),
        manufacturer=str(v["manufacturer"]),
        model=str(v["model"]),
        firmware=str(v["firmware"]),
        serial=str(v.get("serial") or serial),
        hardware_id=str(v.get("hardware_id") or f"EMU-{index}"),
        location=str(v["location"]),
        preset=str(v["preset"]),
        ptz=ptz,
    )


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> EmulatorConfig:
    """YAML(任意)と環境変数から設定を作る。"""
    env = dict(os.environ if env is None else env)
    path = path or env.get(ENV_PREFIX + "CONFIG")
    doc: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            doc = yaml.safe_load(p.read_text()) or {}
        else:
            raise FileNotFoundError(f"設定ファイルがありません: {p}")

    # 全体設定: YAML → 環境変数
    glob: dict[str, Any] = {}
    for _, (key, typ) in _GLOBAL_ENV_KEYS.items():
        if key in doc and doc[key] is not None:
            glob[key] = _convert(doc[key], typ)
    glob.update(_env_overrides(env, ENV_PREFIX, _GLOBAL_ENV_KEYS))
    base_port = int(glob.get("onvif_port", 8080))

    # カメラ台数: YAML の cameras(リスト or 整数)/count、環境変数 EMULATOR_CAMERAS
    yaml_cams = doc.get("cameras") or []
    if isinstance(yaml_cams, int):
        count, yaml_cams = yaml_cams, []
    else:
        count = int(doc.get("count") or len(yaml_cams) or DEFAULT_CAMERA_COUNT)
    if env.get(ENV_PREFIX + "CAMERAS"):
        count = int(env[ENV_PREFIX + "CAMERAS"])
    if count < 1:
        raise ValueError("カメラ台数は 1 以上にしてください")

    yaml_defaults = doc.get("defaults") or {}
    env_defaults = _env_overrides(env, ENV_PREFIX, _CAMERA_ENV_KEYS)
    env_defaults.pop("name", None)  # 名前は全体で共通にしない
    env_defaults.pop("onvif_port", None)  # EMULATOR_ONVIF_PORT は全体設定(先頭カメラのポート)

    cameras: list[CameraConfig] = []
    for i in range(1, count + 1):
        values = dict(CAMERA_DEFAULTS)
        values.update(yaml_defaults)
        if i - 1 < len(yaml_cams):
            values.update(yaml_cams[i - 1] or {})
        values.update(env_defaults)
        values.update(_env_overrides(env, f"{ENV_PREFIX}CAM{i}_", _CAMERA_ENV_KEYS))
        cameras.append(_build_camera(i, values, base_port))

    names = [c.name for c in cameras]
    if len(set(names)) != len(names):
        raise ValueError(f"カメラ名が重複しています: {names}")
    ports = [c.onvif_port for c in cameras]
    if len(set(ports)) != len(ports):
        raise ValueError(f"ONVIF ポートが重複しています: {ports}")

    return EmulatorConfig(cameras=cameras, **glob)

"""疑似 PTZ の状態と動きのシミュレーション(映像やSOAPに依存しない純粋なロジック)。

座標は ONVIF の Generic 空間に合わせる(docs/contracts.md の「PTZ」節):
  pan  -1〜1(-1 が左端、1 が右端)、tilt -1〜1(-1 が下端、1 が上端)、zoom 0〜1(0 が等倍、1 が最大倍率)

切り出し窓との対応:
  倍率 m = 1 + zoom × (max_zoom − 1)(線形)。窓の大きさは元映像の 1/m
  窓の左端 = (元映像の幅 − 窓の幅) × (pan + 1) / 2、上端 = (元映像の高さ − 窓の高さ) × (1 − tilt) / 2
  そのため等倍(zoom = 0)では pan / tilt を動かしても見た目は変わらない

動きは時刻から遅延評価する(update(now) で前回からの経過分だけ進める)。SOAP の処理と映像の更新ループの
どちらから呼んでもよく、別スレッドやタイマーを必要としない。時計は差し替えられる(テスト用)。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import PtzSettings

MAX_PRESETS = 32
PRESET_NAME_MAX = 64
TIMEOUT_RANGE_S = (0.1, 600.0)  # ContinuousMove の Timeout の範囲(外れたら丸める)
DEFAULT_TIMEOUT_S = 10.0  # Timeout を省略したとき(DefaultPTZTimeout)


class PtzError(Exception):
    """PTZ 操作のエラー。kind は ONVIF の ter: サブコードに対応する(NoToken、PresetExist など)。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class Axis:
    """1 軸(pan / tilt / zoom)。速度モード(ContinuousMove)か目標モード(Absolute / Relative / Goto)で動く。"""

    lo: float
    hi: float
    max_speed: float  # 単位 / 秒
    pos: float = 0.0
    velocity: float = 0.0  # 速度モードの速さ(単位 / 秒、符号付き)
    target: float | None = None  # 目標モードの目標位置
    speed: float = 0.0  # 目標モードの速さ(単位 / 秒)

    @property
    def moving(self) -> bool:
        return self.velocity != 0.0 or self.target is not None

    def stop(self) -> None:
        self.velocity = 0.0
        self.target = None

    def set_velocity(self, v: float) -> None:
        """v は -1〜1(範囲外は丸める)。0 で停止。"""
        self.target = None
        self.velocity = clamp(v, -1.0, 1.0) * self.max_speed

    def set_target(self, target: float, speed_fraction: float) -> None:
        self.velocity = 0.0
        self.target = clamp(target, self.lo, self.hi)
        self.speed = clamp(speed_fraction, 0.0, 1.0) * self.max_speed
        if self.speed <= 0.0:
            self.speed = self.max_speed
        if self.target == self.pos:
            self.target = None

    def advance(self, dt: float) -> None:
        if dt <= 0:
            return
        if self.target is not None:
            d = self.target - self.pos
            step = self.speed * dt
            if abs(d) <= step:
                self.pos = self.target
                self.target = None
            else:
                self.pos += math.copysign(step, d)
        elif self.velocity != 0.0:
            self.pos += self.velocity * dt
            # 端に着いたら止める(MoveStatus は IDLE になる)
            if self.pos <= self.lo:
                self.pos, self.velocity = self.lo, 0.0
            elif self.pos >= self.hi:
                self.pos, self.velocity = self.hi, 0.0


@dataclass
class Preset:
    token: str
    name: str
    pan: float
    tilt: float
    zoom: float


@dataclass(frozen=True)
class CropWindow:
    """元映像に対する切り出し窓(大きさと位置の比率)。映像側はこれを FFmpeg の crop の式にする。"""

    magnification: float  # 1 以上。窓の幅 = 元映像の幅 / magnification
    fx: float  # 0〜1。窓の左端 = (元映像の幅 − 窓の幅) × fx
    fy: float  # 0〜1。窓の上端 = (元映像の高さ − 窓の高さ) × fy


class PtzController:
    """1 台のカメラの PTZ 状態。状態はメモリ上だけ(再起動で初期化)。"""

    def __init__(self, settings: PtzSettings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.pan = Axis(-1.0, 1.0, settings.pan_tilt_speed)
        self.tilt = Axis(-1.0, 1.0, settings.pan_tilt_speed)
        self.zoom = Axis(0.0, 1.0, settings.zoom_speed)
        self.home = (0.0, 0.0, 0.0)
        self.presets: dict[str, Preset] = {}
        self.deadline: float | None = None  # ContinuousMove の自動停止の時刻
        self._last = clock()
        self.listeners: list[Callable[[], None]] = []  # 操作のたびに呼ぶ(映像の更新ループを起こす)

    # ---- 時間を進める ----

    @property
    def axes(self) -> tuple[Axis, Axis, Axis]:
        return (self.pan, self.tilt, self.zoom)

    def update(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if now <= self._last:
            return
        if self.deadline is not None and self.deadline <= now:
            # Timeout の時刻ちょうどで速度モードの軸を止める
            dt = max(0.0, self.deadline - self._last)
            for a in self.axes:
                a.advance(dt)
            for a in self.axes:
                a.velocity = 0.0
            self._last = max(self._last, self.deadline)
            self.deadline = None
        dt = now - self._last
        for a in self.axes:
            a.advance(dt)
        self._last = now
        if self.deadline is not None and not any(a.velocity for a in self.axes):
            self.deadline = None

    def _changed(self) -> None:
        for cb in self.listeners:
            cb()

    # ---- 状態 ----

    def position(self) -> tuple[float, float, float]:
        self.update()
        return (self.pan.pos, self.tilt.pos, self.zoom.pos)

    def moving(self) -> tuple[bool, bool]:
        """(pan/tilt が動いているか, zoom が動いているか)"""
        self.update()
        return (self.pan.moving or self.tilt.moving, self.zoom.moving)

    @property
    def any_moving(self) -> bool:
        return any(a.moving for a in self.axes)

    def magnification(self, zoom: float | None = None) -> float:
        z = self.zoom.pos if zoom is None else zoom
        return 1.0 + clamp(z, 0.0, 1.0) * (self.settings.max_zoom - 1.0)

    def window(self) -> CropWindow:
        self.update()
        return CropWindow(
            magnification=self.magnification(),
            fx=(self.pan.pos + 1.0) / 2.0,
            fy=(1.0 - self.tilt.pos) / 2.0,
        )

    # ---- 操作 ----

    def continuous_move(
        self, pan: float | None, tilt: float | None, zoom: float | None, timeout_s: float | None = None
    ) -> None:
        """速度(-1〜1)で動かす。None の軸は変えない。Timeout が来たら速度モードの軸を止める。"""
        self.update()
        for axis, v in ((self.pan, pan), (self.tilt, tilt), (self.zoom, zoom)):
            if v is not None:
                axis.set_velocity(v)
        timeout = DEFAULT_TIMEOUT_S if timeout_s is None else clamp(timeout_s, *TIMEOUT_RANGE_S)
        self.deadline = self._last + timeout if any(a.velocity for a in self.axes) else None
        self._changed()

    def absolute_move(
        self,
        pan: float | None,
        tilt: float | None,
        zoom: float | None,
        pan_tilt_speed: tuple[float, float] | None = None,
        zoom_speed: float | None = None,
    ) -> None:
        """位置へ動かす(範囲外は丸める)。速度は 0〜1(最大速度に対する割合、省略で 1)。"""
        self.update()
        ps, ts = pan_tilt_speed if pan_tilt_speed is not None else (1.0, 1.0)
        zs = 1.0 if zoom_speed is None else zoom_speed
        for axis, v, s in ((self.pan, pan, ps), (self.tilt, tilt, ts), (self.zoom, zoom, zs)):
            if v is not None:
                axis.set_target(v, s)
        self._changed()

    def relative_move(
        self,
        pan: float | None,
        tilt: float | None,
        zoom: float | None,
        pan_tilt_speed: tuple[float, float] | None = None,
        zoom_speed: float | None = None,
    ) -> None:
        """今の位置(目標へ動いている途中なら目標)からの差分で動かす。"""
        self.update()

        def base(a: Axis) -> float:
            return a.target if a.target is not None else a.pos

        self.absolute_move(
            None if pan is None else base(self.pan) + pan,
            None if tilt is None else base(self.tilt) + tilt,
            None if zoom is None else base(self.zoom) + zoom,
            pan_tilt_speed,
            zoom_speed,
        )

    def stop(self, pan_tilt: bool = True, zoom: bool = True) -> None:
        self.update()
        if pan_tilt:
            self.pan.stop()
            self.tilt.stop()
        if zoom:
            self.zoom.stop()
        if not any(a.velocity for a in self.axes):
            self.deadline = None
        self._changed()

    # ---- プリセットとホーム ----

    def _next_token(self) -> str:
        n = 1
        while str(n) in self.presets:
            n += 1
        return str(n)

    def set_preset(self, name: str | None = None, token: str | None = None) -> str:
        """今の位置をプリセットに保存する。token を指定したら上書き(存在しなければエラー)。"""
        pan, tilt, zoom = self.position()
        if name is not None:
            name = name.strip()
            if not name or len(name) > PRESET_NAME_MAX:
                raise PtzError("InvalidPresetName", f"Preset name must be 1-{PRESET_NAME_MAX} characters")
        if token:
            if token not in self.presets:
                raise PtzError("NoToken", f"Preset token does not exist: {token}")
        else:
            if len(self.presets) >= MAX_PRESETS:
                raise PtzError("TooManyPresets", f"Maximum number of presets ({MAX_PRESETS}) reached")
            token = self._next_token()
        if name is not None and any(p.name == name and p.token != token for p in self.presets.values()):
            raise PtzError("PresetExist", f"Preset name already exists: {name}")
        old = self.presets.get(token)
        final_name = name if name is not None else (old.name if old else f"Preset {token}")
        self.presets[token] = Preset(token, final_name, pan, tilt, zoom)
        return token

    def remove_preset(self, token: str) -> None:
        if token not in self.presets:
            raise PtzError("NoToken", f"Preset token does not exist: {token}")
        del self.presets[token]

    def goto_preset(self, token: str, pan_tilt_speed=None, zoom_speed=None) -> None:
        p = self.presets.get(token)
        if p is None:
            raise PtzError("NoToken", f"Preset token does not exist: {token}")
        self.absolute_move(p.pan, p.tilt, p.zoom, pan_tilt_speed, zoom_speed)

    def set_home(self) -> None:
        self.home = self.position()

    def goto_home(self, pan_tilt_speed=None, zoom_speed=None) -> None:
        self.absolute_move(*self.home, pan_tilt_speed, zoom_speed)


def parse_duration(text: str) -> float:
    """xs:duration(PT10S、PT0.5S、PT1M、P0DT0H0M5S など)を秒にする。年・月は扱わない。"""
    m = re.fullmatch(
        r"\s*(-)?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d*)?)S)?)?\s*",
        text or "",
    )
    if not m or text.strip() in ("P", "PT"):
        raise ValueError(f"invalid duration: {text!r}")
    sign, d, h, mi, s = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    return -total if sign else total

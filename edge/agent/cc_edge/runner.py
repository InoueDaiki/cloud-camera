"""1 カメラぶんの GStreamer パイプラインを動かす子プロセス。

    python3 -m cc_edge.runner '<PipelineSpec の JSON>'

rtspsrc ! rtph26Xdepay ! h26Xparse ! capsfilter(hvc1/avc, au) ! h26Xtimestamper ! queue
        ! kvssink|fakesink|matroskamux+filesink

再エンコードしない。kvssink は NAL 変換をしない設定(nal_adaptation_flags=0)で動くため、
コーデックプライベートデータ(VPS/SPS/PPS)を caps の codec_data で渡す必要がある。
そのため h265parse の出力を stream-format=hvc1(長さ前置き + HEVCDecoderConfigurationRecord)に固定する。

標準出力に 1 行 1 JSON のイベントを出す(親プロセスが読む):
  {"type": "caps", ...} / {"type": "stats", ...} / {"type": "ack", ...} / {"type": "kvs_error", ...}
異常(バスの ERROR、EOS、一定時間バッファが来ない)で 0 以外の終了コードで終わる。親がバックオフ付きで再起動する。
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .pipeline import PipelineSpec  # noqa: E402

ACK_TYPES = {0: "UNDEFINED", 1: "BUFFERING", 2: "RECEIVED", 3: "PERSISTED", 4: "ERROR", 5: "IDLE"}


class _FragmentAck(ctypes.Structure):
    # amazon-kinesis-video-streams-pic の struct __FragmentAck
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("ack_type", ctypes.c_int),
        ("timestamp", ctypes.c_uint64),
        ("sequence_number", ctypes.c_char * 129),
        ("result", ctypes.c_int),
    ]


def _pointer_address(ptr) -> int:
    """G_TYPE_POINTER のシグナル引数からアドレスを取り出す。

    PyGObject は gobject.GPointer で包んで渡す。CPython の PyGPointer は
    {PyObject_HEAD; gpointer pointer; ...} なので、ヘッダーの直後を読む。
    """
    if isinstance(ptr, int):
        return ptr
    head = ctypes.sizeof(ctypes.c_ssize_t) + ctypes.sizeof(ctypes.c_void_p)  # ob_refcnt + ob_type
    addr = ctypes.c_void_p.from_address(id(ptr) + head).value
    if not addr:
        raise ValueError("ポインタが NULL")
    return addr


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class Runner:
    def __init__(self, spec: PipelineSpec):
        self.spec = spec
        self.loop = GLib.MainLoop()
        self.exit_code = 0
        self.bytes = 0
        self.frames = 0
        self.keyframes = 0
        self.last_buffer_mono: float | None = None
        self.started_mono = time.monotonic()
        self.last_key_pts: int | None = None
        self.gop_s: float | None = None
        self.persist_latency_ms: list[float] = []
        self.acks = {"BUFFERING": 0, "RECEIVED": 0, "PERSISTED": 0, "ERROR": 0}
        self.kvs_errors = 0
        self.no_dts = 0
        self.pipeline = self._build()

    # --- 組み立て ---------------------------------------------------------
    def _make(self, factory: str, name: str | None = None, **props) -> Gst.Element:
        el = Gst.ElementFactory.make(factory, name)
        if el is None:
            raise RuntimeError(f"GStreamer の要素 {factory} が見つからない")
        for k, v in props.items():
            if v is None:
                continue
            el.set_property(k.replace("_", "-"), v)
        return el

    def _build(self) -> Gst.Pipeline:
        s = self.spec
        codec = s.codec_lower
        pipe = Gst.Pipeline.new(f"cam-{s.camera_id}")
        src = self._make("rtspsrc", "src", location=s.rtsp_url, latency=s.latency_ms,
                         protocols=s.protocols_flag())
        if s.username:
            src.set_property("user-id", s.username)
            src.set_property("user-pw", s.password)
        self.depay = self._make(f"rtp{codec}depay", "depay")
        parse = self._make(f"{codec}parse", "parse", config_interval=-1)
        fmt = "hvc1" if codec == "h265" else "avc"
        caps = self._make("capsfilter", "caps",
                          caps=Gst.Caps.from_string(f"video/x-{codec},stream-format={fmt},alignment=au"))
        # RTP から取り出したフレームには DTS がない。kvssink は「HEADER フラグ付き(VPS/SPS/PPS を含む
        # キーフレーム)で DTS が無効」なバッファを捨てるため、そのままではキーフレームが全部落ちて
        # フラグメントが 1 つもできない。h26Xtimestamper で DTS を推定して付ける
        stamper = self._make(f"{codec}timestamper", "stamper")
        queue = self._make("queue", "q", max_size_buffers=0, max_size_bytes=0,
                           max_size_time=5 * Gst.SECOND, leaky=2)
        elems = [self.depay, parse, caps, stamper, queue]
        if s.sink == "kvs":
            sink = self._make("kvssink", "sink", stream_name=s.stream_name, aws_region=s.region,
                              retention_period=s.retention_hours, storage_size=s.storage_size_mb,
                              log_config=s.log_config, framerate=s.framerate or 25,
                              key_frame_fragmentation=True, fragment_duration=2000,
                              restart_on_error=True)
            iot = Gst.Structure.new_from_string(s.iot_certificate_structure())
            if iot is None:
                raise RuntimeError("iot-certificate の構造体を作れない")
            sink.set_property("iot-certificate", iot)
            sink.connect("stream-error", self._on_stream_error)
            sink.connect("fragment-ack", self._on_fragment_ack)
            elems.append(sink)
        elif s.sink == "file":
            mux = self._make("matroskamux", "mux", streamable=True)
            sink = self._make("filesink", "sink", location=s.output_path, sync=False)
            elems += [mux, sink]
        else:
            sink = self._make("fakesink", "sink", sync=False)
            elems.append(sink)

        pipe.add(src)
        for e in elems:
            pipe.add(e)
        for a, b in zip(elems, elems[1:], strict=False):
            if not a.link(b):
                raise RuntimeError(f"{a.get_name()} → {b.get_name()} をリンクできない")
        src.connect("pad-added", self._on_pad_added)
        stamper.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)
        caps.get_static_pad("src").connect("notify::caps", self._on_caps)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        return pipe

    # --- コールバック -----------------------------------------------------
    def _on_pad_added(self, _src, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps()
        st = caps.get_structure(0) if caps else None
        media = st.get_string("media") if st else None
        enc = (st.get_string("encoding-name") or "") if st else ""
        if media and media != "video":
            return  # 音声は送らない
        sinkpad = self.depay.get_static_pad("sink")
        if sinkpad.is_linked():
            return
        want = self.spec.codec_upper
        got = enc.upper().replace("HEVC", "H265")
        if got and got != want:
            emit({"type": "error", "message": f"RTSP のコーデックが {got}(期待は {want})"})
            self.exit_code = 3
            GLib.idle_add(self.loop.quit)
            return
        ret = pad.link(sinkpad)
        if ret != Gst.PadLinkReturn.OK:
            emit({"type": "error", "message": f"rtspsrc のパッドをリンクできない: {ret}"})

    def _on_caps(self, pad: Gst.Pad, _pspec) -> None:
        caps = pad.get_current_caps()
        if not caps:
            return
        st = caps.get_structure(0)
        ok_w, w = st.get_int("width")
        ok_h, h = st.get_int("height")
        ok_f, fn, fd = st.get_fraction("framerate")
        emit({"type": "caps", "caps": caps.to_string()[:500],
              "codec": self.spec.codec_upper,
              "width": w if ok_w else None, "height": h if ok_h else None,
              "framerate": (fn / fd) if ok_f and fd else None,
              "has_codec_data": st.has_field("codec_data"),
              "profile": st.get_string("profile")})

    def _on_buffer(self, _pad, info: Gst.PadProbeInfo):
        buf = info.get_buffer()
        self.bytes += buf.get_size()
        self.frames += 1
        self.last_buffer_mono = time.monotonic()
        if buf.dts == Gst.CLOCK_TIME_NONE:
            self.no_dts += 1  # kvssink に捨てられ得るバッファ
        if not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
            self.keyframes += 1
            if buf.pts != Gst.CLOCK_TIME_NONE:
                if self.last_key_pts is not None and buf.pts > self.last_key_pts:
                    self.gop_s = (buf.pts - self.last_key_pts) / Gst.SECOND
                self.last_key_pts = buf.pts
        return Gst.PadProbeReturn.OK

    def _on_stream_error(self, _sink, code: int) -> None:
        self.kvs_errors += 1
        emit({"type": "kvs_error", "code": f"0x{code:08x}"})

    def _on_fragment_ack(self, _sink, ptr) -> None:
        try:
            ack = _FragmentAck.from_address(_pointer_address(ptr))
            if ack.version != 0 or ack.ack_type not in ACK_TYPES:
                raise ValueError(f"FragmentAck として読めない(version={ack.version}, type={ack.ack_type})")
            kind = ACK_TYPES[ack.ack_type]
        except Exception as e:  # noqa: BLE001  PyGObject の版によってはポインタを取れない
            emit({"type": "ack_parse_error", "message": str(e)})
            return
        if kind in self.acks:
            self.acks[kind] += 1
        if kind == "PERSISTED":
            # timestamp はフラグメントの先頭のタイムコード(絶対時刻、ミリ秒)
            lat = time.time() * 1000 - ack.timestamp
            if 0 < lat < 3_600_000:
                self.persist_latency_ms.append(lat)
        if kind in ("PERSISTED", "ERROR"):
            emit({"type": "ack", "ack": kind, "fragment_ts_ms": ack.timestamp,
                  "seq": ack.sequence_number.decode(errors="replace"), "result": ack.result})

    def _on_message(self, _bus, msg: Gst.Message) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            emit({"type": "error", "source": msg.src.get_name() if msg.src else "",
                  "message": err.message, "debug": (dbg or "")[:500]})
            self.exit_code = 2
            self.loop.quit()
        elif t == Gst.MessageType.EOS:
            emit({"type": "eos"})
            self.exit_code = 4
            self.loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipeline:
            _old, new, _pending = msg.parse_state_changed()
            emit({"type": "state", "state": Gst.Element.state_get_name(new)})
        elif t == Gst.MessageType.WARNING:
            err, _dbg = msg.parse_warning()
            emit({"type": "warning", "source": msg.src.get_name() if msg.src else "", "message": err.message})

    def _tick(self) -> bool:
        now = time.monotonic()
        lat = self.persist_latency_ms
        emit({
            "type": "stats",
            "bytes": self.bytes, "frames": self.frames, "keyframes": self.keyframes,
            "gop_s": self.gop_s,
            "uptime_s": round(now - self.started_mono, 1),
            "last_buffer_age_s": None if self.last_buffer_mono is None else round(now - self.last_buffer_mono, 2),
            "acks": dict(self.acks), "kvs_errors": self.kvs_errors, "no_dts": self.no_dts,
            "persist_latency_ms": (
                {"n": len(lat), "avg": round(sum(lat) / len(lat)), "max": round(max(lat)), "min": round(min(lat))}
                if lat else None
            ),
        })
        self.persist_latency_ms = []
        # 一定時間バッファが来なければ再起動してもらう(rtspsrc が固まる対策)
        ref = self.last_buffer_mono if self.last_buffer_mono is not None else self.started_mono
        limit = self.spec.stall_timeout_s * (1 if self.last_buffer_mono is not None else 2)
        if now - ref > limit:
            emit({"type": "error", "message": f"{limit:.0f} 秒バッファが来ない"})
            self.exit_code = 5
            self.loop.quit()
            return False
        return True

    def run(self) -> int:
        GLib.timeout_add(int(self.spec.stats_interval_s * 1000), self._tick)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            emit({"type": "error", "message": "PLAYING にできない"})
            self.exit_code = 2
        else:
            self.loop.run()
        self._tick_final()
        self.pipeline.set_state(Gst.State.NULL)
        # 接続できずに終わったときの空ファイルを残さない
        if self.spec.sink == "file" and self.bytes == 0:
            try:
                os.remove(self.spec.output_path)
            except OSError:
                pass
        return self.exit_code

    def _tick_final(self) -> None:
        emit({"type": "stats", "final": True, "bytes": self.bytes, "frames": self.frames,
              "keyframes": self.keyframes, "acks": dict(self.acks)})

    def _on_signal(self) -> bool:
        # 停止要求: kvssink にバッファを吐き出させるため EOS を送ってから止める
        emit({"type": "stopping"})
        self.exit_code = 0
        self.pipeline.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(5, self.loop.quit)
        self._stopping = True
        return False


def main() -> int:
    Gst.init(None)
    spec = PipelineSpec.from_json(sys.argv[1])
    try:
        runner = Runner(spec)
    except Exception as e:  # noqa: BLE001
        emit({"type": "error", "message": f"パイプラインを組めない: {e}"})
        return 2
    code = runner.run()
    if getattr(runner, "_stopping", False):
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())

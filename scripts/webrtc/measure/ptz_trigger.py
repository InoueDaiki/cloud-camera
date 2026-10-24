"""エッジのコンテナの中で PTZ を 1 回動かし、送った時刻を JSON で出す(PTZ → 映像反映の計測用)。

    docker cp ptz_trigger.py cc-webrtc-edge-1:/tmp/ && docker exec cc-webrtc-edge-1 python3 /tmp/ptz_trigger.py zoom

既存のエッジの PTZ クライアント(cc_edge.ptz.PtzClient)をそのまま使う。接続(GetServices、GetProfiles)は先に済ませ、
送る直前の壁時計(epoch ミリ秒)を t_send_ms とする。MQTT や API を通した遅れは含まない(本番では + IoT Core の配送)。

    home : ホーム位置(0, 0, 0)に戻し、止まるまで待つ
    zoom : 最大倍率へ AbsoluteMove(全体が拡大されるので、画面のどこでも変化がわかる)
"""

import json
import sys
import time

from cc_edge.ptz import PtzClient

URL = "http://emulator:8080/onvif/device_service"


def main() -> None:
    op = sys.argv[1]
    cli = PtzClient(URL, "admin", "admin")
    if not cli.connect():
        sys.exit("PTZ に対応していない")
    t_send = time.time()
    if op == "home":
        cli.goto_home()
    else:
        cli.absolute_move(None, None, 1.0)
    t_ack = time.time()
    out = {"op": op, "t_send_ms": round(t_send * 1000), "soap_ms": round((t_ack - t_send) * 1000)}
    if op == "home":
        while cli.get_status().move_status == "MOVING":
            time.sleep(0.1)
        out["t_idle_ms"] = round(time.time() * 1000)
    print(json.dumps(out))


if __name__ == "__main__":
    main()

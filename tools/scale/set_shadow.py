"""cc-tmp-scale-gw-001 のシャドウ desired に 10 カメラを書く(streaming を引数で切り替え)。"""
import json
import sys

import boto3

streaming = (sys.argv[1] if len(sys.argv) > 1 else "true") == "true"
cams = {
    f"cam-{i:03d}": {
        "onvif_url": f"http://emulator:{8080 + i - 1}/onvif/device_service",
        "username": "admin",
        "password": "admin",
        "profile_token": None,
        "streaming": streaming,
    }
    for i in range(1, 11)
}
ep = boto3.client("iot").describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
d = boto3.client("iot-data", endpoint_url=f"https://{ep}")
d.update_thing_shadow(thingName="cc-tmp-scale-gw-001", payload=json.dumps({"state": {"desired": {"cameras": cams}}}).encode())
doc = json.loads(d.get_thing_shadow(thingName="cc-tmp-scale-gw-001")["payload"].read())
state_only = json.dumps({"state": doc.get("state")}, separators=(",", ":"))
print("state bytes:", len(state_only), "full doc bytes:", len(json.dumps(doc, separators=(",", ":"))))

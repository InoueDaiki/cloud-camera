"""cc-tmp-scale-gw-001 のシャドウの大きさ(state の desired / reported、metadata を除く)を測る。"""
import json

import boto3

ep = boto3.client("iot").describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
d = boto3.client("iot-data", endpoint_url=f"https://{ep}")
doc = json.loads(d.get_thing_shadow(thingName="cc-tmp-scale-gw-001")["payload"].read())
st = doc.get("state", {})
c = lambda x: len(json.dumps(x, separators=(",", ":"), ensure_ascii=False).encode())  # noqa: E731
print("state", c(st), "desired", c(st.get("desired")), "reported", c(st.get("reported")), "delta", c(st.get("delta")))
rep = (st.get("reported") or {}).get("cameras") or {}
for cam, v in sorted(rep.items())[:1]:
    print("reported per camera", cam, c(v), json.dumps(v, ensure_ascii=False)[:600])
print("reported top-level (non cameras)", c({k: v for k, v in (st.get("reported") or {}).items() if k != "cameras"}))

"""CloudTrail で KVS のコントロールプレーン API(GetDataEndpoint、DescribeStream)の呼び出しを数える。

python -I trail.py <start> <end>
呼び出し元(Lambda のロール / エッジのロール)ごと、API ごと、ストリームごとに数える。cc-tmp-scale のストリームだけ。
"""
import collections
import json
import sys
from datetime import datetime

import boto3

ct = boto3.client("cloudtrail")
s = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
e = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
cnt = collections.Counter()
for name in ("GetDataEndpoint", "DescribeStream"):
    kw = dict(LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": name}], StartTime=s, EndTime=e, MaxResults=50)
    while True:
        r = ct.lookup_events(**kw)
        for ev in r["Events"]:
            d = json.loads(ev["CloudTrailEvent"])
            stream = ((d.get("requestParameters") or {}).get("streamName") or "")
            if not stream.startswith("cc-tmp-scale-"):
                continue
            who = (d.get("userIdentity") or {}).get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "?")
            api = (d.get("requestParameters") or {}).get("aPIName") or (d.get("requestParameters") or {}).get("apiName") or ""
            cnt[(name, who, api)] += 1
        if not r.get("NextToken"):
            break
        kw["NextToken"] = r["NextToken"]
for k, v in sorted(cnt.items()):
    print(*k, v)
print("minutes", (e - s).total_seconds() / 60)

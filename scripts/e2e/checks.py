"""E2E の各チェック(scripts/e2e/run.sh から 1 段階ずつ呼ぶ)。

    uv run --no-project --with boto3 python scripts/e2e/checks.py <段階> [--timeout 秒]

段階の結果は環境変数 E2E_RESULTS の TSV に 1 行追記する(段階、PASS/FAIL/SKIP、秒、詳細)。
段階をまたいで使う値(S3 に退避された区間など)は E2E_STATE の JSON に残す。
Terraform の出力は E2E_TF_JSON(terraform output -json の保存結果)から読む。

終了コード: 0 = PASS、2 = SKIP、1 = FAIL
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.types import TypeDeserializer

CAMERA = os.environ.get("E2E_CAMERA", "cam-001")
GATEWAY = os.environ.get("E2E_GATEWAY", "gw-001")
FFPROBE_IMAGE = os.environ.get("E2E_FFPROBE_IMAGE", "cloud-camera/onvif-emulator:latest")
# エミュレーターの ONVIF(compose のネットワーク内から見た URL)
ONVIF_URL = os.environ.get("E2E_ONVIF_URL", "http://emulator:8080/onvif/device_service")


class Skip(Exception):
    pass


class Fail(Exception):
    pass


# ---------------------------------------------------------------- 共通


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def tf() -> dict:
    data = json.loads(Path(os.environ["E2E_TF_JSON"]).read_text())
    return {k: v["value"] for k, v in data.items()}


def state_path() -> Path:
    return Path(os.environ["E2E_STATE"])


def load_state() -> dict:
    p = state_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(**kw) -> None:
    s = load_state()
    s.update(kw)
    state_path().write_text(json.dumps(s, indent=2, ensure_ascii=False))


def dummies() -> set[str]:
    """ダミーの zip でデプロイした Lambda(該当チェックは SKIP)"""
    return set(filter(None, os.environ.get("E2E_DUMMY", "").split(",")))


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def http(method: str, path: str, body: dict | None = None, timeout: int = 40) -> tuple[int, bytes, dict]:
    """CloudFront 経由で API を呼ぶ(ビューワーと同じ経路)"""
    url = path if path.startswith("http") else tf()["cloudfront_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def api_json(method: str, path: str, body: dict | None = None, timeout: int = 40) -> dict:
    status, raw, _ = http(method, path, body, timeout)
    if status != 200:
        raise Fail(f"{method} {path} → HTTP {status}: {raw[:300]!r}")
    return json.loads(raw)


def poll(fn, timeout: float, interval: float = 5.0, what: str = ""):
    """fn() が真の値を返すまで待つ。最後の例外(Fail)か None を返したら失敗"""
    deadline = time.time() + timeout
    last = None
    while True:
        try:
            v = fn()
            if v:
                return v
        except Fail as e:
            last = e
        if time.time() >= deadline:
            raise Fail(f"{what} が {int(timeout)} 秒以内に満たされない" + (f"(最後: {last})" if last else ""))
        time.sleep(interval)


def ffprobe(target: str) -> dict:
    """ffprobe(エミュレーターのイメージのもの)で最初の映像ストリームを調べる。target は URL"""
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "ffprobe", FFPROBE_IMAGE,
        "-v", "error", "-allowed_extensions", "ALL",
        "-show_entries", "stream=codec_name,codec_tag_string,width,height:format=duration,format_name",
        "-select_streams", "v:0", "-of", "json", target,
    ]
    # HLS 以外では -allowed_extensions は使えない
    if ".m3u8" not in target and "getHLS" not in target:
        cmd.remove("-allowed_extensions")
        cmd.remove("ALL")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise Fail(f"ffprobe 失敗: {p.stderr.strip()[-400:]}")
    out = json.loads(p.stdout)
    streams = out.get("streams") or []
    if not streams:
        raise Fail(f"ffprobe: 映像ストリームがない ({p.stderr.strip()[-200:]})")
    return {**streams[0], **out.get("format", {})}


def kvs_list_fragments(stream: str, start: datetime, end: datetime) -> list[dict]:
    kv = boto3.client("kinesisvideo")
    ep = kv.get_data_endpoint(StreamName=stream, APIName="LIST_FRAGMENTS")["DataEndpoint"]
    am = boto3.client("kinesis-video-archived-media", endpoint_url=ep)
    frags: list[dict] = []
    kw = {
        "StreamName": stream,
        "MaxResults": 1000,
        "FragmentSelector": {
            "FragmentSelectorType": "PRODUCER_TIMESTAMP",
            "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
        },
    }
    while True:
        r = am.list_fragments(**kw)
        frags += r.get("Fragments", [])
        if not r.get("NextToken"):
            return frags
        kw = {"StreamName": stream, "MaxResults": 1000, "NextToken": r["NextToken"]}


def camera_item(cams: dict) -> dict:
    for c in cams.get("cameras", []):
        if c.get("camera_id") == CAMERA:
            return c
    raise Fail(f"/api/cameras に {CAMERA} がない")


# ---------------------------------------------------------------- 段階


def stage_shadow(a) -> str:
    """シャドウの desired に cam-001(ONVIF はエミュレーター)を書く"""
    t = tf()
    thing = t["gateways"][GATEWAY]["thing_name"]
    payload = {"state": {"desired": {"cameras": {CAMERA: {
        "onvif_url": ONVIF_URL, "username": "admin", "password": "admin",
        "profile_token": None, "streaming": True,
    }}}}}
    iot = boto3.client("iot-data", endpoint_url=f"https://{t['iot_data_endpoint']}")
    r = iot.update_thing_shadow(thingName=thing, payload=json.dumps(payload).encode())
    ver = json.loads(r["payload"].read()).get("version")
    return f"{thing} desired.cameras.{CAMERA} を設定(version {ver}、{ONVIF_URL})"


def stage_kvs(a) -> str:
    """KVS に断片が届き、ProducerTimestamp が壁時計に近い"""
    stream = tf()["kvs_streams"][CAMERA] if isinstance(tf()["kvs_streams"], dict) else f"{tf()['project']}-{CAMERA}"

    def check():
        now = utcnow()
        frags = kvs_list_fragments(stream, now - timedelta(minutes=2), now + timedelta(minutes=1))
        if len(frags) < 5:
            raise Fail(f"断片 {len(frags)} 本")
        # 前回の実行の断片が残っていることがあるので、今届いているか(最新が 20 秒以内か)まで待つ
        lag = (utcnow() - max(f["ProducerTimestamp"] for f in frags)).total_seconds()
        if lag > 20:
            raise Fail(f"最新の断片が {lag:.0f} 秒前")
        return frags

    frags = poll(check, a.timeout, 5, "KVS の断片 5 本以上")
    now = utcnow()
    latest = max(frags, key=lambda f: f["ProducerTimestamp"])
    lag = (now - latest["ProducerTimestamp"]).total_seconds()
    skew = (latest["ServerTimestamp"] - latest["ProducerTimestamp"]).total_seconds()
    lens = sorted(f["FragmentLengthInMilliseconds"] for f in frags)
    future = [f for f in frags if f["ProducerTimestamp"] > now + timedelta(seconds=5)]
    detail = (f"{stream}: 直近 2 分で {len(frags)} 本、断片長の中央値 {lens[len(lens) // 2]}ms、"
              f"最新の Producer と壁時計の差 {lag:.1f}s、Server−Producer {skew:.1f}s")
    if lag > 20 or future:
        raise Fail(detail + "(ProducerTimestamp が壁時計から離れている)")
    save_state(kvs_first_seen=iso(now))
    return detail


def stage_cameras(a) -> str:
    """GET /api/cameras で online=true、reported.streaming=true"""
    def check():
        c = camera_item(api_json("GET", "/api/cameras"))
        rep = c.get("reported") or {}
        if c.get("online") is True and rep.get("streaming") is True:
            return c
        raise Fail(f"online={c.get('online')} reported={json.dumps(rep, ensure_ascii=False)[:200]}")

    c = poll(check, a.timeout, 5, "online と reported.streaming")
    rep = c["reported"]
    return (f"online=true、streaming=true、codec={rep.get('codec')}、resolution={rep.get('resolution')}、"
            f"rtsp_url={rep.get('rtsp_url')}")


def stage_api_kvs(a) -> str:
    """timeline の kvs 範囲と kvs-hls(LIVE / ON_DEMAND)。ffprobe で hevc を確認"""
    now = utcnow()
    frm = now - timedelta(minutes=10)
    tl = api_json("GET", f"/api/cameras/{CAMERA}/timeline?from={iso(frm)}&to={iso(now)}")
    kvs = [r for r in tl.get("recorded", []) if r.get("source") == "kvs"]
    if not kvs:
        raise Fail(f"timeline に kvs の範囲がない: {json.dumps(tl)[:400]}")
    kvs_sec = sum((parse_iso(r["end"]) - parse_iso(r["start"])).total_seconds() for r in kvs)

    out = [f"timeline: kvs {len(kvs)} 範囲 計 {kvs_sec:.0f}s(video_processed_until={tl.get('video_processed_until')})"]
    for mode in ("LIVE", "ON_DEMAND"):
        q = f"mode={mode}"
        if mode == "ON_DEMAND":
            q += f"&from={iso(now - timedelta(minutes=2))}&to={iso(now - timedelta(seconds=10))}"
        r = api_json("GET", f"/api/cameras/{CAMERA}/kvs-hls?{q}")
        url = r.get("url")
        if not url:
            raise Fail(f"kvs-hls {mode}: url がない {r}")
        status, master, _ = http("GET", url)
        text = master.decode(errors="replace")
        if status != 200 or "#EXTM3U" not in text:
            raise Fail(f"kvs-hls {mode}: マスタープレイリストが読めない HTTP {status}")
        codecs = [ln.split("CODECS=")[1].split('"')[1] for ln in text.splitlines() if "CODECS=" in ln]
        probe = ffprobe(url)
        if probe.get("codec_name") != "hevc":
            raise Fail(f"kvs-hls {mode}: ffprobe の codec が {probe.get('codec_name')}")
        out.append(f"kvs-hls {mode}: CODECS={codecs[:1]}、ffprobe {probe['codec_name']}/{probe.get('codec_tag_string')} "
                   f"{probe.get('width')}x{probe.get('height')}")
    return "; ".join(out)


def _sections(table: str, since: datetime) -> list[dict]:
    ddb = boto3.client("dynamodb")
    r = ddb.query(
        TableName=table,
        KeyConditionExpression="camera_id = :c AND sk >= :s",
        ExpressionAttributeValues={":c": {"S": CAMERA}, ":s": {"S": iso(since)}},
    )
    items = []
    for it in r.get("Items", []):
        if it["sk"]["S"].startswith("CURSOR#"):
            continue
        items.append(TypeDeserializer().deserialize({"M": it}))
    return items


def stage_archive(a) -> str:
    """DynamoDB の区間の件(video_status=DONE)と S3 の clip.mp4 / index.m3u8 / sprite.jpg"""
    if "video_archiver" in dummies():
        raise Skip("video_archiver はダミー")
    t = tf()
    since = parse_iso(load_state().get("kvs_first_seen", iso(utcnow() - timedelta(minutes=15)))) - timedelta(minutes=2)

    def check():
        done = [s for s in _sections(t["state_table"], since) if s.get("video_status") == "DONE"]
        # 1 分まるごと録画がある区間(断片 55 本以上)を選ぶ。最初の区間は途中から始まる
        full = [s for s in done if int(s.get("fragment_count", 0)) >= 55]
        if full:
            return full
        raise Fail(f"DONE の区間 {len(done)} 件(1 分まるごとのものなし)")

    secs = poll(check, a.timeout, 15, "video_status=DONE の区間")
    sec = secs[0]
    files = sec.get("files") or {}
    for k in ("clip", "playlist", "sprite"):
        if k not in files:
            raise Fail(f"区間 {sec['sk']} の files に {k} がない: {files}")
    s3 = boto3.client("s3")
    sizes = {k: s3.head_object(Bucket=t["media_bucket"], Key=v)["ContentLength"] for k, v in files.items()}
    cf = t["cloudfront_url"].rstrip("/")
    clip = ffprobe(f"{cf}/media/{files['clip']}")
    if clip.get("codec_name") != "hevc":
        raise Fail(f"clip.mp4 の codec が {clip.get('codec_name')}")
    pl = ffprobe(f"{cf}/media/{files['playlist']}")
    if pl.get("codec_name") != "hevc":
        raise Fail(f"index.m3u8 の codec が {pl.get('codec_name')}")
    sprite = ffprobe(f"{cf}/media/{files['sprite']}")
    if (sprite.get("width"), sprite.get("height")) != (1600, 540):
        raise Fail(f"sprite.jpg が {sprite.get('width')}x{sprite.get('height')}(期待 1600x540)")
    segs = sec.get("segments") or []
    save_state(s3_section=sec["sk"], s3_segments=[[int(x) for x in s] for s in segs])
    return (f"区間 {sec['sk']}(DONE {len(secs)} 件): fragments={sec.get('fragment_count')} bytes={sec.get('bytes')} "
            f"segments={len(segs)} video_duration_ms={sec.get('video_duration_ms')}; "
            f"clip.mp4 {sizes['clip']}B hevc {float(clip.get('duration', 0)):.1f}s; "
            f"index.m3u8 hevc {float(pl.get('duration', 0)):.1f}s; sprite.jpg 1600x540")


def stage_detect(a) -> str:
    """detect_status=DONE、detections の Parquet、Athena で行が引ける"""
    if "detector" in dummies():
        raise Skip("detector はダミー")
    t = tf()
    since = parse_iso(load_state().get("kvs_first_seen", iso(utcnow() - timedelta(minutes=15)))) - timedelta(minutes=2)

    def check():
        done = [s for s in _sections(t["state_table"], since) if s.get("detect_status") == "DONE"]
        # 検出 0 件の区間では Parquet に行が入らないので、検出のある区間を待つ
        hit = [s for s in done if int(s.get("detect_count", 0)) > 0]
        if hit:
            return done, hit
        raise Fail(f"detect_status=DONE {len(done)} 件、検出のある区間なし "
                   f"(detect_count={[int(s.get('detect_count', 0)) for s in done[:5]]})")

    done, hit = poll(check, a.timeout, 15, "検出のある detect_status=DONE の区間")
    dt = parse_iso(hit[0]["sk"]).strftime("%Y-%m-%d")
    key = f"detections/dt={dt}/{CAMERA}.parquet"
    head = boto3.client("s3").head_object(Bucket=t["detections_bucket"], Key=key)
    ath = boto3.client("athena")
    q = ath.start_query_execution(
        QueryString=f"SELECT label, count(*) AS n FROM detections WHERE dt = '{dt}' AND camera_id = '{CAMERA}' GROUP BY label ORDER BY n DESC",
        QueryExecutionContext={"Database": t["glue_database"]},
        WorkGroup=t["athena_workgroup"],
    )["QueryExecutionId"]
    while True:
        st = ath.get_query_execution(QueryExecutionId=q)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if st["State"] != "SUCCEEDED":
        raise Fail(f"Athena: {st}")
    rows = ath.get_query_results(QueryExecutionId=q)["ResultSet"]["Rows"][1:]
    labels = {r["Data"][0].get("VarCharValue"): int(r["Data"][1]["VarCharValue"]) for r in rows}
    if not labels:
        raise Fail(f"Athena で行が引けない(Parquet {head['ContentLength']}B)")
    return (f"DONE {len(done)} 件、{key} {head['ContentLength']}B、Athena: {labels}、"
            f"detect_count={[int(s.get('detect_count', 0)) for s in done[:5]]}")


def stage_api_s3(a) -> str:
    """timeline の s3 範囲と vod.m3u8(ffprobe で読める)"""
    if "video_archiver" in dummies():
        raise Skip("video_archiver はダミー")
    st = load_state()
    if "s3_section" not in st:
        raise Fail("S3 に退避された区間が分からない(archive の段階が失敗)")
    sec = parse_iso(st["s3_section"])
    now = utcnow()
    tl = api_json("GET", f"/api/cameras/{CAMERA}/timeline?from={iso(now - timedelta(minutes=30))}&to={iso(now)}")
    s3r = [r for r in tl.get("recorded", []) if r.get("source") == "s3"]
    kvs = [r for r in tl.get("recorded", []) if r.get("source") == "kvs"]
    if not s3r:
        raise Fail(f"timeline に s3 の範囲がない: {json.dumps(tl)[:400]}")
    minutes = tl.get("minutes") or []
    frm, to = sec, sec + timedelta(minutes=1)
    path = f"/api/cameras/{CAMERA}/vod.m3u8?from={iso(frm)}&to={iso(to)}"
    status, raw, _ = http("GET", path)
    text = raw.decode(errors="replace")
    if status != 200 or "#EXTM3U" not in text:
        raise Fail(f"vod.m3u8 HTTP {status}: {text[:300]}")
    tags = {k: text.count(k) for k in ("#EXT-X-MAP", "#EXT-X-PROGRAM-DATE-TIME", "#EXTINF", "#EXT-X-BYTERANGE")}
    probe = ffprobe(tf()["cloudfront_url"].rstrip("/") + path)
    if probe.get("codec_name") != "hevc":
        raise Fail(f"vod.m3u8 の codec が {probe.get('codec_name')}")
    # 処理済み位置の前後(S3 → KVS)で隙間がないか
    gap = None
    if s3r and kvs:
        gap = (parse_iso(kvs[0]["start"]) - parse_iso(s3r[-1]["end"])).total_seconds()
    return (f"timeline: s3 {len(s3r)} 範囲・kvs {len(kvs)} 範囲・minutes {len(minutes)}・failed {len(tl.get('failed') or [])}、"
            f"s3→kvs の継ぎ目 {gap}s; vod.m3u8 {tags}、ffprobe hevc {float(probe.get('duration', 0)):.1f}s")


def stage_search(a) -> str:
    """POST /api/search(Bedrock → Athena)"""
    q = os.environ.get("E2E_QUESTION", "直近10分で人は映った?")
    t0 = time.time()
    r = api_json("POST", "/api/search", {"question": q}, timeout=60)
    el = time.time() - t0
    ans = (r.get("answer") or "").replace("\n", " ")
    if not ans:
        raise Fail(f"answer が空: {r}")
    u = r.get("usage") or {}
    detail = (f"「{q}」→「{ans[:80]}」 hits={len(r.get('hits') or [])} sql={'あり' if r.get('sql') else 'なし'} "
              f"tokens={u.get('total_tokens')} {el:.1f}s")
    if "detector" in dummies():
        # 検出結果がないので回答の中身は確かめない(API が答えられることだけ)
        return detail + "(detector はダミーなので内容は未検証)"
    if not r.get("sql"):
        raise Fail(detail + "(SQL が実行されていない)")
    return detail


def stage_stop(a) -> str:
    """PUT streaming=false で reported.streaming が false になる"""
    r = api_json("PUT", f"/api/cameras/{CAMERA}/streaming", {"streaming": False})

    def check():
        c = camera_item(api_json("GET", "/api/cameras"))
        if (c.get("reported") or {}).get("streaming") is False:
            return c
        raise Fail(f"reported.streaming={(c.get('reported') or {}).get('streaming')}")

    t0 = time.time()
    poll(check, a.timeout, 2, "reported.streaming=false")
    return f"PUT → version {r.get('version')}、reported.streaming=false まで {time.time() - t0:.0f}s"


def stage_residue(a) -> str:
    """destroy 後の残骸チェック(削除直後はタグ検索などに少し残ることがあるので待つ)"""
    return poll(lambda: _residue() or None, a.timeout, 20, "残骸がなくなる")


def _residue() -> str:
    project = os.environ["E2E_PROJECT"]
    if project.startswith("cloud-camera"):
        raise Fail("cloud-camera* は対象外")
    found: list[str] = []
    tag = boto3.client("resourcegroupstaggingapi")
    for page in tag.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project", "Values": [project]}]):
        found += [f"tag:{m['ResourceARN']}" for m in page["ResourceTagMappingList"]]
    found += [f"s3:{b['Name']}" for b in boto3.client("s3").list_buckets()["Buckets"] if b["Name"].startswith(project + "-")]
    logs = boto3.client("logs")
    for prefix in (f"/aws/lambda/{project}-", f"/aws/apigateway/{project}", f"/aws/vendedlogs/{project}", project):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            found += [f"logs:{g['logGroupName']}" for g in page["logGroups"]]
    kv = boto3.client("kinesisvideo")
    streams = kv.list_streams(StreamNameCondition={"ComparisonOperator": "BEGINS_WITH", "ComparisonValue": project + "-"})
    found += [f"kvs:{s['StreamName']} ({s['Status']})" for s in streams.get("StreamInfoList", [])]
    iot = boto3.client("iot")
    found += [f"iot-thing:{t['thingName']}" for t in iot.list_things()["things"] if t["thingName"].startswith(project + "-")]
    found += [f"iot-policy:{p['policyName']}" for p in iot.list_policies()["policies"] if p["policyName"].startswith(project + "-")]
    found += [f"iot-role-alias:{r}" for r in iot.list_role_aliases()["roleAliases"] if r.startswith(project + "-")]
    # 証明書は名前を持たないので、run.sh が deploy 時に控えた ID がまだあるかで見る
    for cid in load_state().get("certificate_ids", []):
        try:
            d = iot.describe_certificate(certificateId=cid)["certificateDescription"]
            found.append(f"iot-cert:{cid} ({d['status']})")
        except iot.exceptions.ResourceNotFoundException:
            pass
    sch = boto3.client("scheduler")
    found += [f"scheduler-group:{g['Name']}" for g in sch.list_schedule_groups(NamePrefix=project)["ScheduleGroups"]]
    ddb = boto3.client("dynamodb")
    found += [f"ddb:{n}" for n in ddb.list_tables()["TableNames"] if n.startswith(project + "-")]
    glue = boto3.client("glue")
    dbname = project.replace("-", "_")
    found += [f"glue:{d['Name']}" for d in glue.get_databases()["DatabaseList"] if d["Name"] == dbname]
    try:
        boto3.client("athena").get_work_group(WorkGroup=project)
        found.append(f"athena-wg:{project}")
    except Exception:
        pass
    iam = boto3.client("iam")
    for page in iam.get_paginator("list_roles").paginate():
        found += [f"iam-role:{r['RoleName']}" for r in page["Roles"] if r["RoleName"].startswith(project + "-")]
    cf = boto3.client("cloudfront")
    for d in (cf.list_distributions().get("DistributionList", {}).get("Items") or []):
        if project in (d.get("Comment") or ""):
            found.append(f"cloudfront:{d['Id']}")
    if found:
        raise Fail("残骸: " + ", ".join(found))
    return "タグ、S3、ロググループ、KVS、IoT(Thing・ポリシー・ロールエイリアス・証明書)、スケジュール、DynamoDB、Glue、Athena、IAM、CloudFront に残りなし"


def stage_certs(a) -> str:
    """deploy 直後に証明書 ID を控える(残骸チェック用)"""
    t = tf()
    iot = boto3.client("iot")
    ids = []
    for gw, g in t["gateways"].items():
        for p in iot.list_thing_principals(thingName=g["thing_name"])["principals"]:
            if ":cert/" in p:
                ids.append(p.split("/")[-1])
    save_state(certificate_ids=ids)
    return f"証明書 {len(ids)} 枚"


STAGES = {
    "shadow": stage_shadow,
    "kvs": stage_kvs,
    "cameras": stage_cameras,
    "api-kvs": stage_api_kvs,
    "archive": stage_archive,
    "detect": stage_detect,
    "api-s3": stage_api_s3,
    "search": stage_search,
    "stop": stage_stop,
    "residue": stage_residue,
    "certs": stage_certs,
}


def record(name: str, status: str, secs: float, detail: str) -> None:
    line = "\t".join([name, status, f"{secs:.0f}", detail.replace("\t", " ").replace("\n", " ")])
    if os.environ.get("E2E_RESULTS"):
        with open(os.environ["E2E_RESULTS"], "a") as f:
            f.write(line + "\n")
    print(f"[{status}] {name}: {detail}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=sorted(STAGES))
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--name", help="結果の表に出す名前(既定は段階名)")
    a = ap.parse_args()
    t0 = time.time()
    name = a.name or a.stage
    try:
        detail = STAGES[a.stage](a)
    except Skip as e:
        record(name, "SKIP", time.time() - t0, str(e))
        return 2
    except Exception as e:  # noqa: BLE001 — 何が起きても表に残す
        record(name, "FAIL", time.time() - t0, f"{type(e).__name__}: {e}" if not isinstance(e, Fail) else str(e))
        return 1
    record(name, "PASS", time.time() - t0, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())

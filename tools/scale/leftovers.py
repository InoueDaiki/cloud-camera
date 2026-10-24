"""cc-tmp-scale の残骸を探す(読み取りだけ)。"""
import boto3

P = "cc-tmp-scale"
found = []
tag = boto3.client("resourcegroupstaggingapi")
for page in tag.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project", "Values": [P]}]):
    found += [("tag", r["ResourceARN"]) for r in page["ResourceTagMappingList"]]
for b in boto3.client("s3").list_buckets()["Buckets"]:
    if b["Name"].startswith(P + "-"):
        found.append(("s3", b["Name"]))
logs = boto3.client("logs")
for pref in (f"/aws/lambda/{P}", f"/aws/apigateway/{P}", P):
    for g in logs.describe_log_groups(logGroupNamePrefix=pref).get("logGroups", []):
        found.append(("logs", g["logGroupName"]))
kv = boto3.client("kinesisvideo")
for s in kv.list_streams(StreamNameCondition={"ComparisonOperator": "BEGINS_WITH", "ComparisonValue": P})["StreamInfoList"]:
    found.append(("kvs", s["StreamName"]))
iot = boto3.client("iot")
for t in iot.list_things()["things"]:
    if t["thingName"].startswith(P):
        found.append(("iot thing", t["thingName"]))
for pol in iot.list_policies()["policies"]:
    if pol["policyName"].startswith(P):
        found.append(("iot policy", pol["policyName"]))
for a in iot.list_role_aliases()["roleAliases"]:
    if a.startswith(P):
        found.append(("iot role alias", a))
# 証明書: Thing やポリシーが消えていれば紐付けはない。ACTIVE / INACTIVE の証明書のうち、どのポリシーにも Thing にも付いていないものを数える
certs = iot.list_certificates(pageSize=250)["certificates"]
print("IoT 証明書(アカウント全体)", len(certs))
sch = boto3.client("scheduler")
for g in sch.list_schedule_groups(NamePrefix=P)["ScheduleGroups"]:
    found.append(("schedule group", g["Name"]))
for t in boto3.client("dynamodb").list_tables()["TableNames"]:
    if t.startswith(P):
        found.append(("dynamodb", t))
for f in boto3.client("lambda").list_functions()["Functions"]:
    if f["FunctionName"].startswith(P):
        found.append(("lambda", f["FunctionName"]))
iam = boto3.client("iam")
for page in iam.get_paginator("list_roles").paginate():
    found += [("iam role", r["RoleName"]) for r in page["Roles"] if r["RoleName"].startswith(P)]
for a in boto3.client("apigatewayv2").get_apis()["Items"]:
    if a["Name"].startswith(P):
        found.append(("apigw", a["Name"]))
cf = boto3.client("cloudfront")
for d in (cf.list_distributions().get("DistributionList", {}).get("Items") or []):
    if P in (d.get("Comment") or "") or any(P in o["DomainName"] for o in d["Origins"]["Items"]):
        found.append(("cloudfront", d["Id"] + " " + d["Status"]))
for g in boto3.client("glue").get_databases()["DatabaseList"]:
    if g["Name"].startswith(P.replace("-", "_")):
        found.append(("glue", g["Name"]))
for w in boto3.client("athena").list_work_groups()["WorkGroups"]:
    if w["Name"].startswith(P):
        found.append(("athena", w["Name"]))
print("残骸:", found if found else "なし")

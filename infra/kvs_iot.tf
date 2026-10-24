# ---------------------------------------------------------------------------
# Kinesis Video Streams(カメラごと)
# ---------------------------------------------------------------------------
resource "aws_kinesis_video_stream" "camera" {
  for_each = local.cameras

  name                    = "${local.stream_name_prefix}${each.key}"
  data_retention_in_hours = var.kvs_retention_hours
  media_type              = local.kvs_media_types[each.key]
}

# ---------------------------------------------------------------------------
# IoT Core: Thing、証明書、ポリシー(ゲートウェイごと)
# ---------------------------------------------------------------------------
data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

data "aws_iot_endpoint" "credential" {
  endpoint_type = "iot:CredentialProvider"
}

resource "aws_iot_thing" "gateway" {
  for_each = var.gateways

  name = "${local.iot_thing_prefix}${each.key}"
}

# 鍵ペアごと Terraform で発行する(秘密鍵は state にだけ残る。state の扱いに注意)
resource "aws_iot_certificate" "gateway" {
  for_each = var.gateways

  active = true
}

resource "aws_iot_thing_principal_attachment" "gateway" {
  for_each = var.gateways

  thing     = aws_iot_thing.gateway[each.key].name
  principal = aws_iot_certificate.gateway[each.key].arn
}

# 自分の Thing のトピックとシャドウだけを許可する
data "aws_iam_policy_document" "iot_gateway" {
  for_each = var.gateways

  statement {
    sid       = "Connect"
    actions   = ["iot:Connect"]
    resources = ["arn:${local.partition}:iot:${local.region}:${local.account_id}:client/${aws_iot_thing.gateway[each.key].name}"]
  }

  statement {
    sid     = "Publish"
    actions = ["iot:Publish", "iot:Receive", "iot:RetainPublish"]
    resources = [
      "arn:${local.partition}:iot:${local.region}:${local.account_id}:topic/${local.p}/${each.key}/*",
      "arn:${local.partition}:iot:${local.region}:${local.account_id}:topic/$aws/things/${aws_iot_thing.gateway[each.key].name}/shadow/*",
    ]
  }

  statement {
    sid     = "Subscribe"
    actions = ["iot:Subscribe"]
    resources = [
      "arn:${local.partition}:iot:${local.region}:${local.account_id}:topicfilter/${local.p}/${each.key}/*",
      "arn:${local.partition}:iot:${local.region}:${local.account_id}:topicfilter/$aws/things/${aws_iot_thing.gateway[each.key].name}/shadow/*",
    ]
  }

  statement {
    sid     = "Shadow"
    actions = ["iot:GetThingShadow", "iot:UpdateThingShadow"]
    resources = [
      aws_iot_thing.gateway[each.key].arn,
    ]
  }

  statement {
    sid       = "KvsCredentials"
    actions   = ["iot:AssumeRoleWithCertificate"]
    resources = [aws_iot_role_alias.kvs.arn]
  }
}

resource "aws_iot_policy" "gateway" {
  for_each = var.gateways

  name   = "${local.iot_thing_prefix}${each.key}-policy"
  policy = data.aws_iam_policy_document.iot_gateway[each.key].json
}

resource "aws_iot_policy_attachment" "gateway" {
  for_each = var.gateways

  policy = aws_iot_policy.gateway[each.key].name
  target = aws_iot_certificate.gateway[each.key].arn
}

# ---------------------------------------------------------------------------
# IoT 認証情報プロバイダー経由で KVS に書くためのロール
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "kvs_producer_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["credentials.iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "kvs_producer" {
  name               = "${local.p}-kvs-producer"
  assume_role_policy = data.aws_iam_policy_document.kvs_producer_assume.json
  # ロールエイリアスの credential_duration 以上にする必要がある
  max_session_duration = 43200
}

data "aws_iam_policy_document" "kvs_producer" {
  statement {
    actions = [
      "kinesisvideo:DescribeStream",
      "kinesisvideo:GetDataEndpoint",
      "kinesisvideo:PutMedia",
      "kinesisvideo:TagStream",
      "kinesisvideo:ListTagsForStream",
    ]
    resources = [local.stream_arn_pattern]
  }
}

resource "aws_iam_role_policy" "kvs_producer" {
  name   = "kvs-put-media"
  role   = aws_iam_role.kvs_producer.id
  policy = data.aws_iam_policy_document.kvs_producer.json
}

resource "aws_iot_role_alias" "kvs" {
  alias    = local.kvs_role_alias
  role_arn = aws_iam_role.kvs_producer.arn
  # 回線断の最中に認証情報の取り直しが重なると kvssink が止まり、バッファが消える(edge/README.md「回線断とバッファ」)。
  # 取り直しの頻度を下げるため、上限の 12 時間にする
  credential_duration = 43200
}

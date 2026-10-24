# ---------------------------------------------------------------------------
# KVS WebRTC: カメラごとのシグナリングチャネル(低遅延のライブ。docs/webrtc.md)
#
# hashicorp/aws(6.70)には AWS::KinesisVideo::SignalingChannel のリソースがないので、awscc(Cloud Control API)で作る。
# 名前は KVS ストリームと同じ {p}-{camera_id}(リソースの種類が違うので衝突しない)。
# master はエッジ(IoT 認証情報プロバイダーのロール {p}-kvs-producer)、viewer は API の Lambda が署名した URL でつなぐ。
# ---------------------------------------------------------------------------
locals {
  # awscc は default_tags を持たないので、同じタグをリソースに付ける
  awscc_tags = [
    { key = "Project", value = local.p },
    { key = "ManagedBy", value = "terraform" },
  ]
}

resource "awscc_kinesisvideo_signaling_channel" "camera" {
  for_each = local.cameras

  name = "${local.stream_name_prefix}${each.key}"
  type = "SINGLE_MASTER"
  # 届かなかったシグナリングのメッセージを保持する秒数(5〜120)
  message_ttl_seconds = 60
  tags                = local.awscc_tags
}

locals {
  signaling_channel_arns = [for c in awscc_kinesisvideo_signaling_channel.camera : c.arn]
}

# エッジ(master)。kvssink と同じロールエイリアス {p}-kvs-role-alias のロールに足す
data "aws_iam_policy_document" "kvs_producer_webrtc" {
  count = length(local.cameras) > 0 ? 1 : 0

  statement {
    actions = [
      "kinesisvideo:DescribeSignalingChannel",
      "kinesisvideo:GetSignalingChannelEndpoint",
      "kinesisvideo:GetIceServerConfig",
      "kinesisvideo:ConnectAsMaster",
    ]
    resources = local.signaling_channel_arns
  }
}

resource "aws_iam_role_policy" "kvs_producer_webrtc" {
  count = length(local.cameras) > 0 ? 1 : 0

  name   = "kvs-webrtc-master"
  role   = aws_iam_role.kvs_producer.id
  policy = data.aws_iam_policy_document.kvs_producer_webrtc[0].json
}

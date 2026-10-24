# ---------------------------------------------------------------------------
# Lambda: video_archiver / detector / api
#   zip は build/<key>.zip(各 lambdas/*/build.sh が作る)を artifacts バケットに
#   アップロードしてから S3 経由でデプロイする(直接アップロードの 50MB 制限を避ける)
# ---------------------------------------------------------------------------
locals {
  lambda_env = {
    PROJECT              = local.p
    STATE_TABLE          = aws_dynamodb_table.state.name
    MEDIA_BUCKET         = aws_s3_bucket.this["media"].id
    DETECTIONS_BUCKET    = aws_s3_bucket.this["detections"].id
    STREAM_NAME_PREFIX   = local.stream_name_prefix
    LAG_SECONDS          = tostring(var.lag_seconds)
    MAX_MINUTES          = tostring(var.max_minutes)
    MAX_ATTEMPTS         = tostring(var.max_attempts)
    MEDIA_RETENTION_DAYS = tostring(var.media_retention_days)
    GLUE_DATABASE        = aws_glue_catalog_database.main.name
    GLUE_TABLE           = aws_glue_catalog_table.detections.name
    ATHENA_WORKGROUP     = aws_athena_workgroup.main.name
    BEDROCK_MODEL_ID     = var.bedrock_model_id
    IOT_DATA_ENDPOINT    = data.aws_iot_endpoint.data.endpoint_address
    IOT_THING_PREFIX     = local.iot_thing_prefix
    CAMERAS              = jsonencode({ for c in local.cameras : c => local.camera_gateway[c] })
  }
}

locals {
  # zip がなくても destroy はできるように、存在しないときはハッシュを空にする(apply は precondition で止める)
  lambda_zip_path = { for k in keys(local.lambdas) : k => "${local.lambda_zip_dir}/${k}.zip" }
  lambda_zip_md5  = { for k, p in local.lambda_zip_path : k => fileexists(p) ? filemd5(p) : null }
  lambda_zip_b64  = { for k, p in local.lambda_zip_path : k => fileexists(p) ? filebase64sha256(p) : null }
}

resource "aws_s3_object" "lambda_zip" {
  for_each = local.lambdas

  bucket      = aws_s3_bucket.this["artifacts"].id
  key         = "lambda/${each.key}.zip"
  source      = local.lambda_zip_path[each.key]
  source_hash = local.lambda_zip_md5[each.key]

  lifecycle {
    precondition {
      condition     = local.lambda_zip_md5[each.key] != null
      error_message = "${local.lambda_zip_path[each.key]} がない。先に scripts/build-all.sh(または lambdas/${each.key}/build.sh)を実行する。"
    }
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.lambdas

  name              = "/aws/lambda/${each.value.function_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambdas

  name               = "${each.value.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas

  name   = "${each.key}-policy"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}

resource "aws_lambda_function" "this" {
  for_each = local.lambdas

  function_name    = each.value.function_name
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.lambda_handler"
  s3_bucket        = aws_s3_object.lambda_zip[each.key].bucket
  s3_key           = aws_s3_object.lambda_zip[each.key].key
  source_code_hash = local.lambda_zip_b64[each.key]
  memory_size      = each.value.memory_size
  timeout          = each.value.timeout
  layers           = each.value.layers

  ephemeral_storage {
    size = each.value.ephemeral_storage
  }

  environment {
    variables = local.lambda_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda[each.key].name
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

# ---------------------------------------------------------------------------
# IAM(関数ごとに最小権限)
# ---------------------------------------------------------------------------
locals {
  state_table_arn = aws_dynamodb_table.state.arn
  glue_arns = [
    "arn:${local.partition}:glue:${local.region}:${local.account_id}:catalog",
    aws_glue_catalog_database.main.arn,
    aws_glue_catalog_table.detections.arn,
  ]
  iot_thing_arns = [for gw, t in aws_iot_thing.gateway : t.arn]
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.lambdas

  # ログ(ロググループは Terraform で作るので CreateLogGroup は不要)
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
  }

  # 状態テーブル
  statement {
    sid = "StateTable"
    actions = each.key == "api" ? [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:BatchGetItem",
      ] : [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [local.state_table_arn]
  }

  # KVS(カメラのストリームだけ)
  statement {
    sid = "KinesisVideo"
    actions = {
      video_archiver = [
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:ListFragments",
        "kinesisvideo:GetClip",
        "kinesisvideo:GetImages",
        "kinesisvideo:GetMediaForFragmentList",
      ]
      detector = [
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:ListFragments",
        "kinesisvideo:GetImages",
      ]
      # HLS のセッション URL は発行した側の権限で再生されるので、再生系の操作も付ける
      api = [
        "kinesisvideo:DescribeStream",
        "kinesisvideo:GetDataEndpoint",
        "kinesisvideo:ListFragments",
        "kinesisvideo:GetHLSStreamingSessionURL",
        "kinesisvideo:GetHLSMasterPlaylist",
        "kinesisvideo:GetHLSMediaPlaylist",
        "kinesisvideo:GetMP4InitFragment",
        "kinesisvideo:GetMP4MediaFragment",
        "kinesisvideo:GetTSFragment",
        "kinesisvideo:GetClip",
        "kinesisvideo:GetImages",
      ]
    }[each.key]
    resources = [local.stream_arn_pattern]
  }

  # S3: メディア
  dynamic "statement" {
    for_each = each.key == "video_archiver" ? [1] : []
    content {
      sid       = "MediaWrite"
      actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
      resources = ["${aws_s3_bucket.this["media"].arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid       = "MediaRead"
      actions   = ["s3:GetObject"]
      resources = ["${aws_s3_bucket.this["media"].arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = contains(["video_archiver", "api"], each.key) ? [1] : []
    content {
      sid       = "MediaList"
      actions   = ["s3:ListBucket"]
      resources = [aws_s3_bucket.this["media"].arn]
    }
  }

  # S3: 検出結果(detector は読み書き、api は Athena 経由で読む)
  dynamic "statement" {
    for_each = contains(["detector", "api"], each.key) ? [1] : []
    content {
      sid       = "DetectionsObjects"
      actions   = each.key == "detector" ? ["s3:GetObject", "s3:PutObject"] : ["s3:GetObject"]
      resources = ["${aws_s3_bucket.this["detections"].arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = contains(["detector", "api"], each.key) ? [1] : []
    content {
      sid       = "DetectionsList"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.this["detections"].arn]
    }
  }

  # Glue(テーブル定義は Terraform で作るので読み取りだけ)
  dynamic "statement" {
    for_each = contains(["detector", "api"], each.key) ? [1] : []
    content {
      sid       = "GlueRead"
      actions   = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
      resources = local.glue_arns
    }
  }

  # Athena と結果バケット(api だけ)
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid = "Athena"
      actions = [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution",
        "athena:GetWorkGroup",
      ]
      resources = [aws_athena_workgroup.main.arn]
    }
  }
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid       = "AthenaResults"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
      resources = ["${aws_s3_bucket.this["athena"].arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid       = "AthenaResultsBucket"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
      resources = [aws_s3_bucket.this["athena"].arn]
    }
  }

  # Bedrock(api だけ)。Converse / ConverseStream も IAM では InvokeModel* で判定される。
  # 推論プロファイル経由の場合は、プロファイルと(ルーティング先リージョンの)基盤モデルの両方に権限が要る
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid     = "Bedrock"
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = concat(
        ["arn:${local.partition}:bedrock:*::foundation-model/${local.bedrock_base_model}"],
        local.bedrock_is_profile ? ["arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/${var.bedrock_model_id}"] : [],
      )
    }
  }

  # IoT シャドウ(api だけ)
  dynamic "statement" {
    for_each = each.key == "api" ? [1] : []
    content {
      sid       = "IotShadow"
      actions   = ["iot:GetThingShadow", "iot:UpdateThingShadow"]
      resources = local.iot_thing_arns
    }
  }
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler: カメラごとに video / detect を 1 分おきに起動
# ---------------------------------------------------------------------------
locals {
  schedule_targets = {
    video  = "video_archiver"
    detect = "detector"
  }
  schedules = merge([
    for kind, fn in local.schedule_targets : {
      for c in local.cameras : "${kind}-${c}" => { kind = kind, function = fn, camera_id = c }
    }
  ]...)
}

resource "aws_scheduler_schedule_group" "main" {
  name = local.p
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.p}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.this["video_archiver"].arn,
      aws_lambda_function.this["detector"].arn,
    ]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "invoke-lambda"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "camera" {
  for_each = local.schedules

  name                = "${local.p}-${each.key}"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "rate(1 minute)"
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.this[each.value.function].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ camera_id = each.value.camera_id })

    # 取りこぼしは次の起動が処理済み位置から追いつくので、再試行はしない
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}

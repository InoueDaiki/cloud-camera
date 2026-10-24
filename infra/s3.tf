# ---------------------------------------------------------------------------
# S3 バケット
#   media      : 動画、スプライト、プレイリスト(CloudFront OAC 経由で配信)
#   detections : 検出結果の Parquet(バージョニング、旧バージョンは 1 日で削除)
#   athena     : Athena のクエリ結果(7 日で削除)
#   viewer     : ビューワーの静的ファイル(CloudFront OAC 経由で配信)
#   artifacts  : Lambda の zip(50MB を超えても S3 経由でデプロイできるように)
# ---------------------------------------------------------------------------
locals {
  buckets = {
    media      = "${local.p}-media-${local.account_id}"
    detections = "${local.p}-detections-${local.account_id}"
    athena     = "${local.p}-athena-${local.account_id}"
    viewer     = "${local.p}-viewer-${local.account_id}"
    artifacts  = "${local.p}-artifacts-${local.account_id}"
  }
}

resource "aws_s3_bucket" "this" {
  for_each = local.buckets

  bucket        = each.value
  force_destroy = var.force_destroy_buckets
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = local.buckets

  bucket                  = aws_s3_bucket.this[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = local.buckets

  bucket = aws_s3_bucket.this[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "detections" {
  bucket = aws_s3_bucket.this["detections"].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.this["media"].id

  rule {
    id     = "expire-media"
    status = "Enabled"
    filter {}
    expiration {
      days = var.media_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "detections" {
  bucket = aws_s3_bucket.this["detections"].id

  # day ファイルは 1 日 1,440 回書き直すので、旧バージョンは 1 日で消す
  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    expiration {
      expired_object_delete_marker = true
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.detections]
}

resource "aws_s3_bucket_lifecycle_configuration" "athena" {
  bucket = aws_s3_bucket.this["athena"].id

  rule {
    id     = "expire-results"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.this["artifacts"].id

  rule {
    id     = "abort-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# CloudFront(OAC)からの GetObject だけを許可する
data "aws_iam_policy_document" "cloudfront_read" {
  for_each = toset(["media", "viewer"])

  statement {
    sid       = "AllowCloudFrontOAC"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this[each.key].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this[each.key].arn, "${aws_s3_bucket.this[each.key].arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudfront_read" {
  for_each = toset(["media", "viewer"])

  bucket = aws_s3_bucket.this[each.key].id
  policy = data.aws_iam_policy_document.cloudfront_read[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.this]
}

# コスト計測(tools/cost)用に PUT/GET の回数を CloudWatch に出す
resource "aws_s3_bucket_metric" "requests" {
  for_each = toset(["media", "detections"])

  bucket = aws_s3_bucket.this[each.key].id
  name   = "EntireBucket"
}

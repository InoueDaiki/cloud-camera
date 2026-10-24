# ---------------------------------------------------------------------------
# CloudFront: ビューワー(既定)、/media/*(メディアバケット)、/api/*(HTTP API)
# ---------------------------------------------------------------------------
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "${local.p}-s3"
  description                       = "${local.p} の S3 オリジン用"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# /media/cam-001/... → メディアバケットのキー cam-001/... に変換する
resource "aws_cloudfront_function" "media_strip_prefix" {
  name    = "${local.p}-media-strip-prefix"
  runtime = "cloudfront-js-2.0"
  comment = "/media/ 接頭辞を外してメディアバケットのキーにする"
  publish = true
  code    = file("${path.module}/functions/media_strip_prefix.js")
}

# SPA のルーティング: 拡張子のないパスは /index.html を返す。
# custom_error_response(403/404 → index.html)はディストリビューション全体に効き、
# /api/* の 404 や /media/* の 403 まで index.html に化けるので、関数で既定の振る舞いだけに適用する
resource "aws_cloudfront_function" "spa_rewrite" {
  name    = "${local.p}-spa-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "拡張子のないパスを /index.html に書き換える"
  publish = true
  code    = file("${path.module}/functions/spa_rewrite.js")
}

locals {
  origin_viewer = "viewer"
  origin_media  = "media"
  origin_api    = "api"
  api_domain    = replace(aws_apigatewayv2_api.main.api_endpoint, "https://", "")
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  comment             = local.p
  default_root_object = "index.html"
  price_class         = var.cloudfront_price_class
  http_version        = "http2and3"
  is_ipv6_enabled     = true

  origin {
    origin_id                = local.origin_viewer
    domain_name              = aws_s3_bucket.this["viewer"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id                = local.origin_media
    domain_name              = aws_s3_bucket.this["media"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id   = local.origin_api
    domain_name = local.api_domain

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 30
      origin_keepalive_timeout = 5
    }
  }

  # 既定: ビューワーの静的ファイル
  default_cache_behavior {
    target_origin_id       = local.origin_viewer
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.spa_rewrite.arn
    }
  }

  # API: キャッシュしない。クエリ文字列、ヘッダー、Cookie を転送(Host は除く)
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = local.origin_api
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }

  # プレイリストはキャッシュしない
  ordered_cache_behavior {
    path_pattern           = "/media/*.m3u8"
    target_origin_id       = local.origin_media
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = data.aws_cloudfront_cache_policy.disabled.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.media_strip_prefix.arn
    }
  }

  # 動画、スプライト(Range リクエストはそのまま S3 に渡る)
  ordered_cache_behavior {
    path_pattern           = "/media/*"
    target_origin_id       = local.origin_media
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.media_strip_prefix.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

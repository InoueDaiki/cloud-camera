# ---------------------------------------------------------------------------
# DynamoDB: 処理済み位置と 1 分区間の件
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "state" {
  name         = "${local.p}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "camera_id"
  range_key    = "sk"

  attribute {
    name = "camera_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }
}

# ---------------------------------------------------------------------------
# Glue Data Catalog: 検出結果のテーブル(dt はパーティションプロジェクション)
# ---------------------------------------------------------------------------
resource "aws_glue_catalog_database" "main" {
  name        = local.glue_database
  description = "${local.p} の検出結果"
}

resource "aws_glue_catalog_table" "detections" {
  name          = local.glue_table
  database_name = aws_glue_catalog_database.main.name
  table_type    = "EXTERNAL_TABLE"
  description   = "物体検出の結果(1 行 = 1 検出)"

  parameters = {
    "classification"              = "parquet"
    "EXTERNAL"                    = "TRUE"
    "parquet.compression"         = "SNAPPY"
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "2026-01-01,NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${aws_s3_bucket.this["detections"].id}/detections/dt=$${dt}/"
  }

  partition_keys {
    name    = "dt"
    type    = "string"
    comment = "日付(UTC、yyyy-MM-dd)"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.this["detections"].id}/detections/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name    = "camera_id"
      type    = "string"
      comment = "カメラ ID"
    }
    columns {
      name    = "ts"
      type    = "timestamp"
      comment = "フレームの時刻(UTC、ミリ秒)"
    }
    columns {
      name    = "label"
      type    = "string"
      comment = "COCO のクラス名(英語)"
    }
    columns {
      name    = "confidence"
      type    = "float"
      comment = "信頼度 0〜1"
    }
    columns {
      name    = "x"
      type    = "float"
      comment = "バウンディングボックス左上 x(0〜1)"
    }
    columns {
      name    = "y"
      type    = "float"
      comment = "バウンディングボックス左上 y(0〜1)"
    }
    columns {
      name    = "w"
      type    = "float"
      comment = "幅(0〜1)"
    }
    columns {
      name    = "h"
      type    = "float"
      comment = "高さ(0〜1)"
    }
    columns {
      name    = "model"
      type    = "string"
      comment = "検出モデル名"
    }
  }
}

# ---------------------------------------------------------------------------
# Athena ワークグループ(結果は athena バケット、スキャン量に上限)
# ---------------------------------------------------------------------------
resource "aws_athena_workgroup" "main" {
  name          = local.p
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.athena_bytes_scanned_cutoff

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }

    result_configuration {
      output_location = "s3://${aws_s3_bucket.this["athena"].id}/results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

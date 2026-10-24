data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  p          = var.project
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition

  # カメラ ID → ゲートウェイ ID
  camera_gateway = merge([
    for gw, cams in var.gateways : { for c in cams : c => gw }
  ]...)

  cameras = var.cameras != null ? toset(var.cameras) : toset(keys(local.camera_gateway))

  # Glue のデータベース名はハイフンを使えないのでアンダースコアにする
  glue_database = replace(local.p, "-", "_")
  glue_table    = "detections"

  iot_thing_prefix   = "${local.p}-"
  stream_name_prefix = "${local.p}-"
  kvs_role_alias     = "${local.p}-kvs-role-alias"

  # Lambda の定義。キーは build/<key>.zip とディレクトリ名に対応する
  lambda_defaults = {
    video_archiver = { memory_size = 512, timeout = 300, ephemeral_storage = 1024 }
    detector       = { memory_size = 3008, timeout = 300, ephemeral_storage = 1024 }
    api            = { memory_size = 512, timeout = 30, ephemeral_storage = 512 }
  }
  lambdas = {
    for k, d in local.lambda_defaults : k => {
      function_name     = "${local.p}-${replace(k, "_", "-")}"
      memory_size       = coalesce(try(var.lambda_settings[k].memory_size, null), d.memory_size)
      timeout           = coalesce(try(var.lambda_settings[k].timeout, null), d.timeout)
      ephemeral_storage = coalesce(try(var.lambda_settings[k].ephemeral_storage, null), d.ephemeral_storage)
      layers            = lookup(var.lambda_layers, k, [])
    }
  }
  lambda_zip_dir = coalesce(var.lambda_zip_dir, "${path.module}/../build")

  # Bedrock: 推論プロファイル ID(jp. / apac. / global. など)から基盤モデル ID を取り出す
  bedrock_is_profile = can(regex("^(us|eu|apac|jp|au|ca|us-gov|global)\\.", var.bedrock_model_id))
  bedrock_base_model = local.bedrock_is_profile ? regex("^[^.]+\\.(.+)$", var.bedrock_model_id)[0] : var.bedrock_model_id

  stream_arn_pattern = "arn:${local.partition}:kinesisvideo:${local.region}:${local.account_id}:stream/${local.stream_name_prefix}*/*"
}

check "cameras_match_gateways" {
  assert {
    condition     = length(setsubtract(local.cameras, keys(local.camera_gateway))) == 0
    error_message = "var.cameras に、どのゲートウェイにも属さないカメラがある。"
  }
}

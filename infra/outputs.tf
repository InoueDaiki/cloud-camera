# scripts/provision-edge.sh などがこの名前で読む。名前を変えるときは契約を直す

output "project" {
  value = local.p
}

output "region" {
  value = local.region
}

output "iot_data_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "iot_credential_endpoint" {
  value = data.aws_iot_endpoint.credential.endpoint_address
}

output "kvs_role_alias" {
  value = aws_iot_role_alias.kvs.alias
}

output "gateways" {
  description = "gateway_id → {thing_name, cameras}"
  value = {
    for gw, cams in var.gateways : gw => {
      thing_name = aws_iot_thing.gateway[gw].name
      cameras    = cams
    }
  }
}

output "gateway_certificates" {
  description = "gateway_id → {certificate_pem, private_key}"
  sensitive   = true
  value = {
    for gw, cert in aws_iot_certificate.gateway : gw => {
      certificate_pem = cert.certificate_pem
      private_key     = cert.private_key
    }
  }
}

output "kvs_streams" {
  description = "camera_id → KVS ストリーム名"
  value       = { for c, s in aws_kinesis_video_stream.camera : c => s.name }
}

output "webrtc_channels" {
  description = "camera_id → {name, arn}(KVS WebRTC のシグナリングチャネル)"
  value       = { for c, ch in awscc_kinesisvideo_signaling_channel.camera : c => { name = ch.name, arn = ch.arn } }
}

output "webrtc_channel_prefix" {
  description = "シグナリングチャネル名の接頭辞(チャネル名 = 接頭辞 + カメラ ID)。エッジの config.json に書く"
  value       = local.stream_name_prefix
}

output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "viewer_bucket" {
  value = aws_s3_bucket.this["viewer"].id
}

output "media_bucket" {
  value = aws_s3_bucket.this["media"].id
}

output "detections_bucket" {
  value = aws_s3_bucket.this["detections"].id
}

output "athena_bucket" {
  value = aws_s3_bucket.this["athena"].id
}

output "artifacts_bucket" {
  value = aws_s3_bucket.this["artifacts"].id
}

output "state_table" {
  value = aws_dynamodb_table.state.name
}

output "api_endpoint" {
  description = "API Gateway の直接のエンドポイント(通常は cloudfront_url/api/... を使う)"
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "lambda_functions" {
  value = { for k, f in aws_lambda_function.this : k => f.function_name }
}

output "glue_database" {
  value = aws_glue_catalog_database.main.name
}

output "athena_workgroup" {
  value = aws_athena_workgroup.main.name
}

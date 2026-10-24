// CloudFront Function(viewer-request)
// /media/cam-001/2026/... → /cam-001/2026/...(メディアバケットのキー)
function handler(event) {
  var request = event.request;
  if (request.uri.indexOf('/media/') === 0) {
    request.uri = request.uri.substring('/media'.length);
  }
  return request;
}

// CloudFront Function(viewer-request、既定のビヘイビアだけ)
// 最後のパス要素に拡張子がなければ SPA のルートとみなして /index.html を返す
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var last = uri.substring(uri.lastIndexOf('/') + 1);
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}

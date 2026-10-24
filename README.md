# cloud-camera

サーバーレス映像基盤の検証プロジェクト。背景と設計は [構想.md](構想.md)、検証の中で決めたこと・変えたことは [docs/decisions.md](docs/decisions.md)、コンポーネント間の約束事は [docs/contracts.md](docs/contracts.md)。

## セットアップ

```sh
mise trust && mise install
```

AWS CLI のプロファイル `cloud-camera`、リージョン `ap-northeast-1` は `mise.toml` で設定される。

## よく使うコマンド

| やること | コマンド |
| --- | --- |
| 全コンポーネントの単体テストと静的検査 | `scripts/test-all.sh` |
| ビルド(Lambda の zip とビューワー) | `scripts/build-all.sh` |
| デプロイ / 削除 | `scripts/deploy.sh [PROJECT]` / `scripts/destroy.sh [PROJECT]` |
| エミュレーターとエッジをローカルで起動 | `docker compose up --build`(詳細は [edge/README.md](edge/README.md)) |
| コストの机上計算 | `cd tools/cost && mise exec -- uv run python -m cost desk` |

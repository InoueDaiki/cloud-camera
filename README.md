# cloud-camera

サーバーレス映像基盤の検証プロジェクト。背景と設計は [構想.md](構想.md)、コンポーネント間の約束事は [docs/contracts.md](docs/contracts.md)。

## セットアップ

```sh
mise trust && mise install
```

AWS CLI のプロファイル `cloud-camera`、リージョン `ap-northeast-1` は `mise.toml` で設定される。

全体を通す検証(E2E)は [docs/e2e.md](docs/e2e.md)(`scripts/e2e/run.sh`)。

# 運用ドキュメント

初期セットアップ、変更・デプロイの手順、調整値の一覧。

## 1. 環境

| 項目 | 値 |
|---|---|
| GCP プロジェクト | infra-dev-project(`--env dev`) |
| リージョン | asia-northeast1 |
| モデル推論 | global endpoint(リージョナル割当逼迫時の 429 回避) |
| レジストリ | `config/projects.yaml`(`projects.<env>.yaml` があれば優先) |

## 2. 初期セットアップ

新しい GCP プロジェクトで基盤を立ち上げる手順。

### 2.1 GCP API とステージングバケット

```bash
gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com \
  --project <PROJECT>
gsutil mb -l asia-northeast1 -p <PROJECT> gs://<staging-bucket>
```

バケット名は `config/deploy.<env>.yaml` の `staging_bucket` に記載する。

### 2.2 シークレット登録

すべて **人間が静的に登録する**(システムが実行時に書き換えることはない)。

シークレット名はレジストリ(`config/projects.yaml`)の `*_secret` に書いた名前がそのまま使われる。プロジェクト別に預かるものは `-<project id>` サフィックスで分ける。

| シークレット名(例) | 内容 | 取得方法 |
|---|---|---|
| `ws-backlog-key-<project>` | Backlog API キー | 閲覧専用ユーザーで発行 |
| `ws-gws-token-<project>` | GWS OAuth リフレッシュトークン | readonly スコープのみで同意して取得 |
| `ws-gws-client-secret` | OAuth クライアントシークレット | OAuth クライアント作成時 |
| `ws-slack-token-<project>` | Slack 静的ユーザートークン(`xoxp-`) | App を Install to Workspace(rotation 無効必須) |
| `ws-github-pat` または `ws-github-app-key` | GitHub PAT(単一プロジェクト)/ GitHub App 秘密鍵(複数プロジェクト) | read 系スコープ / App 作成時 |

```bash
printf '%s' '<value>' | gcloud secrets create <name> --project <PROJECT> \
  --replication-policy automatic --data-file=-
```

登録後、Reasoning Engine のサービスエージェントに `roles/secretmanager.secretAccessor` を付与する。

<details>
<summary>Slack App 作成時の注意</summary>

- マニフェストは `config/slack_app_manifest.yaml`。**`token_rotation_enabled: false` 必須** — rotation は App 単位で不可逆のため、一度 Opt In した App は再利用できず新規作成になる
- スコープは `search:read` / `channels:read` / `channels:history` / `groups:read` / `groups:history` / `users:read` の 6 つ。DM 系(`im:*` / `mpim:*`)は目的外のため付与しない
</details>

### 2.3 レジストリ(config/projects.yaml)

設定の正はリポジトリ内 YAML。deploy スクリプトが検証して env_vars(`WS_PROJECTS`)へ展開する。

```yaml
projects:
  - id: example             # 英数小文字のサービス名。state・ログ・封筒で使う識別子
    name: EXAMPLE           # 表示名
    members:                # IAP 入場許可リスト(sync_iap.sh が同期)
      - user@example.com
    backlog:
      domain: example.backlog.jp
      project_keys: [PRJXXX] # 1 サービスが複数キーを束ねる。id とは別物
      api_key_secret: ws-backlog-key-example  # 値は Secret Manager 名のみ
    gws:
      refresh_token_secret: ws-gws-token-example
    slack:
      user_token_secret: ws-slack-token-example
      team_id: T0XXXXXXX
    anchors:                # 参照の基点(初期値。ユーザーが UI で追加・削除可能)
      - name: 障害一覧
        url: https://docs.google.com/spreadsheets/d/...
```

### 2.4 IAP

BFF は `--iap` 付きでデプロイされ、`X-Goog-Authenticated-User-Email` ヘッダで本人確認する。

**メンバーの正はレジストリの `members` ただ 1 箇所**。`members` の役割は IAP 入場許可リストのみで、`frontend/sync_iap.sh <env>` が `roles/iap.httpsResourceAccessor` を突合する(`deploy.sh` が末尾で自動実行)。IAP を通過したユーザーは全プロジェクトにアクセス可(BFF での所属解決は行わない)。

同期は `user:` バインディングのみを対象とし、グループ・サービスアカウントには触れない。差分の確認は `--dry-run`。

```bash
./frontend/sync_iap.sh <env> --dry-run   # 差分表示のみ
./frontend/sync_iap.sh <env>             # 適用(deploy.sh からも呼ばれる)
```

> **注意**: `WS_DEV_USER` はローカル開発専用で IAP をバイパスする。**IAP 有効環境では絶対に設定しない**こと。

### 2.5 エージェント構成

応答と探索は別エージェントに分離されている。orchestrator は検索ツールを構造的に
持たないため、自ら探索できない。素材の充足判定を応答側に置くことで、探索の途中で
止まった応答がそのままユーザーに届く経路を塞いでいる。

```
root: _RetryPlanOnlyAgent          ツール呼出ゼロのターンを 1 度だけ再実行
 └─ orchestrator (wsagent_llm)     応答 + 探索の指揮。tools=[AgentTool(explorer)]
     └─ explorer                    検索・読取ツール 16 個
```

## 3. 変更とデプロイ

### 3.1 変更の種類と必要な操作

| 変更対象 | 変更ファイル | 必要なデプロイ |
|---|---|---|
| システムプロンプト | `agent/agents/wsagent/prompts/system.py` | Agent Engine |
| モデル・パラメータ | `agent/agents/wsagent/agent.py` | Agent Engine |
| ツール・Gateway | `agent/agents/wsagent/{tools,gateway}/` | Agent Engine |
| アンカー・接続設定 | `config/projects.yaml` | Agent Engine **と** BFF |
| メンバー(IAP 入場許可) | `config/projects.yaml` の `members` | `frontend/sync_iap.sh <env>`(BFF デプロイ時は自動実行) |
| UI・SSE 中継 | `frontend/` | BFF |
| シークレット値の差替え | Secret Manager(バージョン追加) | 不要(遅延解決のため次回呼び出しから反映) |

### 3.2 Agent Engine のデプロイ

```bash
cd agent
uv run pytest                                     # 契約テスト
uv run python scripts/deploy.py --env dev --dry-run   # 展開内容の確認
uv run python scripts/deploy.py --env dev
```

- `display_name="ws-agent"` の既存エンジンがあれば update、なければ create。複数あれば `--resource-name` で指定
- 依存パッケージは `deploy.py` の `REQUIREMENTS` に固定(ADK 1.23.0)
- `GOOGLE_CLOUD_PROJECT` は Agent Engine 側が注入する予約 env のため渡さない

### 3.3 BFF のデプロイ

```bash
cd frontend
go build ./...                                    # ローカル検証
./deploy.sh dev <agent-engine-resource-name>
```

Cloud Build でイメージをビルドし、レジストリを `WS_PROJECTS` / `WS_ENGINES` に展開して Cloud Run へデプロイする。

### 3.4 デプロイ後の確認

1. BFF の URL を開き、ランチャー画面と全プロジェクトのバッジ表示を確認
2. 横断質問を 1 件送信し、ストリーミング応答・出典リンク・右ペインの「参照した記録」を確認
3. 探索の中身は Agent Engine の **Playground**(セッションイベント)で function_call / function_response を確認できる

## 4. 調整値

<details open>
<summary>推論パラメータ(agent/agents/wsagent/agent.py)</summary>

| 項目 | 現在値 | 根拠 |
|---|---|---|
| モデル | `gemini-2.5-flash`(両エージェント共通) | ツール呼び出しの確実性(グラウンディング)と応答速度の両立 |
| temperature | `0.5` | 探索軌道の再現性と考察の幅のバランス |
| thinking(explorer) | `thinking_budget=-1`(dynamic) | 探索実行が主で判断の比重が低い |
| thinking(orchestrator) | `thinking_budget=8192`(固定) | dynamic ではモデルが思考を省略し、素材と質問の主題一致判定が働かなかった |
| explorer 呼出上限 | 3 回 / ターン | 再依頼ループの暴走防止。超過時は手持ちの素材で回答 |
| endpoint | global | リージョナル割当逼迫時の 429 回避 |
</details>

<details open>
<summary>封筒の切詰め(agent/agents/wsagent/gateway/envelope.py)</summary>

| 項目 | 現在値 | 説明 |
|---|---|---|
| `MAX_ITEM_CHARS` | 4,000 | item 1 件の本文上限。超過は切詰め + `truncated` 自己申告 |
| `MAX_ENVELOPE_CHARS` | 24,000 | 封筒全体の本文合計上限 |
| `PROJECT_CONCURRENCY` | 3 | プロジェクト横断照会の並列数 |
</details>

<details open>
<summary>BFF の入力制限(frontend/main.go)</summary>

| 項目 | 現在値 | 説明 |
|---|---|---|
| anchors 件数 | 12 | ユーザー送信アンカーの上限 |
| anchor name / url | 100 / 500 文字 | 超過分は切詰め |
</details>

## 5. トラブルシューティング

| 症状 | 見る場所 | 典型原因と対処 |
|---|---|---|
| 応答が浅い・参照が偏る | Playground のセッションイベント | プロンプト・入力の問題。ツール呼び出し履歴(search/read の回数と引数)で切り分ける |
| `auth_expired` failure | Cloud Logging(severity=ERROR) | リフレッシュトークン失効。§2.2 の手順で再登録 |
| 429 RESOURCE_EXHAUSTED | Agent Engine ログ | global endpoint で原則回避済み。継続するならクォータ確認 |
| セッションが分かれる/引き継がれる | BFF ログ | `conv_id` は state に保存され `list_sessions` で全インスタンスが収束する設計。BFF のデプロイ版が最新か確認 |
| IAP の「アクセス権がありません」画面 | — | レジストリ `members` に無い、または IAP 側に未反映。YAML 更新後 `frontend/sync_iap.sh <env> --dry-run` で差分を確認し同期 |
| ツールが空封筒を返す | 封筒の `failures[].reason` | `not_configured`(レジストリに該当ソースの設定なし)/ `timeout` / `upstream_error` を自己申告している |
| GitHub コード検索が常に 0 件 | 封筒の `failures[].reason` が `not_configured` | 対象が fork のため GitHub の索引対象外。`code_search_indexed: false` による意図的な遮断。ソースへは `search_github_paths` / `list_github_tree` から到達する |

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

ワークスペース情報(Slack / Google Workspace / Backlog / GitHub)を read-only で横断検索・要約するエージェント基盤。ADK + Vertex AI Agent Engine + 自作フロントエンド(Cloud Run + IAP)。

## 設計上の不変条件

- すべて read-only。書き込みコードパスを作らない
- アクセス境界はプラットフォーム側。基盤は「プロジェクトが用意したクレデンシャルを預かって使うだけ」
- Tool Gateway 方式: LLM 可視ツールは自前関数のみ(project_id 引数を持たない)。project_ids は session state からのみ解決し、before_tool_callback がモデル由来の識別子を剥奪
- ツール戻り値は封筒スキーマ(source/projects/fetched_at/complete/failures/count/items)。不完全性は常に自己申告
- シークレットは Secret Manager。import 時の環境変数参照は禁止(遅延解決)
- エージェント応答は自由テキスト(Markdown)


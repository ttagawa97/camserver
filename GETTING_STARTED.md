# インターネットカメラ静止画管理システム - 構築完了ガイド

## 構築完了 ✅

Django REST APIサーバーが完成しました。以下のコンポーネントが実装されています：

### ✅ 実装済み機能

1. **Django REST API サーバー**
   - 企業・現場・カメラ・画像管理API
   - 権限ベースのアクセス制御（RBAC）
   - 4つのユーザーロール対応（システム管理者、企業管理者、現場管理者、一般ユーザー）

2. **APScheduler による定期画像取得**
   - Webサーバー起動時に自動起動
   - 動的にスケジュール変更可能（API経由）
   - カメラごとの独立したスケジューリング

3. **カメラ接続・画像取得機構**
   - BASIC認証対応HTTP/HTTPSカメラ対応
   - 接続テスト機能
   - 画像保存（企業・現場・カメラ・日付階層）
   - サムネイル自動生成

4. **データベース**
   - SQLiteで開発環境用
   - モデル設計：Company → Site → Camera → Image

5. **管理コマンド**
   - `init_sample_data`: サンプルデータ初期化
   - `scheduler_status`: スケジューラーステータス表示

## クイックスタート

### サーバー起動

```bash
# 方法1: 付属のスクリプト使用
cd /home/ubuntu/develop/camserver
./start_server.sh

# 方法2: 手動起動
cd /home/ubuntu/develop/camserver
source venv/bin/activate
python manage.py runserver 0.0.0.0:8000
```

### スケジューラーのステータス確認

```bash
cd /home/ubuntu/develop/camserver
source venv/bin/activate
python manage.py scheduler_status
```

出力例：
```
Status: RUNNING ✓
Active Jobs: 4

Jobs:
  - ID: camera_63264c88-6539-424f-b8d4-2e587628f17a
    Name: Capture 工場入口カメラ
    Next Run: 2026-06-22 15:55:16.126069+09:00
    Trigger: interval[0:01:00]
```

## テストユーザー認証情報

| ロール | ユーザー名 | パスワード |
|--------|-----------|-----------|
| システム管理者 | admin | admin123 |
| 企業管理者 | company_admin | password123 |
| 現場管理者 | site_admin | password123 |
| 一般ユーザー | user | password123 |

## APIエンドポイント

すべてのエンドポイントは `/api/v1/` で始まります。

### 認証
```
POST   /auth/login/              # ログイン
GET    /auth/me/                 # ログインユーザー情報
POST   /auth/logout/             # ログアウト
```

### 企業・現場・カメラ
```
GET    /companies/               # 企業一覧
POST   /companies/               # 企業作成
GET    /sites/                   # 現場一覧
POST   /sites/                   # 現場作成
GET    /cameras/                 # カメラ一覧
POST   /cameras/                 # カメラ登録
POST   /cameras/{id}/test_connection/          # カメラ接続テスト
POST   /cameras/{id}/capture_now/              # 即座に画像取得
PATCH  /cameras/{id}/update_schedule/          # 取得間隔変更（動的反映）
```

### 画像・スケジュール
```
GET    /images/                  # 画像一覧
GET    /images/dates_with_images/              # 画像存在日付一覧
GET    /images/latest_images/                  # 複数カメラ最新画像
GET    /schedules/               # スケジュール一覧
```

## Django Admin

- **URL**: http://localhost:8000/admin/
- **ユーザー**: admin
- **パスワード**: admin123

## プロジェクト構成

```
camserver/
├── camserver/
│   ├── settings.py              # Django設定
│   ├── urls.py                  # URL設定
│   ├── wsgi.py                  # WSGI設定
│   ├── scheduler.py             # APScheduler管理
│   └── __init__.py
├── core/
│   ├── models.py                # モデル定義
│   ├── views.py                 # ViewSet（API実装）
│   ├── serializers.py           # DRFシリアライザー
│   ├── urls.py                  # API ルーティング
│   ├── apps.py                  # アプリ設定（スケジューラー自動起動）
│   ├── admin.py                 # Django Admin設定
│   └── management/commands/
│       ├── init_sample_data.py          # サンプルデータ初期化
│       └── scheduler_status.py          # スケジューラーステータス表示
├── tasks/
│   ├── camera.py                # カメラ画像取得・処理ロジック
│   └── __init__.py
├── media/                       # 取得画像保存先
├── logs/                        # ログ出力先
├── static/                      # 静的ファイル
├── manage.py
├── requirements.txt
├── README.md
├── start_server.sh              # サーバー起動スクリプト
├── spec/                        # 仕様ドキュメント
└── db.sqlite3                   # SQLiteデータベース
```

## 重要な特徴

### 1. Webサーバー起動時にタイマー自動起動

```python
# core/apps.py の ready() メソッドで自動起動
def ready(self):
    from camserver.scheduler import scheduler_instance
    scheduler_instance.start()
```

効果:
- `python manage.py runserver` を実行するだけでタイマータスクも自動起動
- 別プロセスを立てる必要がない
- スケジューラーはバックグラウンドで動作

### 2. 動的スケジュール変更（即時反映）

APIで取得間隔を変更すると、スケジューラーが即座に再設定されます：

```bash
# 例: 取得間隔を5分から3分に変更
PATCH /api/v1/cameras/{id}/update_schedule/
{
  "capture_interval_minutes": 3
}
```

### 3. 権限別アクセス制御

各APIエンドポイントがユーザーの権限に基づいてデータをフィルタリング：
- **システム管理者**: すべてのデータ参照可能
- **企業管理者**: 属する企業のデータのみ参照可能
- **現場管理者**: 属する現場のデータのみ参照可能
- **一般ユーザー**: データ参照なし

### 4. 画像保存の階層構造

```
media/
├── company_001/
│   ├── site_001/
│   │   ├── camera_001/
│   │   │   ├── 2026/
│   │   │   │   ├── 06/
│   │   │   │   │   └── 22/
│   │   │   │   │       ├── 20260622_153010_123456.jpg
│   │   │   │   │       ├── 20260622_153010_123456_thumb.jpg
│   │   │   │   │       └── ...
```

## トラブルシューティング

### Q: スケジューラーが起動しない

A: ログを確認してください：
```bash
tail -f logs/camserver.log
```

### Q: カメラ接続テストが失敗する

A: 以下を確認してください：
- カメラのURL（例: `http://192.168.1.100/snapshot.jpg`）
- BASIC認証のユーザー名・パスワード
- ネットワーク接続
- ファイアウォール設定

### Q: スケジューラーのジョブが実行されない

A: 以下を確認してください：
```bash
# スケジューラーのステータス確認
python manage.py scheduler_status

# カメラが is_capturing=True になっているか
# Django Adminで確認: http://localhost:8000/admin/core/camera/
```

### Q: 画像が保存されない

A: 以下を確認してください：
```bash
# media ディレクトリの書き込み権限
ls -ld media/
chmod -R 755 media/

# ディスク容量確認
df -h

# ログでエラー確認
tail -f logs/camserver.log
```

## 次のステップ

### フロントエンド開発（別途対応）

- Vue.js/React を使用したUI開発
- API の認証処理（JWT または Session）
- 画像ギャラリー表示
- カレンダー日付選択

### 本番環境への移行（今後の対応）

- AWS Lambda + S3
- PostgreSQL への切り替え
- SSL/TLS設定
- 認証基盤の強化
- キャッシング戦略
- ログ集約

### 運用機能（今後の対応）

- 定期バックアップ
- ログローテーション
- 監視・アラート機能
- 圧縮・アーカイブ処理

## ライセンス

MIT License

---

**構築日**: 2026-06-22  
**仕様バージョン**: Ver0.8

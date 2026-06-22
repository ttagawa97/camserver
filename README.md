# インターネットカメラ静止画管理システム - Django REST API サーバー

## プロジェクト概要

複数台のインターネットカメラから定期的に静止画を取得し、企業・現場・カメラ・日付単位で保存・管理するWebベースの管理システムです。

### 主な特徴

- **複数企業・現場・カメラ管理**: 階層構造で効率的に管理
- **定期画像取得**: APSchedulerによる動的スケジュール実行
- **BASIC認証対応**: HTTP/HTTPSのカメラURLから画像取得
- **REST API**: フロントエンド連携用のAPIサーバー
- **権限管理**: システム管理者、企業管理者、現場管理者、一般ユーザー
- **自動初期化**: サンプルデータと自動スケジュール設定

## システム要件

- Python 3.8+
- Django 4.2
- SQLite3（開発環境）
- APScheduler 3.10

## インストール

### 1. Python環境セットアップ

```bash
# 仮想環境を作成
python -m venv venv

# 仮想環境を有効化
source venv/bin/activate  # Linux/macOS
# または
venv\\Scripts\\activate  # Windows
```

### 2. 依存パッケージをインストール

```bash
pip install -r requirements.txt
```

### 3. データベースマイグレーション

```bash
python manage.py migrate
```

### 4. スーパーユーザー作成（オプション、サンプルデータ使用時は不要）

```bash
python manage.py createsuperuser
```

### 5. サンプルデータ初期化

```bash
python manage.py init_sample_data
```

出力例:
```
System Admin: admin / admin123
Company Admin: company_admin / password123
Site Admin: site_admin / password123
General User: user / password123
```

## 開発サーバー起動

### 方法1: 標準runserver（スケジューラー自動起動）

```bash
python manage.py runserver
```

スケジューラーが自動的に起動し、設定されているカメラから定期的に画像を取得します。

### 方法2: カスタムコマンド

```bash
python manage.py runserver_scheduler
```

## プロジェクト構成

```
camserver/
├── camserver/
│   ├── settings.py       # Django設定
│   ├── urls.py           # ルーティング
│   ├── wsgi.py           # WSGI設定
│   ├── scheduler.py      # APScheduler管理
│   └── __init__.py
├── core/
│   ├── models.py         # モデル定義
│   ├── views.py          # ViewSet定義
│   ├── serializers.py    # シリアライザー
│   ├── urls.py           # API ルーティング
│   ├── admin.py          # Django Admin設定
│   ├── apps.py           # アプリ設定（スケジューラー自動起動）
│   ├── migrations/       # DB マイグレーション
│   └── management/
│       └── commands/
│           ├── init_sample_data.py      # サンプルデータ初期化
│           └── runserver_scheduler.py   # スケジューラー付きサーバー
├── tasks/
│   ├── camera.py         # カメラ画像取得ロジック
│   └── __init__.py
├── media/                # 取得画像保存先
├── logs/                 # ログ出力先
├── manage.py
├── requirements.txt
└── db.sqlite3
```

## API エンドポイント

### 認証

- `POST /api/v1/auth/login/` - ログイン
- `GET /api/v1/auth/me/` - ログインユーザー情報
- `POST /api/v1/auth/logout/` - ログアウト

### 企業管理

- `GET /api/v1/companies/` - 企業一覧
- `POST /api/v1/companies/` - 企業作成
- `GET /api/v1/companies/{id}/` - 企業詳細
- `PUT /api/v1/companies/{id}/` - 企業更新
- `DELETE /api/v1/companies/{id}/` - 企業削除

### 現場管理

- `GET /api/v1/sites/` - 現場一覧
- `POST /api/v1/sites/` - 現場作成
- `GET /api/v1/sites/{id}/` - 現場詳細
- `PUT /api/v1/sites/{id}/` - 現場更新
- `DELETE /api/v1/sites/{id}/` - 現場削除

### カメラ管理

- `GET /api/v1/cameras/` - カメラ一覧
- `POST /api/v1/cameras/` - カメラ登録
- `GET /api/v1/cameras/{id}/` - カメラ詳細
- `PUT /api/v1/cameras/{id}/` - カメラ更新
- `DELETE /api/v1/cameras/{id}/` - カメラ削除
- `POST /api/v1/cameras/{id}/test_connection/` - カメラ接続テスト
- `POST /api/v1/cameras/{id}/capture_now/` - 即座に画像取得
- `PATCH /api/v1/cameras/{id}/update_schedule/` - 取得間隔変更（動的反映）

### 画像参照

- `GET /api/v1/images/` - 画像一覧
- `GET /api/v1/images/{id}/` - 画像詳細
- `GET /api/v1/images/by_date_range/` - 日付範囲で画像取得
- `GET /api/v1/images/dates_with_images/` - 画像存在日付一覧
- `GET /api/v1/images/latest_images/` - 複数カメラ最新画像

### スケジュール参照

- `GET /api/v1/schedules/` - スケジュール一覧
- `GET /api/v1/schedules/{id}/` - スケジュール詳細

## Django Admin

- URL: `http://localhost:8000/admin/`
- ユーザー: `admin`
- パスワード: `admin123`

## スケジューラーの動作

1. **起動時**: Django アプリ起動時（`ready()`メソッド）に APScheduler が自動起動
2. **ジョブ登録**: 有効なカメラ（`is_capturing=True`）のスケジュール情報が DB から復元され、ジョブが登録される
3. **定期実行**: 各カメラの `capture_interval_minutes` に基づいて定期的に画像取得
4. **動的変更**: API を通じて取得間隔を変更すると、スケジューラーが即座に再設定
5. **画像保存**: 取得した画像は `media/company_code/site_code/camera_code/YYYY/MM/DD/` に保存

## カメラ設定例

```json
{
  "site": "uuid-of-site",
  "code": "camera_001",
  "name": "エントランスカメラ",
  "url": "http://192.168.1.100/snapshot.jpg",
  "username": "admin",
  "password": "password123",
  "capture_interval_minutes": 5,
  "save_quality": 85,
  "save_days": 30
}
```

## ログ出力

- コンソール: INFO レベル以上
- ファイル: `logs/camserver.log` に DEBUG レベル以上
- 各モジュール（core, tasks）のログを区分管理

## 注意事項

### 開発環境での使用

- SSL 証明書検証は無効化（`verify=False`）
- デバッグモード有効（`DEBUG=True`）
- シークレットキーはデフォルト値

### 本番環境へのデプロイ

- `settings.py` の `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` を調整
- SSL 証明書検証を有効化
- パスワード管理（暗号化、環境変数）
- ログローテーション設定
- キャッシュ、セッションバックエンド設定

## トラブルシューティング

### スケジューラーが起動しない

```bash
# ログを確認
tail -f logs/camserver.log

# djangoのログレベルを上げる
# settings.py で logging レベルを DEBUG に変更
```

### カメラ接続テストが失敗

- カメラの URL が正しいか確認
- BASIC認証のユーザー名・パスワードを確認
- ネットワーク接続を確認
- ファイアウォール設定を確認

### 画像が保存されない

- `media/` ディレクトリの書き込み権限を確認
- ディスク容量を確認
- ログファイルでエラーを確認

## ライセンス

MIT License

## 開発チーム

Initial Version: 2026-06-22

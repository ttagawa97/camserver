#!/bin/bash
# Djangoサーバーを起動するスクリプト

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 仮想環境を有効化
source venv/bin/activate

# マイグレーション実行
echo "Running migrations..."
python manage.py migrate > /dev/null 2>&1

# サーバー起動
echo "Starting Django development server on http://localhost:8000"
echo ""
echo "APIドキュメント:"
echo "  - 企業管理: http://localhost:8000/api/v1/companies/"
echo "  - 現場管理: http://localhost:8000/api/v1/sites/"
echo "  - カメラ管理: http://localhost:8000/api/v1/cameras/"
echo "  - 画像参照: http://localhost:8000/api/v1/images/"
echo ""
echo "Django Admin:"
echo "  - http://localhost:8000/admin/"
echo "  - ユーザー: admin"
echo "  - パスワード: admin123"
echo ""
echo "テストユーザー:"
echo "  - system_admin: admin / admin123"
echo "  - company_admin: company_admin / password123"
echo "  - site_admin: site_admin / password123"
echo "  - general_user: user / password123"
echo ""
echo "APSchedulerは自動起動します。Ctrl+Cで停止します。"
echo "=========================================================="
echo ""

python manage.py runserver 0.0.0.0:8000

# Suki-match

**半径500m以内の「助けたい」と「助けてほしい」を、15分単位のスキマ時間でつなぐ多世代共助マッチングアプリ。**

少子高齢化を「人口減」ではなく「コミュニティの接続切れ」と捉え、子育て世代の「ちょっとした頼み事」と、シニア・学生の「気軽な社会参加」を結びつけます。

- 例①: 保育園のお迎え後、親が帰宅するまでの15分だけ自宅で見守ってほしい
- 例②: スーパーへ行くついでに、重い荷物を近所の高齢者宅へ届けてほしい

## 主な機能

### アカウント
| 機能 | 内容 |
|---|---|
| 登録・ログイン | django-allauth。メールアドレス必須(確認メールあり)、ユーザー名・メールのどちらでもログイン可 |
| Googleログイン | 環境変数 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` を設定すると有効化。同じメールの既存アカウントと自動で連携 |
| アカウント設定 | ユーザー名・メールアドレス・パスワードの変更、Google連携、メール通知設定 |
| プロフィール | アイコン画像、「お手伝いする理由」(人柄の可視化)、自己紹介、スキル。ヘッダーのアイコンからマイページへ |

### 依頼とマッチング機能
| 機能 | 内容 |
|---|---|
| 募集方式の選択 | **先着順**(最初に押した人で即決定)/ **承認制**(応募者の中から依頼者が選ぶ) |
| 掲載期限 | 15分〜3時間から選択。一覧・詳細でカウントダウン表示(残り10分で赤字)。期限切れは自動で締め切り |
| 予約掲載 | 「手伝ってほしい日時」(30分〜7日後)を指定すると、その日時まで「予約」タブに掲載 |
| 定型タグ | 「詳細はチャットで」「詳細は現地で相談」など。詳細文の代わりに使える |
| 進行中バー | 進行中の依頼・承認待ちの応募を全画面の上部に常時表示 |
| キャンセルとペナルティ | マッチ成立から1分以内はペナルティなし。以降は直近30日の回数で 警告 → 受付停止24時間 → 全停止72時間 → 全停止7日(運営確認) |
| 半径500mマッチング | 現在地(24時間以内に更新されたもの)と、登録した活動拠点の両方を基準に、スキル条件が合う人へ通知 |

### スキル
- 候補から選ぶタグ入力(定番スキルはワンタップで追加)。候補にないスキルは新規登録(1人1日5件まで)
- 全角/半角・カタカナ/ひらがなの表記ゆれを吸収(「ｽﾏﾎ操作」と「スマホ操作」は同じタグ)
- 類似タグの統合: `python manage.py suggest_skill_merges` で候補を出し、管理画面のアクションで統合

### 位置情報とプライバシー
- 他の人に見えるのは「東京都北区赤羽」のような**エリア名だけ**(国土地理院の逆ジオコーダで変換)
- 一覧の距離は「200m以内 / 500m以内 / 1km以内」の段階表示。計算には約250m単位に丸めた座標を使い、何度も見比べて住所を割り出されないようにする
- **正確な住所・地図は、依頼者とマッチングしたヘルパーにだけ表示**
- 現在地が取得できないときは、原因別のエラーを表示(許可されていない / 取得できない / タイムアウト / HTTPSでない)。地図のタップでも指定可能

### コミュニケーションと安全
| 機能 | 内容 |
|---|---|
| チャット | マッチした2人だけのリアルタイムチャット(WebSocket)。完了後3日まで利用可 |
| チャット監視 | 連絡先交換・出会い目的・性的な内容・金銭の話題を検知してスコア化し、一方的な連投や深夜帯も加点する。初期設定は**記録のみ**(管理画面 `/admin/core/message/?review=1` で確認) |
| 通報 | ユーザー・依頼・メッセージを通報できる。30日以内に3人以上から通報されたら、運営が確認するまで自動で受付停止 |

### ランクと通知
- **レベル1〜100**(ポイントの平方根で算出)と、レベル帯のランク: ビギナー(1〜) → レギュラー(6〜) → **アクティブ(11〜、新設)** → ベテラン(16〜) → マスター(26〜)
- 完了で +10pt、高評価(★4以上)のレビューで +2pt
- アプリ内通知(履歴あり)、WebSocketでの即時通知に加え、**メール通知**(アプリを開いていないときに未読をまとめて送信。静かな時間帯も設定できる)
- 依頼が出やすい時間帯の統計(エリア別)

## 技術構成

- Python 3.12 / Django 5.1
- Django Channels + Daphne(WebSocket)
- django-allauth(認証・Googleログイン)
- DB: SQLite(開発)/ `DATABASE_URL` で PostgreSQL に切り替え可能
- Redis: `REDIS_URL` を設定するとチャンネルレイヤーとキャッシュに使用(本番では必須)
- Leaflet + OpenStreetMap(地図での位置指定)、国土地理院 逆ジオコーダAPI(エリア名)

## セットアップ

```bash
python -m venv venv
venv\Scripts\activate        # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

http://127.0.0.1:8000/ を開きます。

### メール送信(.env)

`.env.example` をコピーして `.env` を作り、送信用Gmailのアドレスとアプリパスワードを書くと、確認メールや通知メールが実際に送信されます。`.env` は起動時に自動で読み込まれ、Gitには含まれません。`.env` がない場合、メールは `runserver` のコンソールに出力されるだけです。

### 定期処理

掲載期限切れの締め切りとメールのまとめ送信を行います。本番ではcronやタスクスケジューラで数分おきに実行してください。期限切れの判定は画面表示時にも行うので、開発中は動かさなくても動作します。

```bash
python manage.py run_periodic_tasks            # 1回実行
python manage.py run_periodic_tasks --loop 60  # 開発用: 60秒ごとに繰り返す
```

### 環境変数

| 変数 | 用途 | 既定値 |
|---|---|---|
| `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` / `DJANGO_ALLOWED_HOSTS` | 本番設定 | 開発用の値 |
| `DATABASE_URL` | 例: `postgres://user:pass@localhost:5432/sukimatch` | SQLite |
| `REDIS_URL` | 例: `redis://localhost:6379/0` | インメモリ |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Googleログイン | 無効 |
| `ACCOUNT_EMAIL_VERIFICATION` | `mandatory` / `optional` / `none` | `mandatory` |
| `EMAIL_BACKEND` / `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | メール送信(SES・SendGrid等のSMTP) | コンソール出力 |
| `SITE_URL` | 通知メール内のリンク | `http://127.0.0.1:8000` |
| `GEOCODER_ENABLED` | エリア名の取得(オフライン開発では `0`) | `1` |
| `MODERATION_AUTO_HIDE` | `1` で高リスクのメッセージを自動で非表示 | `0`(記録のみ) |
| `MODERATION_REVIEW_THRESHOLD` / `MODERATION_HIDE_THRESHOLD` | 監視の閾値 | `0.5` / `0.8` |

Googleログインを使うには、Google Cloud Console で OAuth クライアントを作成し、承認済みのリダイレクトURIに `http://127.0.0.1:8000/accounts/google/login/callback/` を登録してください。

## テスト

```bash
python manage.py test
```

## ディレクトリ構成

```
config/                  設定(環境変数で本番/開発を切り替え)
core/
  models.py              User, Skill, PresetTag, HelpRequest, Application, Match, Review,
                         Sanction, Message, Report, Notification, NotificationSetting, ActivityBase
  services.py            マッチング・キャンセル/ペナルティ・通知・チャット・通報・統計のロジック
  moderation.py          チャット監視(ルールと行動パターンによるスコア)
  geocoding.py           逆ジオコーディング(座標 → エリア名)
  skills.py              スキルタグの検索・追加・類似度・統合
  consumers.py           WebSocket(通知、チャット)
  context_processors.py  進行中バー・未読数
  management/commands/   run_periodic_tasks, suggest_skill_merges
  templates/core/        画面
  templates/allauth/     ログイン等の画面をアプリのレイアウトに載せる上書き
  static/core/           style.css, app.js(カウントダウン・地図・タグ入力・WebSocket)
```

## 運用メモ・今後の課題

- **乱造防止**: Googleアカウントは1人で複数作れるため、Googleログインだけでは完全には防げません。SMSによる電話番号認証(Twilio Verify など)の追加を推奨します。
- **チャット監視**: β期間は記録のみで運用し、通報の結果と照らし合わせて閾値を調整してください。文脈を見るAI分類(LLM)は未導入です。導入する場合は、外部へ会話を送ることを利用規約に明記してください。
- **スキル検索**: 表記ゆれ(全角/半角・カタカナ/ひらがな)は吸収しますが、漢字と読み(「見守り」と「みまもり」)は結びつけません。読み仮名の項目を追加するか、形態素解析の導入を検討してください。類似タグの判定は文字列の近さのみで、意味の近さは未対応です。
- **外部サービス**: エリア名の取得に国土地理院API、地図にOpenStreetMapのタイルを使っています。利用が増えたら、各サービスの利用規約や負荷の目安を確認してください。
- **プッシュ通知**: Web Push(PWA)は未実装です。
- **本番運用**: `REDIS_URL` と PostgreSQL を設定し、`DJANGO_DEBUG=0` で `collectstatic` を実行し、Daphne などのASGIサーバーで起動してください。

## 出典

- 内閣府『高齢社会白書』『高齢者の日常生活・地域社会への参加に関する調査』
- PIAZZA株式会社「『孤育て』実態調査」(2024年)

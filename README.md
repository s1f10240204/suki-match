# Suki-match

**半径500m以内の「助けたい」と「助けてほしい」を、15分単位のスキマ時間でつなぐ多世代共助マッチングアプリ。**

少子高齢化を「人口減」ではなく「コミュニティの接続切れ」と捉え、子育て世代の「ちょっとした頼み事」と、シニア・学生の「気軽な社会参加」を結びつけます。

- 例①: 保育園のお迎え後、親が帰宅するまでの15分だけ自宅で見守ってほしい
- 例②: スーパーへ行くついでに、重い荷物を近所の高齢者宅へ届けてほしい

## 主な機能

### アカウント
| 機能 | 内容 |
|---|---|
| 登録・ログイン | django-allauth。メールに届く**6桁の確認コード**を入力して登録(60秒ごとに再送可、最大3回)。ユーザー名・メールのどちらでもログイン可 |
| Googleログイン | `.env` に `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` を設定すると、ログイン・登録画面に「Google」ボタンが出る(下の「Googleログインの設定」参照) |
| アカウント設定 | ユーザー名・メールアドレス・パスワードの変更、Google連携、メール通知設定 |
| 顔写真(必須・審査制) | 依頼・応募には顔写真が必要。アップロード時に撮影場所などのEXIFを削除し、運営が管理画面で承認すると利用可能になる。**マッチング成立前の相手にはサーバー側で作ったモザイク画像**を表示し、成立後に自動で解除 |
| プロフィール | 「お手伝いする理由」(人柄の可視化)、自己紹介、スキル。ヘッダーのアイコンからマイページへ |

### 依頼とマッチング機能
| 機能 | 内容 |
|---|---|
| 募集方式の選択 | **先着順**(最初に押した人で即決定)/ **承認制**(応募者の中から依頼者が選ぶ) |
| 依頼フォーム | 「内容 → 日時 → 場所 → 確認」のステップ入力。必須はタイトル・場所だけで、他は既定値つき |
| 掲載期限 | 任意(未指定なら3時間)。30分〜7日から選べる。一覧・詳細でカウントダウン表示(残り10分で赤字) |
| 日時指定 | 「日時を指定」(30分〜7日後)を選ぶと、その日時まで「日時指定」タブに掲載 |
| おすすめ表示 | 近くの依頼はスキルに関係なくすべて表示し、登録スキルと一致するものを「あなたにおすすめ」として上に強調表示 |
| 定型タグ | 「詳細はチャットで」「詳細は現地で相談」など。詳細文の代わりに使える |
| 進行中バー | 進行中の依頼・承認待ちの応募を全画面の上部に常時表示 |
| キャンセルとペナルティ | マッチ成立から1分以内はペナルティなし。以降は直近30日の回数で 警告 → 受付停止24時間 → 全停止72時間 → 全停止7日(運営確認) |
| 半径500mマッチング | 現在地(24時間以内に更新されたもの)と、登録した活動拠点の両方を基準に、スキル条件が合う人へ通知 |

### スキル
- 候補から選ぶタグ入力(定番スキルはワンタップで追加)。候補にないスキルは新規登録(1人1日5件まで)
- 全角/半角・カタカナ/ひらがなの表記ゆれを吸収(「ｽﾏﾎ操作」と「スマホ操作」は同じタグ)
- 読みでも検索できる(「みま」→「見守り」)。候補が少ないときは似ているタグも提案(「子守り」→「見守り」)
- 類似度は文字2-gramのJaccard係数と正規化レーベンシュタイン距離を、表記と読み(pykakasi)の両方で計算
- 類似タグの統合: `python manage.py suggest_skill_merges` で候補を出し、管理画面のアクションで統合

### 位置情報とプライバシー
- 他の人に見えるのは「東京都北区赤羽」のような**エリア名だけ**(国土地理院の逆ジオコーダで変換)
- 一覧の距離は「200m以内 / 500m以内 / 1km以内」の段階表示。計算には約250m単位に丸めた座標を使い、何度も見比べて住所を割り出されないようにする
- **正確な住所・地図は、依頼者とマッチングしたヘルパーにだけ表示**
- 場所は**住所・駅名での検索**、現在地、地図のタップ・ピンのドラッグで指定できる。現在地が取れないときは原因別のエラーを表示
- 地図は `GOOGLE_MAPS_API_KEY` があれば Google マップ、なければ OpenStreetMap(住所検索は国土地理院API)で表示

### コミュニケーションと安全
| 機能 | 内容 |
|---|---|
| チャット | マッチした2人だけのリアルタイムチャット(WebSocket)。完了後3日まで利用可。マッチングしていないときにメッセージタブを押すと、移動せずに案内を表示 |
| チャット監視 | 連絡先交換・出会い目的・性的な内容・金銭の話題を検知してスコア化し、一方的な連投や深夜帯も加点する。初期設定は**記録のみ**(管理画面 `/admin/core/message/?review=1` で確認) |
| 通報 | ユーザー・依頼・メッセージを通報できる。30日以内に3人以上から通報されたら、運営が確認するまで自動で受付停止 |

### ランクと通知
- **レベル1〜100**(ポイントの平方根で算出)と、レベル帯のランク: ビギナー(1〜) → レギュラー(6〜) → **アクティブ(11〜、新設)** → ベテラン(16〜) → マスター(26〜)
- 完了で +10pt、高評価(★4以上)のレビューで +2pt
- アプリ内通知(履歴あり)、WebSocketでの即時通知に加え、**メール通知**(アプリを開いていないときに未読をまとめて送信。静かな時間帯も設定できる)
- 依頼が出やすい時間帯の統計(エリア別)

## 技術構成

- Python 3.12 / Django 5.2
- Django Channels + Daphne(WebSocket)
- django-allauth(認証・Googleログイン)
- DB: SQLite(開発)/ `DATABASE_URL` で PostgreSQL に切り替え可能
- Redis: `REDIS_URL` を設定するとチャンネルレイヤーとキャッシュに使用(本番では必須)
- Google Maps JavaScript API + Geocoding API(未設定時は Leaflet + OpenStreetMap、国土地理院の住所検索)
- 国土地理院 逆ジオコーダAPI(エリア名)、Pillow(顔写真の処理)、pykakasi(スキルの読み)

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

### 顔写真の審査(運営)

管理画面 `/admin/core/user/?avatar_status__exact=pending` で審査待ちのユーザーを開き、写真を確認して「顔写真を承認する」または「差し戻す」アクションを実行します。結果は本人に通知されます。

### Googleログインの設定

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作り、「APIとサービス」→「OAuth同意画面」を設定する
2. 「認証情報」→「OAuthクライアントID」(種類: ウェブアプリケーション)を作成し、承認済みのリダイレクトURIに `http://localhost:8000/accounts/google/login/callback/` を追加する(本番ではそのドメインのURIも追加)
3. 表示されたクライアントIDとシークレットを `.env` の `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` に書き、サーバーを再起動する

### Google Maps の設定

1. Google Cloud Console で「Maps JavaScript API」と「Geocoding API」を有効にし、APIキーを作る
2. キーの「アプリケーションの制限」で HTTPリファラー(例: `http://localhost:8000/*`)に限定する(キーはブラウザに渡るため)
3. `.env` の `GOOGLE_MAPS_API_KEY` に書き、サーバーを再起動する

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
| `GOOGLE_MAPS_API_KEY` / `GOOGLE_MAPS_MAP_ID` | Googleマップ(地図・住所検索) | 未設定ならOpenStreetMap |
| `SITE_DOMAIN` | メール本文に載せるドメイン(サービス名は suki-match 固定) | `localhost:8000` |
| `ACCOUNT_EMAIL_VERIFICATION` | `mandatory` / `optional` / `none` | `mandatory` |
| `EMAIL_BACKEND` / `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | メール送信(SES・SendGrid等のSMTP) | コンソール出力 |
| `SITE_URL` | 通知メール内のリンク | Renderでは公開URL、それ以外は `http://127.0.0.1:8000` |
| `BREVO_API_KEY` | 設定するとメールを Brevo のAPIで送る(Render無料プラン向け) | 未設定 |
| `MEDIA_IN_DATABASE` | `1` で顔写真をDBに保存 | `0` |
| `TASKS_TOKEN` | 定期処理 `/tasks/run/` を呼ぶための合言葉 | 未設定(無効) |
| `GEOCODER_ENABLED` | エリア名の取得(オフライン開発では `0`) | `1` |
| `MODERATION_AUTO_HIDE` | `1` で高リスクのメッセージを自動で非表示 | `0`(記録のみ) |
| `MODERATION_REVIEW_THRESHOLD` / `MODERATION_HIDE_THRESHOLD` | 監視の閾値 | `0.5` / `0.8` |


## Render で公開する

GitHub Pages は静的ファイル(HTML/CSS/JS)しか置けないため、サーバーやデータベースが必要なこのアプリは動きません。[Render](https://render.com/) の無料プランで公開します。

### 無料プランの制約と、このリポジトリでの対応

| 制約 | 対応 |
|---|---|
| SMTP(587番ポートなど)が使えない → Gmail から送れない | [Brevo](https://www.brevo.com/) の HTTPS API で送る(`BREVO_API_KEY`) |
| ディスクが再起動で消える | 顔写真をデータベースに保存(`MEDIA_IN_DATABASE=1`、`core/storage.py`) |
| シェル・cron が使えない | 管理者はビルド時に自動作成(`build.sh`)、定期処理は GitHub Actions から呼ぶ |
| 15分アクセスがないとスリープ(次のアクセスで約1分かけて起動) | 仕様として受け入れる |
| 無料のデータベースは作成から30日で期限切れ(さらに14日後に削除) | 期限前に有料プランへ変更するか、[Neon](https://neon.tech/) など期限のない無料DBへ移す |

### 1. Brevo(メール送信)の準備

1. Brevo に無料登録する(1日300通まで無料)
2. 「Senders, Domains & Dedicated IPs」→「Senders」で送信元(例: `noreply.skimatch@gmail.com`)を追加し、届いた確認メールで認証する
3. 「SMTP & API」→「API Keys」で APIキーを発行して控える

Gmail などのフリーメールを送信元にすると、Brevo が送信元アドレスを自動で置き換えて配信します。届きはしますが、独自ドメインを取って Brevo で認証すると確実です。

### 2. コードを GitHub に push する

`render.yaml`・`build.sh`・`.github/workflows/periodic-tasks.yml` を含めてコミットし、GitHub に push します。

### 3. Render で Blueprint を作成する

1. Render に GitHub アカウントで登録し、「New」→「Blueprint」を選ぶ
2. このリポジトリを選ぶと `render.yaml` が読み込まれ、Webサービス `suki-match` と データベース `suki-match-db` が作られる
3. 入力を求められる値を設定して「Apply」を押す

| 変数 | 入れる値 |
|---|---|
| `BREVO_API_KEY` | 手順1のAPIキー |
| `DEFAULT_FROM_EMAIL` | `suki-match <noreply.skimatch@gmail.com>`(Brevoで認証した送信元) |
| `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_EMAIL` / `DJANGO_SUPERUSER_PASSWORD` | 管理者アカウント(初回のビルドで自動作成) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_MAPS_API_KEY` | 使う場合のみ。空欄でも動く |

4. ビルドが終わると `https://suki-match.onrender.com` のようなURLで公開される(名前が使われていれば別のURLになる)

### 4. 公開後の設定

- **管理画面**: `https://<公開URL>/admin/` に手順3の管理者でログインし、顔写真の審査を行う
- **定期処理**: GitHub のリポジトリの「Settings」→「Secrets and variables」→「Actions」で、
  - Variables に `APP_URL` = 公開URL(例: `https://suki-match.onrender.com`)
  - Secrets に `TASKS_TOKEN` = Render の Environment にある `TASKS_TOKEN` の値
  
  を登録すると、30分ごとに期限切れの締め切りとメールのまとめ送信が実行される(「Actions」タブから手動実行も可能)
- **Googleログイン**: OAuthクライアントの承認済みリダイレクトURIに `https://<公開URL>/accounts/google/login/callback/` を追加
- **Google Maps**: APIキーの HTTPリファラー制限に `https://<公開URL>/*` を追加

### 更新のしかた

`main` ブランチに push すると、Render が自動でビルドし直して公開を更新します(マイグレーションもビルド時に実行)。

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
  images.py              顔写真の処理(EXIF除去・縮小・モザイク生成)
  adapters.py            allauth のメール送信(サービス名 suki-match を差し込む)
  context_processors.py  進行中バー・未読数・地図の設定
  management/commands/   run_periodic_tasks, suggest_skill_merges
  templates/core/        画面
  templates/allauth/     ログイン等の画面をアプリのレイアウトに載せる上書き
  templates/account/     確認コード入力画面と、日本語の認証メール
  static/core/           style.css, app.js(ステップ入力・地図/住所検索・カウントダウン・タグ入力・WebSocket)
```

## 運用メモ・今後の課題

- **乱造防止**: Googleアカウントは1人で複数作れるため、Googleログインだけでは完全には防げません。SMSによる電話番号認証(Twilio Verify など)の追加を推奨します。
- **チャット監視**: β期間は記録のみで運用し、通報の結果と照らし合わせて閾値を調整してください。文脈を見るAI分類(LLM)は未導入です。導入する場合は、外部へ会話を送ることを利用規約に明記してください。
- **スキルの類似度**: 表記と読みの近さで判定しており、意味の近さ(「子守り」と「ベビーシッター」など)は判定しません。タグが数千件を超えて言い換えが増えたら、多言語の文埋め込み(例: multilingual-e5-small)による意味検索の追加を検討してください。
- **顔写真**: 写真ファイルは `/photos/<id>/` からだけ配信し、審査前の写真は本人と運営以外には見せません。本番ではファイルをS3などの非公開ストレージに置いてください。
- **外部サービス**: エリア名の取得に国土地理院API、地図にGoogle Maps(未設定時はOpenStreetMap)を使っています。利用が増えたら、各サービスの利用規約や負荷の目安を確認してください。
- **プッシュ通知**: Web Push(PWA)は未実装です。
- **本番運用**: 公開手順は「Render で公開する」を参照。複数のサーバーで動かす規模になったら `REDIS_URL` を設定してください(1台なら不要)。

## 出典

- 内閣府『高齢社会白書』『高齢者の日常生活・地域社会への参加に関する調査』
- PIAZZA株式会社「『孤育て』実態調査」(2024年)

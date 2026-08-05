# GiftBot
Discord bot.
Executing the gift code command will automatically redeem the gift code for all registered players.

複数ギルドでの同時運用に対応。ギルドごとに`/setup`で登録した1チャンネル内の3つの「コメント」(Botが投稿・編集するメッセージ)がデータの実体となる。

## Directory Tree

```text
GiftBot/
│
├── main.py            起動処理
├── config.py          設定・権限・エラー文言
├── commands.py        スラッシュコマンド本体
├── views_discord.py   確認ダイアログ・永続View(ボタン/Select)
├── chat_manager.py     ギルド登録・コメント管理・ギルド単位の排他ロック
│
├── exchange.py         複数プレイヤー分の交換ループ制御
├── wos_api_client.py    WOSギフトコードAPIへのHTTP POSTクライアント
│
├── file_manager.py      汎用ファイルI/O(CSV, git/local)
│
├── data/
│   └── chat_settings.csv    (local保存)ギルド・チャンネル登録情報
│
├── Dockerfile
└── requirements.txt
```

`players.csv`・`history.csv`は**ファイルとして常設されない**。各ギルドの登録チャンネル内に投稿される3つのコメント(`players_data`・`history_data`・`last_result`)がデータの唯一のソース。

---

# データの保存方式(重要)

```
/setup 実行
      |
      v
登録チャンネルに players_data -> history_data -> last_result の順で投稿
      |
      v
/add_player, /delete_player, /giftcode, players_dataの「📤インポート」ボタン
      |
      v
該当コメントを読み込み(read_*_comment)
      |
      v
内容を書き換えてedit(write_*_comment)
```

## players_data(1メッセージで完結)

- 正データは**添付ファイル(players_data.csv)**として保持する。人数上限は無い(設定上限は`PLAYER_MAX_COUNT`)。
- メッセージ本文には50人区切り(`PLAYERS_PAGE_SIZE`)の**プレビュー**のみ表示する。
- 50人を超える場合、本文下部の**Select(ページ切り替え)**でプレビュー表示だけを切り替える(添付ファイルは書き換えない。常に1メッセージのまま)。
- 「📤 インポート」ボタン(custom_id固定の永続View)で、チャンネル内のユーザー投稿CSV添付を取り込み、全員分を丸ごと置き換える(旧`/load_csv`)。
- バックアップ専用ボタンは無い。**添付ファイル自体がいつでもダウンロード可能な最新バックアップ**として機能する。
- インポート時、有効データが`PLAYER_MAX_COUNT`(既定500人)を超える場合は**先頭からその人数だけ取り込み**、超過分は警告メッセージ付きで読み込まない。

## history_data

- ギフトコードごとに`date, gift_code, remain`を1行で管理。
- 同一コードが再実行された場合、既存行を削除し`remain`を今回の成功者数だけ減算して1件に統合する。
- `remain`は「ライセンス無し(`license=False`)ギルドが、このコードであと何回成功できるか」(初期値`FREE_LICENSE_SUCCESS_LIMIT`)。`remain<=0`になったコードは、ライセンス無しギルドでは`/giftcode`実行前にブロックされる。
- **fatal復旧機構**: `/giftcode`実行前にhistory_dataコメントの生存確認を行い、削除されていた場合は`fatal`(初期値`FATAL_MAX`)が残っている限り自動的に空のhistory_dataを再作成し、`fatal`を1消費する。`fatal`が0になったギルドは登録情報自体は残るが「無効化」状態になり、`/help`以外の全コマンドが使用不可になる(アクティブなギルド登録数にもカウントされない)。

## last_result(ダッシュボード)

`/giftcode`実行中・完了時に**このメッセージだけをEDIT**する(新規投稿はしない)。

```
最後に交換したギフトコード: `xxxxxx`
交換日:2026/8/3 21:25
経過時間:1分26秒
✅:成功 26人
☑️:交換済み 13人
👤:存在しないプレイヤー 5人
❌:それ以外のエラー 3人
--:未交換 0人
🔁:待機中(API制限)   ← レート制限で待機中のときだけ表示
Bot状態: 処理中/待機中/完了/⚠️レート制限により中断
```

- 「コードをコピー」ボタン: 直近のコードをコードブロック付きでephemeral表示(コピーしやすくするため)。
- 「ログの詳細」ボタン: 成功・交換済みを除く結果(存在しないプレイヤー/エラー/未交換)を、処理の先頭から最大50件、Embedで表示。
- `REPORTING_NUMBER`人ごとに、players_dataとlast_resultの両方を再EDITする。
- レート制限(API 429等)が**3回連続**で発生した場合、無限待機せず安全に処理を打ち切る(現在および残りのプレイヤーは全員「未交換」に分類される)。この打ち切り専用のカウンタはCONTINUE系の結果処理とは完全に分離しており、成功・失敗を問わずRETRY以外の結果が出るたびに0にリセットされる。

---

# ギルド単位の排他制御

`/giftcode`・`/add_player`・`/delete_player`(確認ボタン)・players_dataの「📤インポート」ボタンは、**ギルドごとの`asyncio.Lock`**で排他制御されている(`chat_manager.get_guild_lock(guild_id)`)。

- ロックのキーは常に「そのコマンドを呼び出したインタラクションのguild.id」のみ。channel_idや他の値では絶対にキーにしない。
- 既に別の処理が進行中(`lock.locked()`)の場合は、待たせずに即座に「他の処理が進行中です」と案内して終了する(Discordのインタラクション応答時間制限を考慮し、ロック解放を待機させることはしない)。
- ロックはあくまで**同一ギルド内**の排他制御であり、別ギルドの処理には一切影響しない(ギルドAの`/giftcode`実行中でも、ギルドBは通常通りコマンドを実行できる)。
- discord.py自体が単一のasyncioイベントループ上で動くため、ここでの「排他制御」はOSスレッドではなく`asyncio.Lock`を用いている。

---

# ギルド最大登録数・プレイヤー上限

| 項目 | 定数(config.py) | 既定値 |
|---|---|---|
| 同時登録可能ギルド数(fatal>0のみ) | `GUILD_MAX_COUNT` | 10 |
| history復旧の残り回数初期値 | `FATAL_MAX` | 3 |
| ライセンス無しギルドの同一コード成功上限 | `FREE_LICENSE_SUCCESS_LIMIT` | 50 |
| 1ギルドあたりのプレイヤー登録上限 | `PLAYER_MAX_COUNT` | 500 |

未登録ギルドが`/setup`以外のコマンドを叩いた場合、残り登録可能数(`GUILD_MAX_COUNT - アクティブ数`)を案内する。上限に達している場合は「これ以上登録できません」と案内する。

---

# Python Files

## main.py

Bot起動・Discordログイン・スラッシュコマンド同期。`on_ready`で`bot.add_view(PlayersDataView())`を呼び、players_dataの「📤インポート」ボタンをBot再起動後も動作する永続Viewとして登録する。

⚠️ 既知の制約: `Intents.default()`には特権インテント`message_content`が含まれていない。このインテントが無いと、Bot以外が投稿したメッセージの`content`/`attachments`はDiscord側の仕様で空になる。インポートボタンがチャンネル内のユーザー投稿CSVを見つけられない場合、intentの問題である可能性がある。その場合でも処理は例外にはならず、「見つかりませんでした」という結果を返して安全に終了する。

---

## commands.py

```
├── /help           全員, ギルド外でも利用可
├── /add_player     プレイヤー登録
├── /get_player     登録人数のみ表示(動作確認用)
├── /delete_player  プレイヤー削除(確認ダイアログ経由)
├── /giftcode       ギフトコード交換(はい/いいえ→人数選択→実行)
├── /setup          利用チャンネル登録(テキストチャンネル限定)
└── /reject         登録解除
```

`/help`以外の全コマンドは`require_guild`→`require_registered_channel`を通過する必要がある(未登録・チャンネル不一致・無効化ギルドはここで弾かれる)。

---

## views_discord.py

```
├── ConfirmDeleteView   delete_playerの削除確認(ロック付き)
├── GiftCodeConfirmView giftcodeの全員交換 はい/いいえ
├── GiftCodeCountView   いいえ選択後の人数選択(1人/5人/10人)
├── PageSelect          players_dataのページ切り替え(Select)
├── PlayersDataView     players_data用の永続View(インポートボタン+PageSelect)
└── LastResultView      last_result用View(コードコピー+ログ詳細、都度再生成)
```

`PlayersDataView`はcustom_id固定でBot再起動後も動作する。`LastResultView`は都度新しいインスタンスを生成して渡す方式のため、再起動直後〜次のgiftcode実行までの間だけ、既存メッセージ上の古いボタンが一時的に反応しなくなる(メッセージ自体が削除されていた場合は次回のgiftcode実行時に新規投稿されるので実害は無い)。

---

## chat_manager.py

```
├── PLAYER_FIELDS / HISTORY_FIELDS / CHAT_FIELDS
├── require_guild / require_registered_channel   (commands.py, views_discord.py共通)
├── get_guild_lock                                ギルド単位のasyncio.Lock
├── exist / create / save / delete / update_message_ids
├── has_license / is_disabled / consume_fatal
├── check_code_limit / record_history_success
├── ensure_history_comment                        history_data生存確認+fatal復旧
├── post_initial_comments                         /setup直後に3コメントを投稿
├── read_players_comment / write_players_comment   添付ファイル方式・ページプレビュー対応
├── read_history_comment / write_history_comment
├── write_last_result
└── import_players                                Bot投稿分を除外してCSV添付を検索
```

---

## exchange.py

```
├── format_time
├── _categorize            (success, message) -> 5分類のいずれかに変換
└── run_exchange            交換ループ本体。on_progressコールバックで
                             REPORTING_NUMBER人ごと・レート制限待機時・
                             完了時にダッシュボード更新を呼び出す
```

レート制限(RETRY)専用のカウンタを持ち、**3回連続**で発生すると安全に処理を打ち切る(現在のプレイヤー含め残りは「未交換」に分類)。このカウンタはCONTINUE系の結果(成功/交換済み/存在しないプレイヤー/その他エラー)では一切増減せず、RETRY以外の結果が出るたびに0へリセットされる。

---

## wos_api_client.py

WOSギフトコード交換APIへのHTTP POSTクライアント(旧autoplay2.py)。`redeem_gift_code`は呼び出し側(`exchange.run_exchange`)が生成した`requests.Session`を受け取り、対象プレイヤー全員の処理が終わるまで使い回す(TCP/TLS接続の再利用)。

---

## file_manager.py

汎用CSVレコード管理クラス(git/local両対応)。**現状、実際に使われているのは`storage="local"`(chat_settings.csv)のみ**で、GitHubバックエンド(`storage="git"`)はどこからも呼ばれていない。クラス自体は汎用なので残しているが、将来的に完全ローカル運用に統一するなら削除候補。

---

## config.py

主要な設定値:

- `WOS_API_URL` / `WOS_SECRET` : ギフトコード交換APIの接続情報
- `PERMISSIONS` / `ROLE_ADMIN` / `ROLE_MANAGER` : コマンド権限
- `GUILD_MAX_COUNT` / `FATAL_MAX` / `FREE_LICENSE_SUCCESS_LIMIT` / `PLAYER_MAX_COUNT` : 各種上限
- `REPORTING_NUMBER` : players_data/last_resultの再EDIT頻度
- `RESULT_*` : exchange_statusの5分類ラベル

`PLAYER_FIELDS` / `HISTORY_FIELDS` / `CHAT_FIELDS`はchat_manager.pyに移管済みで、このファイルには無い。

---

# Data Flow

```text
Discord User
      |
      v
   main.py
      |
      v
 commands.py ---- get_guild_lock(guild_id)で同一ギルド内の処理を排他
      |
      +------------------+------------------+
      |                  |                  |
      v                  v                  v
chat_manager.py      exchange.py       file_manager.py
      |                  |                  |
      |                  v                  v
      |          wos_api_client.py    chat_settings.csv
      |                  |
      |                  v
      |         WOS Gift Exchange API
      v
登録チャンネルの
players_data / history_data / last_result コメント
```

---

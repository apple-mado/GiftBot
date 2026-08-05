# config.py
import os

"""
GiftBot Configuration
"""

# ==========================
# Exchange Site
# ==========================

GIFT_URL = "https://wos-giftcode.centurygame.com"
WOS_API_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
WOS_SECRET = os.getenv("WOS_SECRET")

# ==========================
# Master Password
# ==========================
# /guild_list, /guild_delete等、Discordのロール権限とは別枠で
# 運営(開発者)のみが使う管理者コマンドの認証パスワード。
MASTER_PASSWORD = "2131X"

# ==========================
# Role Name
# ==========================
#discord の中のロール名で権限を付与。
ROLE_ADMIN = "GiftBot_Admin"
ROLE_MANAGER = "GiftBot_Manager"
# ==========================
# Help COMMAND CONTENT
# ==========================

VERSION = "5.0"
BOTNAME = "GiftBot"
HELP_CONTENT=(
    f"🤖 Bot: {BOTNAME}\n"
    f"Version: {VERSION}\n"
    "Developer: Motto(state:2131,ali:YyY)\n"
    "Storage:Github\n"
    "VPS:conohaVPS\n"
    "adviser:Claude Sonnet5\n\n"

    f"{BOTNAME}を招待してくれてありがとう！\n\n"
    f"１，初めて{BOTNAME}を使う場合は適当なテキストチャンネルで/setupコマンドを実行しましょう。"
    f"{BOTNAME}が鯖とチャンネルを登録します。チャンネル名は後から変更してもOK"
    f"{BOTNAME}の投稿は進捗や結果を更新するので削除しないでください。誤って削除しても３回までは自動復旧します。\n\n"
    f"２，鯖のロールに{ROLE_ADMIN}と{ROLE_MANAGER}を作成してメンバーにロールをつけてください（各コマンドの権限は以下を参照）\n\n"
    "これで準備は完了です。良きホワサバライフを！\n\n"
    "ご不明な点はDeveloperまでお問合せください\n\n"
)
    

# ==========================
# COMMAND PERMISSION
# ==========================



PERMISSIONS = {
    "giftcode": {
        "description": "ギフトコードの自動交換",
        "roles": [ROLE_MANAGER, ROLE_ADMIN]
    },
    "get_player": {
        "description": "登録人数を確認(動作確認用)",
        "roles": []
    },
    "add_player": {
        "description": "プレイヤーを登録",
        "roles": []
    },
    "delete_player": {
        "description": "プレイヤーを削除",
        "roles": [ROLE_ADMIN]
    },
    "setup": {
        "description": "GiftBotの利用チャンネルを登録",
        "roles": [ROLE_ADMIN]
    },
    "reject": {
        "description": "登録済みの利用チャンネルを解除",
        "roles": [ROLE_ADMIN]
    }

}


# ==========================
# Storage Settings
# ==========================

# Local Storage
# プレイヤーデータ・履歴は登録チャンネルのplayers_data / history_data
# コメントが唯一のソースであり、CSVとしての入出力はplayers_dataコメントに
# 常時添付されているplayers_data.csv(ダウンロードするだけでバックアップになる)
# と、同コメント上の「📤 インポート」ボタンがDiscord添付ファイル経由で行う。

# GitHub Storage
# (現在このプロジェクトのFileManagerインスタンスはinfo_guilds.csvの
#  local保存のみで、git storageは使用していない。file_manager.py自体は
#  汎用クラスとして残しているため、設定値のみここに残しておく)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = "apple-mado"
GITHUB_REPO = "GiftBot2"
GITHUB_BRANCH = "GiftBot-backup-data"

# ==========================
# Guild Info (chat_manager / setup,reject)
# ==========================
# ギルド・チャンネル登録情報はローカルCSVに保存する。
# 保存先フォルダは将来変更される可能性があるため変数化しておく。

CHAT_DATA_DIR = "data"
GUILD_INFO_FILE = "info_guilds.csv"
GUILD_INFO_FILE = (f"{CHAT_DATA_DIR}/"f"{GUILD_INFO_FILE}")


# ==========================
# Time Settings
# ==========================

UTC_OFFSET = +9


# ==========================
# Runtime Settings
# ==========================

EXCHANGE_PROGRESS_INTERVAL = 0

# giftcode実行中、players_dataコメント・LastResultダッシュボードの
# 両方を何人ごとに再EDITするかの頻度(10なら10人ごと)。
REPORTING_NUMBER = 10

# ==========================
# Guild Registration Limits
# ==========================

# 同時に登録できるギルドの最大数(fatal<=0で無効化されたギルドは含めない)
GUILD_MAX_COUNT = 10

# historyコメントが誤って削除された際に自動再作成できる残り回数の初期値。
# 0になったギルドは/help以外の全コマンドが実行不可になる(登録数にも含めない)。
FATAL_MAX = 3

# ライセンス(license=False)のギルドが同一ギフトコードで成功できる上限人数。
FREE_LICENSE_SUCCESS_LIMIT = 50

# 1ギルドあたりのプレイヤー登録上限人数。
# add_player、および/load_csv代わりのインポートボタンの双方で適用される。
PLAYER_MAX_COUNT = 500



# ==========================
# ERROR TRANSLATE
# ==========================

ERRORS = {
    "CDK NOT FOUND.":(
        "ABORT","無効なコード"),
    "RECEIVED.": (
        "CONTINUE","交換済み"),
    "USER INFO ERROR.": (
        "CONTINUE","存在しないプレイヤー"),
    "TIME ERROR.": (
        "ABORT","期限切れ"),
    "TIMEOUT.": (
        "RETRY","サーバータイムアウト"),
    "TIMEOUT RETRY.":(
        "RETRY","リトライ"),
    "SAME TYPE EXCHANGE.":(
        "CONTINUE","同じタイプのギフコ"),
    "RECHRGE_MONEY ERROR.":(
        "CONTINUE","バトラー資格がありません"),
    
}

# ==========================
# Gift Exchange Result Labels
# players.csv の "exchange_status" フィールドに保存される値。
# ERRORS の値(text部分)と一致させること。
# LastResultダッシュボードの5分類に対応する:
#   ✅成功 / ☑️交換済み / 👤存在しないプレイヤー / ❌それ以外のエラー / --未交換
# ==========================

RESULT_SUCCESS = "成功"
RESULT_ALREADY_EXCHANGED = "交換済み"
RESULT_INVALID_PLAYER = "存在しないプレイヤー"
RESULT_OTHER_ERROR = "エラー"
RESULT_NOT_EXCHANGED = "未交換"



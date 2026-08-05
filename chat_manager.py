# chat_manager.py


# ==========================
# ギルド単位のチャンネル登録・コメント管理クラス
# ==========================
#
# 複数ギルドでの同時運用を想定し、各ギルドにつき「利用チャンネル」を
# 1つだけ登録できるようにする。登録チャンネル内の3つのコメント
# (メッセージ)がデータの実体となる:
#
#   players_data  : 登録プレイヤー一覧。
#                   正データは添付ファイル(players_data.csv)として保持し、
#                   人数に上限を設けない。メッセージ本文には50人区切りの
#                   プレビューを表示し、Select(ページ切り替え)で
#                   本文の表示だけを切り替える。常に1メッセージで完結する。
#   history_data  : ギフトコード交換履歴(同一コードの重複を統合)
#   last_result   : 直近のgiftcode実行結果ダッシュボード
#
# ギルドの登録情報(guild_id/cha_id/各コメントのメッセージID/
# ライセンス/fatal/active等)はinfo_guilds.csv(ローカル)にまとめて保存する。

import csv
import io
import re
import discord
import asyncio
from datetime import datetime, timezone, timedelta

from file_manager import FileManager
from config import (
    GUILD_INFO_FILE,
    UTC_OFFSET,
    GUILD_MAX_COUNT,
    FATAL_MAX,
    FREE_LICENSE_SUCCESS_LIMIT,
    PLAYER_MAX_COUNT,
    RESULT_SUCCESS,
    RESULT_ALREADY_EXCHANGED,
    RESULT_INVALID_PLAYER,
    RESULT_OTHER_ERROR,
    RESULT_NOT_EXCHANGED,
)

JST = timezone(timedelta(hours=UTC_OFFSET))

# ==========================
# データフィールド定義
# ==========================
# file_managerと同様、フィールド(スキーマ)定義はこのモジュールが内部で保持する。
# commands.py / views_discord.py などはここからimportして利用する。

# players_data
# "exchange_status": 直近のgiftcode実行時の分類。以下の5種類のいずれか。
#   成功 / 交換済み / 存在しないプレイヤー / エラー(それ以外) / 未交換
# (旧"gift_code"列は廃止。どのコードで交換したかはhistory_data側で管理する)
PLAYER_FIELDS = {
    "id": int,
    "name": str,
    "player_id": int,
    "state": int,
    "exchange_status": str
}

# history_data
# "remain": ライセンス無し(license=False)ギルドが、同一コードで
#           あと何回成功できるか(初期値はFREE_LICENSE_SUCCESS_LIMIT)。
#           同一コードが再実行された場合は既存の記録を削除し、
#           remainを新規成功者数だけ減らして1件に統合する。
HISTORY_FIELDS = {
    "date": str,
    "gift_code": str,
    "remain": int
}

# info_guilds.csv (ギルド・チャンネル登録情報 + ライセンス/fatal/active管理)
# players_msg_idはplayers_dataメッセージ(1件のみ)のID。
# "active": 現在このギルドが有効に登録されているか(bool)。
#           /rejectでFalseになり、/setupで(fatal>=1なら)Trueに戻る。
# "fatal": history復旧・rejectで消費される残数の通算カウンタ。
#          0になると/setupでの再登録自体ができなくなる。
CHAT_FIELDS = {
    "id": int,
    "guild_name": str,
    "guild_id": int,
    "cha_name": str,
    "cha_id": int,
    "last_result_msg_id": int,
    "players_msg_id": int,
    "history_msg_id": int,
    "reg_date": str,
    "license": str,
    "paid_date": str,
    "fatal": int,
    "active": str,
}

# 登録情報を保存するファイル（全ギルド共通・1ファイル、ローカルCSV）
_settings_file = FileManager(
    filepath=GUILD_INFO_FILE,
    fields=CHAT_FIELDS,
    storage="local"
)

# ==========================
# コメント(登録チャンネル内のメッセージ)識別用マーカー
# ==========================
PLAYERS_COMMENT_MARKER = "📋GiftBot:players_data"
HISTORY_COMMENT_MARKER = "📜GiftBot:history_data"
LAST_RESULT_COMMENT_MARKER = "📊GiftBot:last_result"

# コードブロック(```csv ... ```)からCSV本文を取り出す正規表現
_CODEBLOCK_PATTERN = re.compile(r"```(?:csv)?\n(.*?)```", re.S)

# players_dataの1ページあたりの表示人数
PLAYERS_PAGE_SIZE = 50

# ログの詳細に表示する最大人数(処理の先頭から)
LOG_DETAIL_MAX = 50


def _safe_convert(converter, value):
    """
    file_manager.FileManager._safe_convertと同等の型変換ヘルパー(モジュール関数版)。
    """
    try:
        if value in ("", None):
            return None
        return converter(value)
    except (ValueError, TypeError):
        return None


def _to_bool(value):
    """CSVの"True"/"False"文字列(または既にbool)をboolに変換する"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


# ==========================
# ギルド・チャンネル判定の共通ヘルパー
# ==========================
# /help以外の全コマンド(および永続Viewのボタン)から呼ばれる想定。
# commands.py / views_discord.py 双方から循環importなしで使えるよう、
# ここ(chat_manager.py)に置く。
# ==========================

# ==========================
# ギルド単位の排他制御(asyncio.Lock)
# ==========================
# giftcode実行中や、add_player/delete_player/インポートによる
# players_dataの読み書きが同一ギルド内で重ならないようにする。
#
# 注意: これはOSレベルの「スレッド」ではなく、discord.py自体が単一の
# asyncioイベントループ上で動く非同期処理であるため、
# asyncio.Lockを使うのが正しい(かつ唯一自然な)排他手段。
#
# キーは必ず「コマンドを呼び出したそのインタラクションのguild_id」を
# 文字列化したものにする。channel_idや他の値をキーにしない・
# 他ギルドのmanager/channelを一切参照しないことで、
# 別ギルドの処理を誤ってブロックしたり、別ギルドのデータを
# 参照してしまったりしないようにしている。
# ==========================

_guild_locks: dict[str, asyncio.Lock] = {}


def get_guild_lock(guild_id) -> asyncio.Lock:
    key = str(guild_id)

    if key not in _guild_locks:
        _guild_locks[key] = asyncio.Lock()

    return _guild_locks[key]


# ==========================
# 全ギルドの登録情報一覧(管理者用コマンドから使用)
# ==========================
# 特定のギルドに紐づかない、chat_settings.csv全件の参照。
# 通常のChatManagerインスタンス(1ギルド分のみ扱う)とは別に、
# モジュール関数として独立させている。
# ==========================

def list_all_guilds():
    result = _settings_file.load()

    if not result["status"]:
        return []

    rows = result["data"] or []

    for row in rows:
        row["license"] = _to_bool(row.get("license"))
        row["active"] = _to_bool(row.get("active"))

    return rows


def find_guild_row_by_id(row_id):
    """
    info_guilds.csvの内部連番id(guild_idではない)で行を検索する。
    /guild_deleteの確認表示・実削除の両方で使う。
    """
    for row in list_all_guilds():
        if str(row.get("id")) == str(row_id):
            return row

    return None


def delete_guild_by_id(row_id):
    """
    info_guilds.csvの内部連番id(guild_idではない)を指定して、
    該当行を完全に削除する(/rejectのような無効化ではなく、
    行自体・fatal含めて跡形もなく削除する。取り消し不可)。
    """
    result = _settings_file.load()

    if not result["status"]:
        return {
            "status": False,
            "action": "delete_guild_by_id",
            "message": "info_guilds.csvの読み込みに失敗しました",
            "data": None
        }

    rows = result["data"] or []

    target = None
    new_rows = []

    for row in rows:
        if str(row.get("id")) == str(row_id):
            target = row
        else:
            new_rows.append(row)

    if target is None:
        return {
            "status": False,
            "action": "delete_guild_by_id",
            "message": f"id:{row_id} は見つかりませんでした",
            "data": None
        }

    save_result = _settings_file.save(new_rows)

    if not save_result["status"]:
        return save_result

    return {
        "status": True,
        "action": "delete_guild_by_id",
        "message": f"id:{row_id} ({target.get('guild_name')}) を削除しました",
        "data": target
    }


async def require_guild(interaction):

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True
        )
        return None

    return interaction.guild


async def require_registered_channel(interaction, guild):

    manager = ChatManager(
        guild_id=guild.id,
        channel_id=interaction.channel_id
    )

    result = manager.exist()

    if not result["status"]:

        remaining = GUILD_MAX_COUNT - manager.get_active_guild_count()

        if remaining > 0:
            slots_text = f"あと{remaining}ギルド登録可能です。"
        else:
            slots_text = (
                "現在ギルド登録数が上限に達しているため、"
                "これ以上登録できません。"
            )

        await interaction.response.send_message(
            "❌このサーバーではまだ /setup が実行されていません。\n"
            "管理者に利用チャンネルで /setup の実行を依頼してください。\n"
            f"{slots_text}",
            ephemeral=True
        )
        return None

    if manager.is_disabled():

        fatal = result["data"].get("fatal")
        fatal = 0 if fatal is None else int(fatal)

        if fatal >= 1:
            recovery_text = (
                f"(残りfatal:{fatal}。管理者による/setupの再実行で"
                "再登録できます)"
            )
        else:
            recovery_text = (
                "(fatalが0のため、/setupでの再登録もできません。"
                "運営にお問い合わせください)"
            )

        await interaction.response.send_message(
            "❌このサーバーの登録は無効化されています。\n"
            f"{recovery_text}",
            ephemeral=True
        )
        return None

    registered_channel_id = str(result["data"]["cha_id"])

    if registered_channel_id != str(interaction.channel_id):
        await interaction.response.send_message(
            "❌このコマンドは登録されたチャンネルでのみ実行できます。\n"
            f"登録チャンネル: <#{registered_channel_id}>",
            ephemeral=True
        )
        return None

    return manager


class ChatManager:

    def __init__(
        self,
        *,
        guild_id,
        channel_id=None
    ):
        self.guild_id = str(guild_id)
        self.channel_id = (
            str(channel_id)
            if channel_id is not None
            else None
        )

        # exist()で取得した登録情報のキャッシュ。
        # スラッシュコマンド/ボタン側(require_registered_channel)で
        # 既にexist()呼び出し済みであれば、以後クラス内の各関数は
        # これを使い回し、chat_settings.csvを読み直さない。
        self._entry = None

    # ==========================
    # 共通返却
    # ==========================
    def _result(
        self,
        status,
        action,
        message,
        data=None
    ):
        return {
            "status": status,
            "action": action,
            "message": message,
            "data": data
        }

    # ==========================
    # 登録情報全件(行のlist)の取得(内部用)
    # ==========================
    def _load_all(self):

        result = _settings_file.load()

        if not result["status"]:
            return []

        return result["data"] or []

    # ==========================
    # 対象ギルドの行を検索(内部用)
    # ==========================
    def _find_row(self, rows):

        for row in rows:
            if str(row.get("guild_id")) == self.guild_id:
                return row

        return None

    # ==================================================
    # EXIST
    # ==========================
    def exist(self):

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            self._entry = None
            return self._result(
                False,
                "exist",
                f"未登録のギルドです: {self.guild_id}"
            )

        entry["license"] = _to_bool(entry.get("license"))
        entry["active"] = _to_bool(entry.get("active"))
        self._entry = entry

        return self._result(
            True,
            "exist",
            f"登録済みのギルドです: {self.guild_id}",
            entry
        )

    # ==================================================
    # ギルド登録数(アクティブ = active かつ fatal>0)のカウント
    # ==========================
    # /rejectされたギルド(active=False)や、fatalを使い切ったギルド
    # (fatal<=0、履歴コメント誤削除の復旧で消費した場合を含む)は、
    # いずれも登録数にカウントしない(枠を圧迫しない)。
    # ==========================

    def get_active_guild_count(self):

        rows = self._load_all()
        count = 0

        for row in rows:
            active = _to_bool(row.get("active"))
            fatal = row.get("fatal")
            fatal_ok = fatal is None or int(fatal) > 0

            if active and fatal_ok:
                count += 1

        return count

    # ==================================================
    # CREATE
    # ==========================
    # 新規登録。
    # - 同一ギルドに既に登録がある場合は失敗させる。
    # - アクティブなギルド登録数がGUILD_MAX_COUNTに達している場合も失敗させる。
    # ==========================

    def create(
        self,
        guild_name,
        channel_name
    ):

        active_count = self.get_active_guild_count()

        if active_count >= GUILD_MAX_COUNT:
            return self._result(
                False,
                "create",
                f"ギルド登録数が上限({GUILD_MAX_COUNT})に達しているため、"
                "これ以上登録できません"
            )

        rows = self._load_all()

        if self._find_row(rows):
            return self._result(
                False,
                "create",
                "このギルドは既にチャンネルが登録されています"
            )

        new_id = 1
        if rows:
            new_id = max(int(row["id"]) for row in rows) + 1

        entry = {
            "id": new_id,
            "guild_name": guild_name,
            "guild_id": self.guild_id,
            "cha_name": channel_name,
            "cha_id": self.channel_id,
            "last_result_msg_id": None,
            "players_msg_id": None,
            "history_msg_id": None,
            "reg_date": datetime.now(JST).strftime(
                "%Y/%m/%d %H:%M:%S"
            ),
            "license": False,
            "paid_date": "",
            "fatal": FATAL_MAX,
            "active": True
        }

        rows.append(entry)

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "create",
            f"登録しました: {self.guild_id}",
            entry
        )

    # ==================================================
    # SAVE
    # ==========================
    # 既存登録の上書き更新(ID・登録日は維持する)。
    # ==========================

    def save(
        self,
        guild_name,
        channel_name
    ):

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            return self._result(
                False,
                "save",
                f"登録されていないため更新できません: {self.guild_id}"
            )

        entry["guild_name"] = guild_name
        entry["cha_name"] = channel_name
        entry["cha_id"] = self.channel_id

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "save",
            f"更新しました: {self.guild_id}",
            entry
        )

    # ==================================================
    # UPDATE MESSAGE IDS
    # ==========================
    # last_result / players / history 各コメントの
    # メッセージIDを更新する。指定しなかったものは変更しない。
    # ==========================

    def update_message_ids(
        self,
        *,
        last_result_message_id=None,
        players_message_id=None,
        history_message_id=None
    ):

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            return self._result(
                False,
                "update_message_ids",
                f"登録されていません: {self.guild_id}"
            )

        if last_result_message_id is not None:
            entry["last_result_msg_id"] = last_result_message_id

        if players_message_id is not None:
            entry["players_msg_id"] = players_message_id

        if history_message_id is not None:
            entry["history_msg_id"] = history_message_id

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "update_message_ids",
            f"メッセージIDを更新しました: {self.guild_id}",
            entry
        )

    # ==================================================
    # DELETE
    # ==========================

    def delete(self):

        rows = self._load_all()

        new_rows = [
            row
            for row in rows
            if str(row.get("guild_id")) != self.guild_id
        ]

        if len(new_rows) == len(rows):
            return self._result(
                True,
                "delete",
                f"元々登録されていません: {self.guild_id}"
            )

        save_result = _settings_file.save(new_rows)

        if not save_result["status"]:
            return save_result

        self._entry = None

        return self._result(
            True,
            "delete",
            f"削除しました: {self.guild_id}"
        )

    # ==================================================
    # DELETE COMMENTS
    # ==========================
    # /reject実行時に、players_data・history_data・last_result の
    # 3つのメッセージ自体をチャンネルから削除する。
    # 呼び出し側(commands.py)は、delete()で登録情報を消す前に
    # このメソッドを呼ぶ想定(self._entryがまだ有効なうちに使う)。
    # 個々のメッセージが既に手動削除されている等で見つからない場合は
    # エラーにせず無視し、削除できたものだけ結果に含める。
    # ==========================

    async def delete_comments(self, bot):

        if self._entry is None:
            exist_result = self.exist()
            if not exist_result["status"]:
                return exist_result

        channel_id = int(self._entry["cha_id"])
        channel = bot.get_channel(channel_id)

        if channel is None:
            return self._result(
                False,
                "delete_comments",
                f"チャンネルが見つかりません: {channel_id}"
            )

        targets = {
            "players_data": self._entry.get("players_msg_id"),
            "history_data": self._entry.get("history_msg_id"),
            "last_result": self._entry.get("last_result_msg_id"),
        }

        deleted = []
        failed = []

        for label, message_id in targets.items():

            if not message_id:
                continue

            try:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
                deleted.append(label)
            except Exception:
                failed.append(label)

        return self._result(
            True,
            "delete_comments",
            f"削除済み:{deleted} / 見つからず:{failed}",
            {"deleted": deleted, "failed": failed}
        )

    # ==================================================
    # ライセンス / fatal 関連ヘルパー
    # ==========================

    def has_license(self):

        if self._entry is None:
            self.exist()

        if self._entry is None:
            return False

        return _to_bool(self._entry.get("license"))

    def is_disabled(self):
        """
        以下いずれかに該当する場合、無効化されたギルドとして扱う:
        - active が False (/rejectされた、またはfatal切れで再登録不可)
        - fatal が0以下(history復旧回数を使い切った。/rejectを経由せず
          誤削除の繰り返しだけで到達した場合も含む)
        """

        if self._entry is None:
            result = self.exist()
            if not result["status"]:
                return False

        active = _to_bool(self._entry.get("active"))
        fatal = self._entry.get("fatal")
        fatal_exhausted = fatal is not None and int(fatal) <= 0

        return (not active) or fatal_exhausted

    def consume_fatal(self):
        """fatalを1消費する(0未満にはしない)。historyコメント復旧時に使用。"""

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            return self._result(
                False,
                "consume_fatal",
                f"登録されていません: {self.guild_id}"
            )

        current = entry.get("fatal")
        current = FATAL_MAX if current is None else int(current)
        new_value = max(0, current - 1)
        entry["fatal"] = new_value

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "consume_fatal",
            f"fatalを{new_value}に更新しました",
            entry
        )

    # ==================================================
    # DEACTIVATE / REACTIVATE
    # ==========================
    # /reject は行自体を削除せず、fatalを1減らして active=False にする
    # (行・fatal自体は削除しない。fatalはreject・history復旧の消費を
    #  通算でカウントし続ける)。
    # /setup が同じギルドに対して再実行された場合、
    # active=False の行が見つかれば、fatalが1以上ある場合のみ
    # reactivate()でその場を有効化して使い回す
    # (fatalが0ならメッセージを返すのみで再登録は行わない)。
    # ==========================

    def deactivate(self):
        """
        /reject用: 行・fatalは削除せず、fatalを1減らして
        (0未満にはしない)active=Falseにする。
        """

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            return self._result(
                True,
                "deactivate",
                f"元々登録されていません: {self.guild_id}"
            )

        current_fatal = entry.get("fatal")
        current_fatal = FATAL_MAX if current_fatal is None else int(current_fatal)
        new_fatal = max(0, current_fatal - 1)

        entry["fatal"] = new_fatal
        entry["active"] = False
        entry["last_result_msg_id"] = None
        entry["players_msg_id"] = None
        entry["history_msg_id"] = None

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "deactivate",
            f"無効化しました(残りfatal:{new_fatal}): {self.guild_id}",
            entry
        )

    def reactivate(self, guild_name, channel_name):
        """
        /setup用: active=Falseだった既存登録を、
        削除・新規作成し直さずにその場で再度有効化する。
        fatalが1未満(=0)の場合は再登録自体を拒否する。
        fatal自体はリセットしない(通算カウントを維持する)。
        ギルド登録数の上限チェックはcreate()と同様にここでも行う。
        """

        rows = self._load_all()
        entry = self._find_row(rows)

        if not entry:
            return self._result(
                False,
                "reactivate",
                f"登録されていません: {self.guild_id}"
            )

        current_fatal = entry.get("fatal")
        current_fatal = FATAL_MAX if current_fatal is None else int(current_fatal)

        if current_fatal < 1:
            return self._result(
                False,
                "reactivate",
                "このギルドはfatalが0のため、再登録できません。"
                "運営にお問い合わせください。"
            )

        active_count = self.get_active_guild_count()

        if active_count >= GUILD_MAX_COUNT:
            return self._result(
                False,
                "reactivate",
                f"ギルド登録数が上限({GUILD_MAX_COUNT})に達しているため、"
                "これ以上登録できません"
            )

        entry["guild_name"] = guild_name
        entry["cha_name"] = channel_name
        entry["cha_id"] = self.channel_id
        entry["active"] = True
        entry["last_result_msg_id"] = None
        entry["players_msg_id"] = None
        entry["history_msg_id"] = None
        entry["reg_date"] = datetime.now(JST).strftime(
            "%Y/%m/%d %H:%M:%S"
        )
        # fatalはリセットしない(通算カウントのまま維持する)

        save_result = _settings_file.save(rows)

        if not save_result["status"]:
            return save_result

        self._entry = entry

        return self._result(
            True,
            "reactivate",
            f"再登録しました: {self.guild_id}",
            entry
        )

    # ==================================================
    # ヘルパー
    # ==========================

    def is_registered_channel(self):

        if self._entry is None:
            result = self.exist()
            if not result["status"]:
                return False

        return (
            str(self._entry["cha_id"])
            == self.channel_id
        )

    # ==================================================
    # チャンネル/メッセージ取得ヘルパー(内部用)
    # ==========================
    # exist()はスラッシュコマンド/ボタン側(require_registered_channel)で
    # 一度呼ばれ、結果はself._entryにキャッシュされる。
    # ここでは既にキャッシュがあればそれを使い、無い場合のみ
    # (念のためのフォールバックとして)exist()を呼ぶ。
    # ==========================

    async def _get_channel(self, bot):

        if self._entry is None:
            exist_result = self.exist()
            if not exist_result["status"]:
                return None, exist_result

        channel_id = int(self._entry["cha_id"])
        channel = bot.get_channel(channel_id)

        if channel is None:
            return None, self._result(
                False,
                "channel",
                f"チャンネルが見つかりません: {channel_id}"
            )

        return channel, self._result(
            True,
            "exist",
            f"登録済みのギルドです: {self.guild_id}",
            self._entry
        )

    async def _fetch_or_find_message(self, channel, message_id, marker):
        """
        message_idで直接取得を試み、失敗した場合は
        マーカー文字列を含む直近のメッセージを履歴から検索する(フォールバック)。
        どちらも失敗した場合はNoneを返す。
        """

        if message_id:
            try:
                return await channel.fetch_message(int(message_id))
            except Exception:
                pass

        async for message in channel.history(limit=200):
            if marker in message.content:
                return message

        return None

    # ==================================================
    # コメント本文の組み立て/解析(内部用)
    # ==========================

    def _build_comment_content(self, marker, fields, rows, extra_header=""):

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fieldnames=fields.keys(),
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)

        header = f"{marker}\n{extra_header}" if extra_header else f"{marker}\n"

        return (
            f"{header}"
            f"```csv\n{output.getvalue()}```"
        )

    def _parse_comment_content(self, fields, content):

        match = _CODEBLOCK_PATTERN.search(content)

        if not match:
            return []

        reader = csv.DictReader(io.StringIO(match.group(1)))

        return [
            {
                field: _safe_convert(converter, row.get(field))
                for field, converter in fields.items()
            }
            for row in reader
        ]

    # ==========================
    # 添付ファイル用: マーカー/コードブロックを伴わない生CSVの変換
    # (players_data.csv の正データ本体はこちらを使う)
    # ==========================

    def _encode_raw_csv(self, fields, rows):

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fieldnames=fields.keys(),
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)

        return output.getvalue()

    def _parse_raw_csv(self, fields, content):

        reader = csv.DictReader(io.StringIO(content))

        return [
            {
                field: _safe_convert(converter, row.get(field))
                for field, converter in fields.items()
            }
            for row in reader
        ]

    # exchange_statusの表示用マーク(LastResultダッシュボードと同じ絵文字)。
    # あくまで本文プレビュー表示のみに使い、添付ファイル(players_data.csv)
    # や内部ロジックで扱う実データ(RESULT_*の文字列)は一切変更しない。
    _EXCHANGE_STATUS_MARKS = {
        RESULT_SUCCESS: "✅",
        RESULT_ALREADY_EXCHANGED: "☑️",
        RESULT_INVALID_PLAYER: "👤",
        RESULT_OTHER_ERROR: "❌",
        RESULT_NOT_EXCHANGED: "--",
    }

    def build_players_preview_content(
        self, page_players, page_index, total_pages, total_count
    ):
        """
        players_dataメッセージ本文(プレビュー)を組み立てる。
        正データは添付ファイル側にあるため、ここは表示用のみ。
        exchange_statusは表示上マーク(✅☑️👤❌--)に変換するが、
        page_players自体(=呼び出し元が持つ実データ)は変更しない
        (表示専用のコピーを作ってから変換する)。
        views_discord.pyのPageSelectからも呼ばれる(公開メソッド)。
        """

        preview_rows = [
            {
                **player,
                "exchange_status": self._EXCHANGE_STATUS_MARKS.get(
                    player.get("exchange_status"),
                    player.get("exchange_status")
                )
            }
            for player in page_players
        ]

        return self._build_comment_content(
            PLAYERS_COMMENT_MARKER,
            PLAYER_FIELDS,
            preview_rows,
            extra_header=(
                f"ページ {page_index + 1}/{total_pages}"
                f"(全{total_count}人。全員分はこのメッセージの"
                "添付ファイル players_data.csv を参照)\n"
            )
        )

    # ==================================================
    # POST INITIAL COMMENTS
    # ==========================
    # /setup成功直後に呼び出す。
    # players_data -> history_data -> last_result の順で投稿する。
    # ==========================

    async def post_initial_comments(
        self,
        channel,
        players_view=None,
        last_result_view=None
    ):

        try:
            players_message = await channel.send(
                self.build_players_preview_content([], 0, 1, 0),
                file=discord.File(
                    io.BytesIO(
                        self._encode_raw_csv(PLAYER_FIELDS, []).encode("utf-8-sig")
                    ),
                    filename="players_data.csv"
                ),
                view=players_view
            )

            history_message = await channel.send(
                self._build_comment_content(
                    HISTORY_COMMENT_MARKER,
                    HISTORY_FIELDS,
                    []
                )
            )

            last_result_message = await channel.send(
                self._build_last_result_content({}),
                view=last_result_view
            )

            update_result = self.update_message_ids(
                last_result_message_id=last_result_message.id,
                players_message_id=players_message.id,
                history_message_id=history_message.id
            )

            if not update_result["status"]:
                return update_result

            return self._result(
                True,
                "post_initial_comments",
                "players_data/history_data/last_resultを投稿しました",
                {
                    "players_msg_id": players_message.id,
                    "history_msg_id": history_message.id,
                    "last_result_msg_id": last_result_message.id
                }
            )

        except Exception as e:
            return self._result(
                False,
                "post_initial_comments",
                str(e)
            )

    # ==================================================
    # PLAYERS_DATA 読み込み/書き込み
    # ==========================
    # 正データは添付ファイル(players_data.csv)に保持し、常に1メッセージ
    # で完結させる(人数による投稿数の増減はしない)。
    # メッセージ本文はページ単位のプレビュー表示のみ。
    # ==========================

    async def read_players_comment(self, bot):

        channel, exist_result = await self._get_channel(bot)

        if channel is None:
            return exist_result

        message_id = self._entry.get("players_msg_id") if self._entry else None

        message = await self._fetch_or_find_message(
            channel, message_id, PLAYERS_COMMENT_MARKER
        )

        if message is None:
            return self._result(
                False,
                "read_players_comment",
                "players_dataコメントが見つかりません"
            )

        if str(message.id) != str(message_id):
            self.update_message_ids(players_message_id=message.id)

        csv_attachment = None
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".csv"):
                csv_attachment = attachment
                break

        if csv_attachment is None:
            # 添付が無い(過去バージョンのメッセージ等)場合のフォールバック:
            # 本文のプレビュー分だけでも復元する
            players = self._parse_comment_content(PLAYER_FIELDS, message.content)
            return self._result(
                True,
                "read_players_comment",
                f"{len(players)}件を読み込みました(添付ファイルなし・プレビューのみ)",
                players
            )

        raw_bytes = await csv_attachment.read()
        players = self._parse_raw_csv(
            PLAYER_FIELDS,
            raw_bytes.decode("utf-8-sig")
        )

        return self._result(
            True,
            "read_players_comment",
            f"{len(players)}件を読み込みました",
            players
        )

    async def write_players_comment(
        self, bot, players, page_index=0, view_factory=None
    ):
        """
        全員分を添付ファイル(players_data.csv)として保存し、
        メッセージ本文には指定ページ(デフォルト先頭ページ)のプレビュー
        のみ表示する。常に1メッセージで完結させる(新規ページの投稿はしない)。

        view_factory: callable(page_index, total_pages) -> discord.ui.View
        """

        channel, exist_result = await self._get_channel(bot)

        if channel is None:
            return exist_result

        total_pages = max(
            1, (len(players) + PLAYERS_PAGE_SIZE - 1) // PLAYERS_PAGE_SIZE
        )
        page_index = max(0, min(page_index, total_pages - 1))
        page_players = players[
            page_index * PLAYERS_PAGE_SIZE: (page_index + 1) * PLAYERS_PAGE_SIZE
        ]

        content = self.build_players_preview_content(
            page_players, page_index, total_pages, len(players)
        )

        if len(content) > 1900:
            return self._result(
                False,
                "write_players_comment",
                f"プレビューの文字数上限(1900文字)を超えました({len(content)}文字)。"
            )

        file = discord.File(
            io.BytesIO(
                self._encode_raw_csv(PLAYER_FIELDS, players).encode("utf-8-sig")
            ),
            filename="players_data.csv"
        )

        view = view_factory(page_index, total_pages) if view_factory else None

        message_id = self._entry.get("players_msg_id") if self._entry else None

        message = await self._fetch_or_find_message(
            channel, message_id, PLAYERS_COMMENT_MARKER
        )

        try:
            if message is None:
                message = await channel.send(content, file=file, view=view)
            else:
                await message.edit(content=content, attachments=[file], view=view)

            if str(message.id) != str(message_id):
                self.update_message_ids(players_message_id=message.id)

            return self._result(
                True,
                "write_players_comment",
                f"{len(players)}件を反映しました({total_pages}ページ中{page_index + 1}ページ目を表示)",
                players
            )

        except Exception as e:
            return self._result(
                False,
                "write_players_comment",
                str(e)
            )

    # ==================================================
    # HISTORY_DATA 読み込み/書き込み
    # ==========================

    async def read_history_comment(self, bot):

        channel, exist_result = await self._get_channel(bot)

        if channel is None:
            return exist_result

        message_id = self._entry.get("history_msg_id")

        message = await self._fetch_or_find_message(
            channel,
            message_id,
            HISTORY_COMMENT_MARKER
        )

        if message is None:
            return self._result(
                False,
                "read_history_comment",
                "history_dataコメントが見つかりません"
            )

        if str(message.id) != str(message_id):
            self.update_message_ids(history_message_id=message.id)

        history = self._parse_comment_content(
            HISTORY_FIELDS,
            message.content
        )

        return self._result(
            True,
            "read_history_comment",
            f"{len(history)}件を読み込みました",
            history
        )

    async def write_history_comment(self, bot, history):

        channel, exist_result = await self._get_channel(bot)

        if channel is None:
            return exist_result

        content = self._build_comment_content(
            HISTORY_COMMENT_MARKER,
            HISTORY_FIELDS,
            history
        )

        if len(content) > 1900:
            return self._result(
                False,
                "write_history_comment",
                (
                    "履歴が多く、コメントの文字数上限"
                    f"(1900文字)を超えました({len(content)}文字)。"
                    "分割保存には未対応です。"
                )
            )

        message_id = self._entry.get("history_msg_id") if self._entry else None

        message = await self._fetch_or_find_message(
            channel,
            message_id,
            HISTORY_COMMENT_MARKER
        )

        try:
            if message is None:
                message = await channel.send(content)
            else:
                await message.edit(content=content)

            if str(message.id) != str(message_id):
                self.update_message_ids(history_message_id=message.id)

            return self._result(
                True,
                "write_history_comment",
                f"{len(history)}件を反映しました",
                history
            )

        except Exception as e:
            return self._result(
                False,
                "write_history_comment",
                str(e)
            )

    # ==================================================
    # fatal復旧付き: history_dataコメントの存在確認
    # ==========================
    # gift_code実行前に呼び出す。history_dataコメントが見つからない場合、
    # fatalが残っていれば自動的に空のhistory_dataを再作成してfatalを1消費する。
    # ==========================

    async def ensure_history_comment(self, bot):

        read_result = await self.read_history_comment(bot)

        if read_result["status"]:
            return self._result(True, "ensure_history_comment", "OK", read_result["data"])

        # 見つからなかった場合、fatalが残っていれば自動再作成する
        current_fatal = self._entry.get("fatal") if self._entry else None
        current_fatal = FATAL_MAX if current_fatal is None else int(current_fatal)

        if current_fatal <= 0:
            return self._result(
                False,
                "ensure_history_comment",
                "history_dataコメントが見つからず、復旧回数も残っていません"
            )

        write_result = await self.write_history_comment(bot, [])

        if not write_result["status"]:
            return write_result

        consume_result = self.consume_fatal()

        return self._result(
            True,
            "ensure_history_comment",
            f"history_dataコメントを再作成しました(残り復旧回数:{consume_result['data']['fatal'] if consume_result['status'] else '?'})",
            []
        )

    # ==================================================
    # ライセンス/history連動: 同一コードの実行可否判定
    # ==========================

    async def check_code_limit(self, bot, code):
        """
        戻り値: (allowed: bool, remain: int, message: str)
        licenseがTrueなら常にallowed=True。
        Falseの場合、historyに同一codeの記録がありremain<=0ならallowed=False。
        """

        if self.has_license():
            return True, None, "ライセンス有効"

        read_result = await self.read_history_comment(bot)
        history = read_result["data"] if read_result["status"] else []

        for row in history:
            if row.get("gift_code") == code:
                remain = row.get("remain")
                remain = FREE_LICENSE_SUCCESS_LIMIT if remain is None else remain

                if remain <= 0:
                    return (
                        False,
                        remain,
                        (
                            "❌このコードは無料プランの上限"
                            f"({FREE_LICENSE_SUCCESS_LIMIT}回成功)に"
                            "達しています。"
                        )
                    )

                return True, remain, "OK"

        return True, FREE_LICENSE_SUCCESS_LIMIT, "OK"

    async def record_history_success(self, bot, code, success_count):
        """
        成功者が1人以上のときに呼び出す。
        同一コードの既存記録があれば削除し、remainを成功者数だけ減らして
        1件に統合する(無ければFREE_LICENSE_SUCCESS_LIMITから減算)。
        """

        if success_count <= 0:
            return self._result(True, "record_history_success", "成功者0のため記録なし")

        read_result = await self.read_history_comment(bot)
        history = read_result["data"] if read_result["status"] else []

        remain = FREE_LICENSE_SUCCESS_LIMIT

        new_history = []
        for row in history:
            if row.get("gift_code") == code:
                existing_remain = row.get("remain")
                remain = (
                    FREE_LICENSE_SUCCESS_LIMIT
                    if existing_remain is None
                    else existing_remain
                )
                continue
            new_history.append(row)

        remain = max(0, remain - success_count)

        new_history.append({
            "date": datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
            "gift_code": code,
            "remain": remain
        })

        return await self.write_history_comment(bot, new_history)

    # ==================================================
    # LAST_RESULT ダッシュボード
    # ==========================

    def _build_last_result_content(self, dashboard):
        """
        dashboard: {
            "code": str, "date": str, "elapsed": str,
            "counts": {"成功":n, "交換済み":n, "存在しないプレイヤー":n,
                       "エラー":n, "未交換":n},
            "waiting": bool, "status": "処理中"/"待機中"/"完了"
        }
        """

        if not dashboard:
            return (
                f"{LAST_RESULT_COMMENT_MARKER}\n"
                "まだギフトコードは実行されていません。"
            )

        counts = dashboard.get("counts", {})

        lines = [
            LAST_RESULT_COMMENT_MARKER,
            f"最後に交換したギフトコード: `{dashboard.get('code', '')}`",
            f"交換日:{dashboard.get('date', '')}",
            f"経過時間:{dashboard.get('elapsed', '')}",
            f"✅:成功 {counts.get(RESULT_SUCCESS, 0)}人",
            f"☑️:交換済み {counts.get(RESULT_ALREADY_EXCHANGED, 0)}人",
            f"👤:存在しないプレイヤー {counts.get(RESULT_INVALID_PLAYER, 0)}人",
            f"❌:それ以外のエラー {counts.get(RESULT_OTHER_ERROR, 0)}人",
            f"--:未交換 {counts.get(RESULT_NOT_EXCHANGED, 0)}人",
        ]

        if dashboard.get("waiting"):
            lines.append("🔁:待機中(API制限)")

        lines.append(f"Bot状態: {dashboard.get('status', '完了')}")

        return "\n".join(lines)

    async def write_last_result(self, bot, dashboard, view=None):

        channel, exist_result = await self._get_channel(bot)

        if channel is None:
            return exist_result

        content = self._build_last_result_content(dashboard)
        message_id = self._entry.get("last_result_msg_id") if self._entry else None

        message = await self._fetch_or_find_message(
            channel,
            message_id,
            LAST_RESULT_COMMENT_MARKER
        )

        try:
            if message is None:
                message = await channel.send(content, view=view)
            else:
                await message.edit(content=content, view=view)

            if str(message.id) != str(message_id):
                self.update_message_ids(last_result_message_id=message.id)

            return self._result(
                True,
                "write_last_result",
                "last_resultを更新しました"
            )

        except Exception as e:
            return self._result(
                False,
                "write_last_result",
                str(e)
            )

    # ==================================================
    # IMPORT (チャンネル添付CSVの取り込み)
    # ==========================
    # 登録済みチャンネル内から最新のplayers.csv添付ファイルを
    # 検索して取り込み、PLAYER_FIELDS形式に正規化して返す。
    # Bot自身が投稿したメッセージ(バックアップ出力等)は対象外にする。
    # ==========================

    async def import_players(self, bot):

        channel, channel_result = await self._get_channel(bot)

        if channel is None:
            return channel_result

        try:
            latest_comment_preview = None

            async for message in channel.history(limit=200):

                author = getattr(message, "author", None)
                bot_user = getattr(bot, "user", None)

                if (
                    author is not None
                    and bot_user is not None
                    and getattr(author, "id", None) == getattr(bot_user, "id", None)
                ):
                    continue

                if latest_comment_preview is None:
                    preview_source = (message.content or "").strip()
                    if len(preview_source) > 25:
                        latest_comment_preview = preview_source[:25] + "..."
                    else:
                        latest_comment_preview = preview_source

                for attachment in message.attachments:

                    if not attachment.filename.lower().endswith(".csv"):
                        continue

                    raw_bytes = await attachment.read()
                    content = raw_bytes.decode("utf-8-sig")

                    return self._parse_players_csv(content)

            if latest_comment_preview:
                not_found_message = (
                    f"「{latest_comment_preview}」まで検索しましたが、"
                    "players.csvが投稿されているコメントがありませんでした"
                )
            else:
                not_found_message = (
                    "Bot以外が投稿したコメントが見つからず、"
                    "players.csvが投稿されているコメントがありませんでした"
                )

            return self._result(
                False,
                "import",
                not_found_message
            )

        except Exception as e:
            return self._result(
                False,
                "import",
                str(e)
            )

    # ==========================
    # CSVテキスト -> PLAYER_FIELDS正規化(内部用)
    # ==========================
    # - name / player_id / state を必須項目とし、
    #   欠けている・数値変換できない行はスキップする。
    # - id は 1 から振り直す。
    # - exchange_status 列が無い/空の場合は空欄で追加する
    #   (gift_code列は廃止したため取り込まない)。
    # ==========================

    def _parse_players_csv(self, content):

        reader = csv.DictReader(io.StringIO(content))

        players = []
        skipped = 0

        for row in reader:

            name = (row.get("name") or "").strip()
            raw_player_id = (row.get("player_id") or "").strip()
            raw_state = (row.get("state") or "").strip()

            if not name or not raw_player_id or not raw_state:
                skipped += 1
                continue

            try:
                player_id = int(raw_player_id)
                state = int(raw_state)
            except ValueError:
                skipped += 1
                continue

            exchange_status = (
                row.get("exchange_status") or ""
            ).strip()

            players.append({
                "name": name,
                "player_id": player_id,
                "state": state,
                "exchange_status": exchange_status
            })

        # 登録上限(PLAYER_MAX_COUNT)を超える場合、先頭からその人数だけを
        # 取り込み、それ以降は読み込まない(警告メッセージを添える)。
        total_valid = len(players)
        over_limit = total_valid > PLAYER_MAX_COUNT

        if over_limit:
            players = players[:PLAYER_MAX_COUNT]

        normalized = []
        for index, player in enumerate(players, start=1):
            player["id"] = index
            normalized.append({
                field: player[field]
                for field in PLAYER_FIELDS.keys()
            })

        if over_limit:
            message = (
                f"⚠️登録上限({PLAYER_MAX_COUNT}人)を超えていたため、"
                f"先頭{PLAYER_MAX_COUNT}人のみ取り込みました"
                f"(有効データ{total_valid}件のうち{total_valid - PLAYER_MAX_COUNT}件は"
                f"読み込んでいません。スキップ:{skipped}件)"
            )
        else:
            message = f"{len(normalized)}件を読み込みました(スキップ:{skipped}件)"

        return self._result(
            True,
            "import",
            message,
            normalized
        )

import re

import discord

from chat_manager import (
    require_guild,
    require_registered_channel,
    get_guild_lock,
    PLAYERS_PAGE_SIZE,
    RESULT_OTHER_ERROR,
    RESULT_INVALID_PLAYER,
    RESULT_NOT_EXCHANGED,
)


class ConfirmDeleteView(discord.ui.View):

    def __init__(
        self,
        players,
        manager,
        bot
    ):
        super().__init__(timeout=60)

        self.players = players
        self.manager = manager
        self.bot = bot


    @discord.ui.button(
        label="削除する",
        style=discord.ButtonStyle.danger
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        # self.manager.guild_id はこのViewを生成したコマンド呼び出し時の
        # ギルドIDのみを保持しており、他ギルドの値を参照することはない。
        lock = get_guild_lock(self.manager.guild_id)

        if lock.locked():
            await interaction.response.edit_message(
                content=(
                    "❌現在このサーバーで他の処理が進行中です。"
                    "しばらく待ってから再度お試しください。"
                ),
                view=None
            )
            return

        async with lock:

            ids = {str(player["id"]) for player in self.players}

            read_result = await self.manager.read_players_comment(self.bot)
            current_players = (
                read_result["data"]
                if read_result["status"]
                else []
            )

            remaining_players = [
                p
                for p in current_players
                if str(p.get("id")) not in ids
            ]

            write_result = await self.manager.write_players_comment(
                self.bot,
                remaining_players,
                view_factory=lambda idx, total: PlayersDataView(idx, total)
            )

            names = "\n".join(
                f'ID:{player["id"]}  {player["name"]}'
                for player in self.players
            )

            if write_result["status"]:
                content = (
                    "✅ 以下のプレイヤーを削除しました。\n\n"
                    f"{names}"
                )
            else:
                content = (
                    "❌ プレイヤーコメントの更新に失敗しました。\n"
                    f"{write_result['message']}"
                )

            await interaction.response.edit_message(
                content=content,
                view=None
            )


    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content="キャンセルしました。",
            view=None
        )


# ==========================
# /giftcode 実行時の確認ダイアログ
# ==========================
#
# 1. 「全員の交換を実行しますか？」-> はい / いいえ
#    はい    : target_players全員を対象に交換を実行
#    いいえ  : 人数選択ボタン(1人/5人/10人)を表示
# 2. 人数選択後、target_playersの先頭からその人数を対象に交換を実行
#
# 実際の交換処理(execute_exchange等)はcommands.py側が保持しており、
# 循環importを避けるため、実行処理は execute_fn として引数で受け取る。
#
#   execute_fn: async def execute_fn(interaction, players) -> None
#

class GiftCodeCountView(discord.ui.View):

    def __init__(
        self,
        target_players,
        execute_fn,
        timeout=60
    ):
        super().__init__(timeout=timeout)

        self.target_players = target_players
        self.execute_fn = execute_fn

    async def _run(
        self,
        interaction: discord.Interaction,
        number: int
    ):
        selected = self.target_players[:number]

        await interaction.response.edit_message(
            content=(
                f"✅先頭から{len(selected)}人の交換を開始します。"
            ),
            view=None
        )

        await self.execute_fn(interaction, selected)

    @discord.ui.button(
        label="1人",
        style=discord.ButtonStyle.primary
    )
    async def one_person(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self._run(interaction, 1)

    @discord.ui.button(
        label="5人",
        style=discord.ButtonStyle.primary
    )
    async def five_people(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self._run(interaction, 5)

    @discord.ui.button(
        label="10人",
        style=discord.ButtonStyle.primary
    )
    async def ten_people(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self._run(interaction, 10)


class GiftCodeConfirmView(discord.ui.View):

    def __init__(
        self,
        target_players,
        execute_fn,
        timeout=60
    ):
        super().__init__(timeout=timeout)

        self.target_players = target_players
        self.execute_fn = execute_fn

    @discord.ui.button(
        label="はい（全員）",
        style=discord.ButtonStyle.success
    )
    async def confirm_all(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content=(
                f"✅全員（{len(self.target_players)}人）の"
                "交換を開始します。"
            ),
            view=None
        )

        await self.execute_fn(interaction, self.target_players)

    @discord.ui.button(
        label="いいえ（人数を選ぶ）",
        style=discord.ButtonStyle.secondary
    )
    async def choose_count(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.edit_message(
            content="交換する人数を選択してください。",
            view=GiftCodeCountView(
                self.target_players,
                self.execute_fn
            )
        )


# ==========================
# players_data コメント用の永続View
# ==========================
#
# インポートは custom_id を固定した永続ボタンとして実装し、
# 旧/load_csvスラッシュコマンドの代わりとする
# (main.pyでbot.add_view()により登録すればBot再起動後も動作する)。
# バックアップ用の別ボタンは無い: players_dataメッセージ自体に
# 全員分のplayers_data.csvが常に添付されているため、そのまま
# ダウンロードすればバックアップとして使える。
#
# ページ切り替えはSelect(PageSelect)で行う。選択されると、
# 添付ファイル(players_data.csv)から最新の全員分を読み込み直し、
#該当ページだけを本文プレビューとして再表示する
# (添付ファイル自体は書き換えない = 常に1メッセージで完結)。
#

class PageSelect(discord.ui.Select):

    def __init__(self, current_page, total_pages):

        options = [
            discord.SelectOption(
                label=f"ページ {i + 1}/{total_pages}",
                value=str(i),
                default=(i == current_page)
            )
            for i in range(total_pages)
        ]

        super().__init__(
            placeholder=f"ページ {current_page + 1}/{total_pages}",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="giftbot:players_page_select"
        )

    async def callback(self, interaction: discord.Interaction):

        guild = await require_guild(interaction)
        if guild is None:
            return

        manager = await require_registered_channel(interaction, guild)
        if manager is None:
            return

        bot = interaction.client
        selected_page = int(self.values[0])

        read_result = await manager.read_players_comment(bot)
        players = read_result["data"] if read_result["status"] else []

        total_pages = max(
            1, (len(players) + PLAYERS_PAGE_SIZE - 1)
            // PLAYERS_PAGE_SIZE
        )
        selected_page = min(selected_page, total_pages - 1)
        page_players = players[
            selected_page * PLAYERS_PAGE_SIZE:
            (selected_page + 1) * PLAYERS_PAGE_SIZE
        ]

        preview = manager.build_players_preview_content(
            page_players, selected_page, total_pages, len(players)
        )

        # 添付ファイル(players_data.csv)は書き換えない。
        # interactionのedit_messageは明示的にattachmentsを渡さないと
        # 添付が消える場合があるため、既存のものをそのまま指定して保持する。
        await interaction.response.edit_message(
            content=preview,
            attachments=interaction.message.attachments,
            view=PlayersDataView(selected_page, total_pages)
        )


class PlayersDataView(discord.ui.View):

    def __init__(self, page_index=0, total_pages=1):
        super().__init__(timeout=None)

        self.page_index = page_index
        self.total_pages = total_pages

        if total_pages > 1:
            self.add_item(PageSelect(page_index, total_pages))

    @discord.ui.button(
        label="📤 インポート",
        style=discord.ButtonStyle.secondary,
        custom_id="giftbot:players_import"
    )
    async def import_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        guild = await require_guild(interaction)
        if guild is None:
            return

        manager = await require_registered_channel(interaction, guild)
        if manager is None:
            return

        # guild.id(このインタラクションが発生したギルドのみ)をキーにする。
        # 他ギルドのロック・データには一切触れない。
        lock = get_guild_lock(guild.id)

        if lock.locked():
            await interaction.response.send_message(
                "❌現在このサーバーで他の処理が進行中です。"
                "しばらく待ってから再度お試しください。",
                ephemeral=True
            )
            return

        bot = interaction.client

        await interaction.response.defer(ephemeral=True)

        async with lock:

            import_result = await manager.import_players(bot)

            if not import_result["status"]:
                await interaction.followup.send(
                    f"❌{import_result['message']}",
                    ephemeral=True
                )
                return

            players = import_result["data"]

            write_result = await manager.write_players_comment(
                bot,
                players,
                view_factory=lambda idx, total: PlayersDataView(idx, total)
            )

            if not write_result["status"]:
                await interaction.followup.send(
                    "❌players_dataへの反映に失敗しました\n"
                    f"{write_result['message']}",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                f"✅{import_result['message']}\n"
                "players_dataを更新しました。",
                ephemeral=True
            )


# ==========================
# last_result ダッシュボード用View
# ==========================
#
# giftcode実行のたびに新しいインスタンスを生成して都度渡す
# (Bot再起動をまたいだ永続化はしていない: 再起動後は次にgiftcodeが
#  実行されるまで、既存のボタンは反応しなくなる)。
#

_LOG_CATEGORY_EMOJI = {
    RESULT_INVALID_PLAYER: "👤",
    RESULT_NOT_EXCHANGED: "--",
    RESULT_OTHER_ERROR: "❌",
}


_LAST_RESULT_CODE_PATTERN = re.compile(r"最後に交換したギフトコード: `(.+?)`")


class LastResultView(discord.ui.View):

    def __init__(self, code="", log_entries=None):
        super().__init__(timeout=None)

        self.code = code
        self.log_entries = log_entries or []

    @discord.ui.button(
        label="🔁 再交換",
        style=discord.ButtonStyle.secondary
    )
    async def reexchange(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        # self.code はView生成時点のインメモリ値でしかなく、Bot再起動後は
        # 頼りにできない(再起動後は次のgiftcode実行までViewが失われるため)。
        # 実際に表示されているメッセージ本文(=永続化された内容)から
        # コードを読み取り直すことで、再起動後でもメッセージさえ残って
        # いれば正しく動作するようにする。
        code = self.code

        message_content = getattr(interaction.message, "content", "") or ""
        match = _LAST_RESULT_CODE_PATTERN.search(message_content)

        if match:
            code = match.group(1)

        if not code:
            await interaction.response.send_message(
                "コードがまだありません。",
                ephemeral=True
            )
            return

        # commands.pyとviews_discord.pyは互いに依存し合う関係にあるため、
        # 循環importを避けて関数内でのみ遅延importする。
        from commands import start_giftcode_flow

        await start_giftcode_flow(interaction, code, interaction.client)

    @discord.ui.button(
        label="ログの詳細",
        style=discord.ButtonStyle.secondary
    )
    async def log_details(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not self.log_entries:
            await interaction.response.send_message(
                "表示する詳細ログはありません"
                "(成功・交換済み以外の対象がいませんでした)。",
                ephemeral=True
            )
            return

        description = "\n".join(
            f"{_LOG_CATEGORY_EMOJI.get(entry['category'], '・')} "
            f"{entry['name']} ({entry['player_id']}) : {entry['detail']}"
            for entry in self.log_entries
        )

        embed = discord.Embed(
            title="ログの詳細",
            description=description[:4000]
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


# ==========================
# /guild_delete 確認ダイアログ
# ==========================
# パスワード認証は/guild_deleteコマンド側で既に済んでおり、
# このViewはephemeral(実行者本人にしか見えない)ため、
# ボタン側で改めてパスワードを問い直すことはしない。
#
# 実際の削除処理(info_guilds.csvからの完全削除+関連コメント削除)は
# commands.py側が保持しており、循環importを避けるため
# execute_fn として引数で受け取る。
#
#   execute_fn: async def execute_fn(interaction) -> None
#

class GuildDeleteConfirmView(discord.ui.View):

    def __init__(self, execute_fn, timeout=60):
        super().__init__(timeout=timeout)

        self.execute_fn = execute_fn

    @discord.ui.button(
        label="はい",
        style=discord.ButtonStyle.danger
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.execute_fn(interaction)

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.edit_message(
            content="キャンセルしました。",
            view=None
        )

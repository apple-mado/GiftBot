# commands.py

import discord
import re
import asyncio
from discord import app_commands
from functools import wraps
from datetime import datetime, timezone, timedelta

from exchange import run_exchange, format_time
from views_discord import (
    ConfirmDeleteView,
    GiftCodeConfirmView,
    PlayersDataView,
    LastResultView,
    GuildDeleteConfirmView,
)
from chat_manager import (
    ChatManager,
    require_guild,
    require_registered_channel,
    get_guild_lock,
    list_all_guilds,
    find_guild_row_by_id,
    delete_guild_by_id,
    RESULT_SUCCESS,
    RESULT_INVALID_PLAYER,
)
from config import (
    HELP_CONTENT,
    PERMISSIONS,
    ROLE_ADMIN,
    UTC_OFFSET,
    PLAYER_MAX_COUNT,
    MASTER_PASSWORD,
)

JST = timezone(timedelta(hours=UTC_OFFSET))


# ==========================
# 権限チェック(共通デコレータ)
# ==========================
def require_permission(command_name):
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):

            required_roles = PERMISSIONS.get(command_name, {}).get("roles", [])

            if required_roles:
                user_roles = {role.name for role in interaction.user.roles}
                text = (
                    "❌ 権限がありません\n\n"
                    f"必要ロール: {required_roles}\n"
                    f"所持ロール: {list(user_roles)}\n"
                    f"管理者権限: {interaction.user.guild_permissions.administrator}"
                )
                if not (user_roles & set(required_roles)):
                    await interaction.response.send_message(
                        text,
                        ephemeral=True
                    )
                    return

            return await func(
                interaction,
                *args,
                **kwargs
            )

        return wrapper
    return decorator


def _players_view_factory(page_index, total_pages):
    return PlayersDataView(page_index, total_pages)


async def _check_permission(interaction, command_name):
    """
    require_permissionデコレータと同じ判定を、デコレータの外
    (ボタンのコールバック等)からも呼べるようにした関数版。
    """
    required_roles = PERMISSIONS.get(command_name, {}).get("roles", [])

    if not required_roles:
        return True

    user_roles = {role.name for role in interaction.user.roles}

    if user_roles & set(required_roles):
        return True

    await interaction.response.send_message(
        "❌権限がありません",
        ephemeral=True
    )
    return False


async def start_giftcode_flow(interaction, code, bot):
    """
    /giftcode の実処理本体。スラッシュコマンド(giftcode_command)、
    および last_resultコメントの「🔁 再交換」ボタンの両方から
    呼び出される(モジュールレベル関数にすることで、views_discord.py側
    からも遅延importで呼べるようにしている)。

    ボタン経由の場合はスラッシュコマンドのrequire_permissionデコレータを
    経由しないため、ここで明示的に権限チェックを行う
    (last_resultは通常メッセージ=誰でも見える・押せるため必須)。
    """

    if not await _check_permission(interaction, "giftcode"):
        return

    guild = await require_guild(interaction)
    if guild is None:
        return

    manager = await require_registered_channel(interaction, guild)
    if manager is None:
        return

    print(f"giftcode_command called from {guild.name}: {guild.id}", flush=True)

    # このギルド専用のロック(キーはguild.idのみ)。
    # 他ギルドのロック・manager・チャンネル情報には一切触れない。
    lock = get_guild_lock(guild.id)

    if lock.locked():
        await interaction.response.send_message(
            "❌現在このサーバーで他の処理(giftcode等)が進行中です。"
            "完了までお待ちください。",
            ephemeral=True
        )
        return

    # history_dataコメントの生存確認(削除されていればfatalを消費して復旧)
    ensure_result = await manager.ensure_history_comment(bot)

    if not ensure_result["status"]:
        await interaction.response.send_message(
            f"❌{ensure_result['message']}",
            ephemeral=True
        )
        return

    # ライセンス無しギルドは同一コードにつき50回成功までの制限がある
    allowed, remain, limit_message = await manager.check_code_limit(bot, code)

    if not allowed:
        await interaction.response.send_message(
            limit_message,
            ephemeral=True
        )
        return

    read_result = await manager.read_players_comment(bot)
    PLAYERS = read_result["data"] if read_result["status"] else []

    if not PLAYERS:
        await interaction.response.send_message(
            "❌登録プレイヤーがありません"
        )
        return

    target_players = [
        player
        for player in PLAYERS
        if player.get("exchange_status") != RESULT_INVALID_PLAYER
    ]
    skipped_count = len(PLAYERS) - len(target_players)

    if not target_players:
        await interaction.response.send_message(
            "❌対象プレイヤーがいません（全員スキップ対象です）"
        )
        return

    skip_text = (
        f"スキップ:{skipped_count}人(存在しないプレイヤー)\n"
        if skipped_count else ""
    )

    # はい/いいえ確認後にViewから呼び出される実処理
    async def run_giftcode_exchange(confirm_interaction, players_to_process):

        # 確認ダイアログ表示から実行までの間に他の処理が始まっている
        # 可能性があるため、ここでも念のため確認してから取得する。
        if lock.locked():
            return

        async with lock:

            # 今回選ばれなかった対象は未交換として反映しておく
            selected = {id(p) for p in players_to_process}
            for player in target_players:
                if id(player) not in selected:
                    player["exchange_status"] = "未交換"

            start_date_text = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

            async def on_progress(
                counts, elapsed, done, total, log_entries,
                waiting, finished, rate_limited_abort=False
            ):
                if rate_limited_abort:
                    status = "⚠️レート制限により中断"
                elif finished:
                    status = "完了"
                elif waiting:
                    status = "待機中"
                else:
                    status = "処理中"

                dashboard = {
                    "code": code,
                    "date": start_date_text,
                    "elapsed": format_time(elapsed),
                    "counts": counts,
                    "waiting": waiting,
                    "status": status,
                }

                await manager.write_players_comment(
                    bot, PLAYERS, view_factory=_players_view_factory
                )
                await manager.write_last_result(
                    bot,
                    dashboard,
                    view=LastResultView(code=code, log_entries=log_entries)
                )

            counts, elapsed, log_entries = await run_exchange(
                code,
                players_to_process,
                on_progress
            )

            success_count = counts.get(RESULT_SUCCESS, 0)

            if success_count > 0:
                await manager.record_history_success(bot, code, success_count)

    await interaction.response.send_message(
        (
            f"対象:{len(target_players)}人\n"
            f"{skip_text}"
            "全員の交換を実行しますか？"
        ),
        view=GiftCodeConfirmView(
            target_players,
            run_giftcode_exchange
        )
    )


def setup_commands(bot):

#### ==========================
#### HELP
#### ==========================

    @bot.tree.command(
        name="help",
        description="コマンド一覧"
    )
    async def help_command(interaction):

        text = (
            f"{HELP_CONTENT}\n"
            "== コマンド一覧 ==\n"
            + create_command_list()
        )

        await interaction.response.send_message(text[:1900])

#### ==========================
#### ADD PLAYER
#### ==========================

    @bot.tree.command(
        name="add_player",
        description="プレイヤーを登録"
    )
    @app_commands.describe(
        name="プレイヤー名",
        player_id="プレイヤーID",
        state="王国(state)"
    )
    @require_permission("add_player")
    async def add_player_command(
        interaction,
        name: str,
        player_id: int,
        state: int
    ):
        guild = await require_guild(interaction)
        if guild is None:
            return

        manager = await require_registered_channel(interaction, guild)
        if manager is None:
            return

        print(f"add_player_command called from {guild.name}: {guild.id}", flush=True)

        # このギルド専用のロック。他ギルドのロックとは完全に独立している
        # (キーはguild.idのみ。channel_idや他ギルドのmanagerは一切参照しない)。
        lock = get_guild_lock(guild.id)

        if lock.locked():
            await interaction.response.send_message(
                "❌現在このサーバーで他の処理が進行中です。"
                "しばらく待ってから再度お試しください。",
                ephemeral=True
            )
            return

        async with lock:

            read_result = await manager.read_players_comment(bot)
            PLAYERS = read_result["data"] if read_result["status"] else []

            if len(PLAYERS) >= PLAYER_MAX_COUNT:
                await interaction.response.send_message(
                    f"❌登録上限({PLAYER_MAX_COUNT}人)に達しているため、"
                    "これ以上登録できません",
                    ephemeral=True
                )
                return

            if any(player["player_id"] == player_id for player in PLAYERS):
                await interaction.response.send_message(
                    "❌同じプレイヤーIDが登録されています",
                    ephemeral=True
                )
                return

            if any(player["name"] == name for player in PLAYERS):
                await interaction.response.send_message(
                    "❌同じ名前が登録されています",
                    ephemeral=True
                )
                return

            new_id = 1
            if PLAYERS:
                new_id = max(p["id"] for p in PLAYERS) + 1

            PLAYERS.append({
                "id": new_id,
                "name": name,
                "player_id": player_id,
                "state": state,
                "exchange_status": ""
            })

            write_result = await manager.write_players_comment(
                bot, PLAYERS, view_factory=_players_view_factory
            )

            if not write_result["status"]:
                await interaction.response.send_message(
                    "❌players_dataの更新に失敗しました\n"
                    f"{write_result['message']}",
                    ephemeral=True
                )
                return

            await interaction.response.send_message(
                "✅登録完了\n"
                f"名前:{name}\n"
                f"ID:{player_id}\n"
                f"王国:{state}"
            )

#### ==========================
#### GET PLAYER (動作確認用: 登録人数のみ表示)
#### ==========================

    @bot.tree.command(
        name="get_player",
        description="登録人数を確認(動作確認用)"
    )
    @require_permission("get_player")
    async def get_player_command(interaction):

        guild = await require_guild(interaction)
        if guild is None:
            return

        manager = await require_registered_channel(interaction, guild)
        if manager is None:
            return

        print(f"get_player_command called from {guild.name}: {guild.id}", flush=True)

        read_result = await manager.read_players_comment(bot)
        players = read_result["data"] if read_result["status"] else []

        await interaction.response.send_message(
            f"📋現在の登録人数: {len(players)}人"
        )

#### ==========================
#### DELETE PLAYER
#### ==========================

    @bot.tree.command(
        name="delete_player",
        description="プレイヤーを削除"
    )
    @app_commands.describe(
        id="削除するプレイヤーのID(カンマ/スペース区切りで複数指定可、最大5件)"
    )
    @require_permission("delete_player")
    async def delete_player_command(
        interaction,
        id: str
    ):
        guild = await require_guild(interaction)
        if guild is None:
            return

        manager = await require_registered_channel(interaction, guild)
        if manager is None:
            return

        print(f"delete_player_command called from {guild.name}: {guild.id}", flush=True)

        lock = get_guild_lock(guild.id)

        if lock.locked():
            await interaction.response.send_message(
                "❌現在このサーバーで他の処理が進行中です。"
                "しばらく待ってから再度お試しください。",
                ephemeral=True
            )
            return

        read_result = await manager.read_players_comment(bot)
        PLAYERS = read_result["data"] if read_result["status"] else []

        ids = []
        seen = set()

        for s in re.split(r"[,\s]+", id.strip()):

            if not s.isdigit():
                continue

            value = int(s)

            if value in seen:
                continue

            seen.add(value)
            ids.append(value)

            if len(ids) >= 5:
                break

        target_players = [
            player
            for player in PLAYERS
            if int(player["id"]) in ids
        ]

        if not target_players:
            await interaction.response.send_message(
                "指定されたIDが見つかりません。",
                ephemeral=True
            )
            return

        text = "\n".join(
            f'ID:{p["id"]}　{p["name"]}　({p["player_id"]})'
            for p in target_players
        )

        await interaction.response.send_message(
            content=(
                "以下のプレイヤーを削除しますか？\n\n"
                f"{text}"
            ),
            view=ConfirmDeleteView(target_players, manager, bot),
            ephemeral=True
        )

#### ==========================
#### GIFT CODE EXCHANGE
#### ==========================
# 実処理本体は start_giftcode_flow() (モジュールレベル関数) に
# 切り出してある。last_resultコメントの「🔁 再交換」ボタンからも
# 同じ処理を呼び出すため。

    @bot.tree.command(
        name="giftcode",
        description="ギフトコード交換"
    )
    @app_commands.describe(code="ギフトコード")
    async def giftcode_command(
        interaction,
        code: str
    ):
        await start_giftcode_flow(interaction, code, bot)

#### ==========================
#### SETUP CHANNEL
#### ==========================

    @bot.tree.command(
        name="setup",
        description="GiftBotの利用チャンネルを登録"
    )
    @require_permission("setup")
    async def setup_command(
        interaction
    ):
        guild = await require_guild(interaction)
        if guild is None:
            return

        # テキストチャンネルのみ登録を許可する
        # (音声チャンネル・フォーラムチャンネル・フォーラム内の投稿(Thread)は非対応)
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌テキストチャンネルのみ登録が有効です。"
                "音声チャンネル、フォーラムチャンネルでは登録できません。",
                ephemeral=True
            )
            return

        print(f"setup_command called from {guild.name}: {guild.id}", flush=True)

        manager = ChatManager(
            guild_id=guild.id,
            channel_id=interaction.channel_id
        )

        exist_result = manager.exist()

        if exist_result["status"] and not manager.is_disabled():
            registered_channel_id = exist_result["data"]["cha_id"]
            await interaction.response.send_message(
                "❌このサーバーは既にチャンネルが登録されています。\n"
                f"登録チャンネル: <#{registered_channel_id}>\n"
                "変更する場合は先に /reject を実行してください。",
                ephemeral=True
            )
            return

        if exist_result["status"] and manager.is_disabled():
            # 無効化されていた登録が残っている場合、削除・新規作成し直さず
            # その場を再度有効化する(fatalが1以上ある場合のみ。
            # fatal自体はリセットせず通算のまま維持する)
            setup_result = manager.reactivate(
                guild_name=guild.name,
                channel_name=interaction.channel.name
            )
        else:
            setup_result = manager.create(
                guild_name=guild.name,
                channel_name=interaction.channel.name
            )

        if not setup_result["status"]:
            await interaction.response.send_message(
                f"❌登録に失敗しました\n{setup_result['message']}",
                ephemeral=True
            )
            return

        # players_data -> history_data -> last_result の順でコメントを投稿
        post_result = await manager.post_initial_comments(
            interaction.channel,
            players_view=PlayersDataView(0, 1),
            last_result_view=LastResultView()
        )

        if not post_result["status"]:
            await interaction.response.send_message(
                "⚠️チャンネル登録は完了しましたが、"
                "コメントの投稿に失敗しました。\n"
                f"{post_result['message']}",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅このチャンネルをGiftBotの利用チャンネルとして登録しました。\n"
            f"サーバー:{guild.name}\n"
            f"チャンネル:{interaction.channel.name}\n"
            "players_data/history_data/last_resultコメントを投稿しました。"
        )

#### ==========================
#### REJECT CHANNEL
#### ==========================

    @bot.tree.command(
        name="reject",
        description="登録済みの利用チャンネルを解除"
    )
    @require_permission("reject")
    async def reject_command(
        interaction
    ):
        guild = await require_guild(interaction)
        if guild is None:
            return

        print(f"reject_command called from {guild.name}: {guild.id}", flush=True)

        manager = ChatManager(
            guild_id=guild.id,
            channel_id=interaction.channel_id
        )

        exist_result = manager.exist()

        if not exist_result["status"]:
            await interaction.response.send_message(
                "❌このサーバーには登録されたチャンネルがありません。",
                ephemeral=True
            )
            return

        # 3メッセージ自体を削除しておく(self._entryがまだ有効な間に行う)
        delete_comments_result = await manager.delete_comments(bot)

        # 行自体は削除せず、fatalを1減らして無効化する
        # (chat_settings.csvからの削除はしない。fatalが1以上残っていれば
        #  /setupでreactivateされる)
        deactivate_result = manager.deactivate()
        remaining_fatal = (
            deactivate_result["data"]["fatal"]
            if deactivate_result["status"] and deactivate_result["data"]
            else "?"
        )

        await interaction.response.send_message(
            "✅このチャンネルの登録を無効化しました。\n"
            "(players_data/history_data/last_resultのコメントも削除しました)\n"
            f"残りfatal:{remaining_fatal}\n"
            "再度使用する場合は、このチャンネルで /setup を実行してください。"
        )

#### ==========================
#### GUILD LIST (管理者用・パスワード認証)
#### ==========================
# ロール(PERMISSIONS)ではなく専用パスワードで認証する管理者コマンド。
# 意図的にPERMISSIONS/ヘルプ一覧には含めず、通常ユーザーからは
# 見えないようにしている。
#
# ⚠️ 注意: スラッシュコマンドの引数はDiscordの仕様上、実行者以外の
# チャンネル参加者にも「/guild_list password:2131」のように
# 入力内容が見える場合がある(応答自体はephemeralで実行者にしか
# 見えないが、パスワードの入力そのものは他人に見られうる)。
####

    @bot.tree.command(
        name="guild_list",
        description="登録ギルド一覧を表示(管理者用)"
    )
    @app_commands.describe(password="管理者用パスワード")
    async def guild_list_command(
        interaction,
        password: str
    ):
        if password != MASTER_PASSWORD:
            await interaction.response.send_message(
                "❌権限がありません",
                ephemeral=True
            )
            return

        rows = list_all_guilds()

        if not rows:
            await interaction.response.send_message(
                "登録されているギルドはありません",
                ephemeral=True
            )
            return

        lines = ["📋登録ギルド一覧(id / guild_name / fatal / active)\n"]

        for row in rows:
            lines.append(
                f"{row.get('id')} / {row.get('guild_name')} / "
                f"fatal:{row.get('fatal')} / active:{row.get('active')}"
            )

        text = "\n".join(lines)

        await interaction.response.send_message(
            text[:1900],
            ephemeral=True
        )

#### ==========================
#### GUILD DELETE (管理者用・パスワード認証)
#### ==========================
# guild_listと同じくPERMISSIONS/ヘルプ一覧には含めない。
# /rejectと違い、info_guilds.csvの行自体を完全に削除する(取り消し不可)。

    @bot.tree.command(
        name="guild_delete",
        description="登録ギルドを完全に削除(管理者用)"
    )
    @app_commands.describe(
        password="管理者用パスワード",
        id="guild_listの先頭列(idであり、guild_idではない)"
    )
    async def guild_delete_command(
        interaction,
        password: str,
        id: str
    ):
        if password != MASTER_PASSWORD:
            await interaction.response.send_message(
                "❌権限がありません",
                ephemeral=True
            )
            return

        target_row = find_guild_row_by_id(id)

        if target_row is None:
            await interaction.response.send_message(
                f"❌id:{id} は見つかりませんでした",
                ephemeral=True
            )
            return

        async def do_delete(confirm_interaction):
            # 行を消す前に、players_data/history_data/last_resultの
            # 3メッセージ自体も削除しておく
            manager = ChatManager(
                guild_id=target_row["guild_id"],
                channel_id=target_row.get("cha_id")
            )
            manager.exist()
            await manager.delete_comments(bot)

            delete_result = delete_guild_by_id(id)

            if delete_result["status"]:
                await confirm_interaction.response.edit_message(
                    content=f"✅{delete_result['message']}",
                    view=None
                )
            else:
                await confirm_interaction.response.edit_message(
                    content=f"❌削除に失敗しました\n{delete_result['message']}",
                    view=None
                )

        await interaction.response.send_message(
            f"「{target_row.get('guild_name')}」(id:{id})を完全に削除しますか？\n"
            "この操作は取り消せません。",
            view=GuildDeleteConfirmView(do_delete),
            ephemeral=True
        )


# ==========================
# Other Functions
# ==========================

def create_command_list():
    lines = []

    for name, info in PERMISSIONS.items():
        roles = info.get("roles", [])
        role_text = "/".join(roles) if roles else "全員"
        lines.append(f"/{name} : {info.get('description', '')} (権限:{role_text})")

    return "\n".join(lines)

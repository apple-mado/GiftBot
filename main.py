# main.py

import discord
from discord.ext import commands

import os
from commands import setup_commands
from views_discord import PlayersDataView
from config import GITHUB_TOKEN

# ==========================
# BOT認証　GitHub認証
# ==========================

#discord bot　の認証に必要
DISCORD_TOKEN = os.getenv(
    "DISCORD_TOKEN"
)


if not DISCORD_TOKEN:
    raise Exception(
        "DISCORD_TOKEN が設定されていません"
    )
if not GITHUB_TOKEN:
    raise Exception(
        "GITHUB_TOKEN が設定されていません"
    )

intents = discord.Intents.default()
intents.message_content = True
# ⚠️ 既知の制約(対応不要と判断済み):
# Intents.default()には message_content(特権インテント)が含まれていない。
# このインテントが無いと、Bot自身が送信したメッセージ以外は
# content/embeds/attachments が空になる(Discord側の仕様)。
# → players_dataの「📤インポート」ボタンはチャンネル内の
#   "ユーザーが投稿したCSV添付"を読む必要があるため、このままでは
#   実運用で見つけられない場合がある。
#   ただしその場合もimport_players()は例外を出さず、
#   「見つかりませんでした」という結果を返して安全に終了する
#   (無限ループ・ハング・クラッシュはしない)ため、機能停止という
#   形では実害は出ない設計になっている。

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

setup_commands(bot)

@bot.event
async def on_ready():
    # players_dataコメントの「📤インポート」ボタン、および
    # ページ切り替え用のSelect(PageSelect)をBot再起動後も動作する
    # 永続Viewとして登録する。
    #
    # 注意: discord.pyの永続View登録は「そのインスタンスに実際に
    # 含まれているcomponentのcustom_id」単位で行われる。
    # PageSelectはtotal_pages<=1のときViewに追加されない実装のため、
    # ここで登録するインスタンスはtotal_pages=2を指定し、Select自身の
    # custom_idを確実に登録する(表示内容は実際のメッセージ側で都度
    # 正しく再計算されるため、ここで渡すpage数自体に意味は無い)。
    bot.add_view(PlayersDataView(0, 2))

    synced = await bot.tree.sync()

        
bot.run(DISCORD_TOKEN)




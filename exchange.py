import asyncio
import time
import requests

from wos_api_client import redeem_gift_code
from config import (
    EXCHANGE_PROGRESS_INTERVAL,
    REPORTING_NUMBER,
    RESULT_SUCCESS,
    RESULT_ALREADY_EXCHANGED,
    RESULT_INVALID_PLAYER,
    RESULT_OTHER_ERROR,
    RESULT_NOT_EXCHANGED,
)


def format_time(seconds: float) -> str:
    total = int(seconds)

    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60

    if hours:
        return f"{hours}時間{minutes}分{seconds}秒"
    elif minutes:
        return f"{minutes}分{seconds}秒"
    else:
        return f"{seconds}秒"


def _categorize(success, message):
    """
    autoplay/wos_api_clientの(success, message)を、
    LastResultダッシュボードの5分類のいずれかに変換する。
    戻り値: (category, detail_text)
    """

    if success:
        return RESULT_SUCCESS, RESULT_SUCCESS

    detail = message[1] if isinstance(message, tuple) else str(message)

    if detail == RESULT_ALREADY_EXCHANGED:
        return RESULT_ALREADY_EXCHANGED, detail

    if detail == RESULT_INVALID_PLAYER:
        return RESULT_INVALID_PLAYER, detail

    return RESULT_OTHER_ERROR, detail


async def run_exchange(code, players_to_process, on_progress):
    """
    players_to_process: 今回処理するプレイヤーのlist(dict)。
                         各dictの"exchange_status"に直接結果を書き込む
                         (参照渡しのため、呼び出し元のPLAYERSにも反映される)。

    on_progress: async def on_progress(counts, elapsed, done, total,
                                        log_entries, waiting, finished,
                                        rate_limited_abort=False)
                 REPORTING_NUMBER人ごと・待機発生時・完了時に呼ばれる。
                 rate_limited_abortは、レート制限が連続3回発生して
                 安全に処理を打ち切った場合のみTrueになる(finished時のみ)。
                 呼び出し側でplayers_data/LastResultの再描画を行う想定。

    戻り値: (counts: dict, elapsed: float, log_entries: list)
    """

    start_time = time.time()
    total = len(players_to_process)

    counts = {
        RESULT_SUCCESS: 0,
        RESULT_ALREADY_EXCHANGED: 0,
        RESULT_INVALID_PLAYER: 0,
        RESULT_OTHER_ERROR: 0,
        RESULT_NOT_EXCHANGED: 0,
    }

    # ログの詳細(成功・交換済みを除く)。先頭から最大50件まで保持する。
    log_entries = []
    LOG_DETAIL_MAX = 50

    with requests.Session() as session:

        offset = 0
        # レート制限(RETRY)が連続で何回発生したかだけを数える専用カウンタ。
        # CONTINUE系(成功/交換済み/存在しないプレイヤー等)の処理では
        # 一切増減しない(別物として扱う)。成功・失敗を問わずRETRY以外の
        # 結果が出るたびに0へリセットする。
        rate_limit_retry_count = 0
        RATE_LIMIT_RETRY_MAX = 3
        rate_limited_abort = False

        while offset < total:

            player = players_to_process[offset]

            name = player["name"]
            player_id = player["player_id"]
            state = player["state"]

            success, message = await asyncio.to_thread(
                redeem_gift_code,
                player_id,
                state,
                code,
                index=offset + 1,
                length=total,
                session=session,
            )

            if message[0] == "RETRY":
                rate_limit_retry_count += 1

                if rate_limit_retry_count >= RATE_LIMIT_RETRY_MAX:
                    # レート制限が連続で規定回数に達したため、
                    # これ以上は無限に待たず安全に処理を終了する。
                    # (現在のプレイヤー含め、残りは全員「未交換」になる)
                    rate_limited_abort = True
                    break

                try:
                    wait_sec = int(message[1])
                except ValueError:
                    wait_sec = 5

                await on_progress(
                    dict(counts),
                    time.time() - start_time,
                    offset,
                    total,
                    list(log_entries),
                    True,
                    False
                )
                await asyncio.sleep(wait_sec)
                # offsetを増やさないので同じ人を再実行
                continue

            rate_limit_retry_count = 0

            category, detail = _categorize(success, message)
            player["exchange_status"] = category
            counts[category] += 1

            if category not in (RESULT_SUCCESS, RESULT_ALREADY_EXCHANGED):
                if len(log_entries) < LOG_DETAIL_MAX:
                    log_entries.append({
                        "name": name,
                        "player_id": player_id,
                        "category": category,
                        "detail": detail
                    })

            aborted = (not success) and message[0] == "ABORT"

            offset += 1

            if aborted:
                break

            if offset % REPORTING_NUMBER == 0 or offset == total:
                await on_progress(
                    dict(counts),
                    time.time() - start_time,
                    offset,
                    total,
                    list(log_entries),
                    False,
                    False
                )

            await asyncio.sleep(EXCHANGE_PROGRESS_INTERVAL)

        # 途中で処理が抜けた場合、残りのプレイヤーは必ず未交換に分類する
        for remaining_player in players_to_process[offset:]:
            remaining_player["exchange_status"] = RESULT_NOT_EXCHANGED
            counts[RESULT_NOT_EXCHANGED] += 1

        elapsed = time.time() - start_time

        await on_progress(
            dict(counts),
            elapsed,
            total,
            total,
            list(log_entries),
            False,
            True,
            rate_limited_abort
        )

        return counts, elapsed, log_entries

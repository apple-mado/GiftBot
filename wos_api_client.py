# wos_api_client.py
#
# WOS(Whiteout Survival)ギフトコード交換APIへのHTTP POSTリクエストを担当する。
# (旧autoplay2.py。旧autoplay.pyはPlaywrightによるブラウザ操作の実装だったが
#  内部APIを直接叩くこの方式に置き換えられ不要になったため削除済み)

import hashlib
import time
import requests

# requests.Session()は呼び出し側(exchange.run_exchange)が
# 渡されたplayersリストの処理が終わるまでの間だけ生成・使い回す。
# ここではそのSessionを受け取って使うだけで、生成/破棄の責務は持たない。
from config import (WOS_API_URL,WOS_SECRET,ERRORS)

def create_sign(
    player_id: str,
    gift_code: str,
    state: str,
    timestamp: int
):

    raw = (
        f"cdk={gift_code}"
        f"&fid={player_id}"
        f"&kid={state}"
        f"&time={timestamp}"
    )

    return hashlib.md5(
        (raw + WOS_SECRET).encode("utf-8")
    ).hexdigest()



def redeem_gift_code(
    player_id: str,
    state: str,
    gift_code: str,
    index: int,
    length: int,
    session: requests.Session
):
    """
    1プレイヤー分のギフトコード交換をWOS APIにPOSTする。

    session: 呼び出し元(exchange.run_exchange)が生成した
             requests.Session。プレイヤーリストの処理が終わるまで
             使い回すことでTCP/TLS接続のオーバーヘッドを削減する。

    戻り値: (success: bool, message: tuple[str, str | int])
        message[0]: "CONTINUE" / "ABORT" / "RETRY" (exchange.py側の制御に使用)
        message[1]: 結果の詳細(表示用テキスト、またはRETRY時の待機秒数)
    """

    try:

        timestamp = int(time.time())
            
        sign = create_sign(
            player_id,
            gift_code,
            state,
            timestamp
        )
        payload = {
            "sign": sign,
            "fid": player_id,
            "cdk": gift_code,
            "kid": state,
            "time": timestamp
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://wos-giftcode.centurygame.com",
            "Referer": "https://wos-giftcode.centurygame.com/",
            "User-Agent": "Mozilla/5.0"
        }
            
        response = session.post(
            WOS_API_URL,
            json=payload,
            headers=headers,
            timeout=10
        )

        result = response.json()
        mymsg = result.get("msg")
  
        # Many Requests Attempt Error =429
        if response.status_code == 429:     
            retry_after = response.headers.get("Retry-After", "60")
            wait_sec = int(retry_after) + 1
            return False, ("RETRY",wait_sec)
    
        if mymsg is None:
            mymsg= ""

        # 成功
        if mymsg == "SUCCESS":
            return True, ("CONTINUE","成功")
        else:
        # エラー
            for key,message in ERRORS.items():
                if key in mymsg:
                    return False,message

        # エラーNEWキーワード
        return False, (
            "CONTINUE",f"不明なPOST: {result}"
        )


    except requests.Timeout:
        return False, ("ABORT","APIタイムアウト")

    except Exception as e:
        return False, ("ABORT",str(e))

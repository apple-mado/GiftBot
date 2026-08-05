# file_manager.py


# ==========================
# ファイル操作のクラス(CSVレコード専用)
# ==========================
#
# 現状chat_settings.csv(storage="local")のみが実際に使われているが、
# GitHub保存(storage="git")の機能自体は今後の用途(履歴のクラウド保存等)
# のために維持している。
#
# git/localという保存先の違いはあるが、
#   「CSVテキスト <-> dictのlist」の変換(_encode/_decode)
#   「そのテキストをどこから読み/どこへ書くか」(_read_*/_write_*)
# の2層に分けることで、create/load/save側の分岐を減らしている。
#
# GitHub側は、直前のload()/save()で取得済みのsha(コミットハッシュ)を
# インスタンス内にキャッシュ(self._cached_sha)しておき、以後の保存では
# 同じファイルへの更新確認のGETを都度繰り返さない。sha競合(他プロセスの
# 更新等)で書き込みが失敗した場合のみ、1回だけ最新shaを取り直して
# 再試行する。

import io
import csv
import base64
import requests

from pathlib import Path
from config import (
    GITHUB_USER,
    GITHUB_REPO,
    GITHUB_BRANCH,
    GITHUB_TOKEN
)

# GitHubがsha不一致を示す際に返しうるステータスコード
_GITHUB_SHA_CONFLICT_STATUS = (409, 422)


class FileManager:

    def __init__(
        self,
        *,
        filepath,
        fields=None,
        storage="git"
    ):
        self.path = filepath
        self.fields = fields or {}
        self.storage = storage.lower()

        # git storage専用: 直近に確認済みのファイルsha。
        # load()/save()の成功のたびに更新され、次回の保存で
        # 重複したGETを避けるために使う。
        self._cached_sha = None

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
    # 型変換
    # ==========================
    def _safe_convert(
        self,
        converter,
        value
    ):
        try:

            if value in ("", None):
                return None

            return converter(value)

        except (
            ValueError,
            TypeError
        ):
            return None

    # ==================================================
    # CSV <-> dictのlist 変換(内部用、storageに依存しない)
    # ==================================================

    def _encode(self, rows):
        """dictのlist -> CSVテキスト"""

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fieldnames=self.fields.keys()
        )

        writer.writeheader()
        writer.writerows(rows)

        return output.getvalue()

    def _decode(self, content):
        """CSVテキスト -> dictのlist(フィールド定義に沿って型変換)"""

        reader = csv.DictReader(io.StringIO(content))

        return [
            {
                field:
                self._safe_convert(
                    converter,
                    row.get(field)
                )
                for field, converter
                in self.fields.items()
            }
            for row in reader
        ]

    # ==================================================
    # GitHub(storage="git"のバックエンド)
    # ==================================================

    def _github_url(self):

        return (
            f"https://api.github.com/repos/"
            f"{GITHUB_USER}/"
            f"{GITHUB_REPO}/contents/"
            f"{self.path}"
        )

    def _github_headers(self):

        return {
            "Authorization":f"Bearer {GITHUB_TOKEN}",
            "Accept":"application/vnd.github+json"
        }

    def _get_github_file(self):

        if not GITHUB_TOKEN:
            return None

        response = requests.get(
            self._github_url(),
            headers=self._github_headers(),
            params={"ref": GITHUB_BRANCH}
        )

        if response.status_code != 200:
            return None

        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8")

        # 取得できたshaをキャッシュしておく(次のsave()で使い回す)
        self._cached_sha = data["sha"]

        return (content, data["sha"])

    def _put_github_file(self, content, sha):
        """PUTのみを行う内部ヘルパー(shaの決定はここでは行わない)"""

        data = {
            "message": f"Update {self.path}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": GITHUB_BRANCH,
        }

        if sha:
            data["sha"] = sha

        response = requests.put(
            self._github_url(),
            headers=self._github_headers(),
            json=data
        )

        return response

    def _save_github_file(
        self,
        content
    ):
        # shaは基本的にキャッシュ(self._cached_sha)を使い回し、
        # 直前にload()/save()していれば追加のGETを行わない。
        # 初回保存でキャッシュが無い場合のみ、ファイルの有無を
        # 確認するために1回だけGETする(新規作成か更新かの判定に必須)。
        if not GITHUB_TOKEN:
            return False

        if self._cached_sha is None:
            self._get_github_file()  # 成功すれば_cached_shaが埋まる

        response = self._put_github_file(content, self._cached_sha)

        if response.status_code in (200, 201):
            self._cached_sha = self._extract_sha(response)
            return True

        if response.status_code in _GITHUB_SHA_CONFLICT_STATUS:
            # キャッシュしていたshaが古くなっていた(他プロセスの更新等)。
            # 1回だけ最新shaを取り直して再試行する。
            self._cached_sha = None
            self._get_github_file()

            retry_response = self._put_github_file(content, self._cached_sha)

            if retry_response.status_code in (200, 201):
                self._cached_sha = self._extract_sha(retry_response)
                return True

            print(
                "GitHub(retry failed):",
                retry_response.status_code,
                retry_response.text,
                flush=True
            )
            return False

        print(
            "GitHub:",
            response.status_code,
            response.text,
            flush=True
        )

        return False

    def _extract_sha(self, response):
        try:
            return response.json()["content"]["sha"]
        except Exception:
            return None

    # ==================================================
    # Local(storage="local"のバックエンド)
    # ==================================================

    def _read_local_file(self):

        with open(
            self.path,
            "r",
            encoding="utf-8",
            newline=""
        ) as f:

            return f.read()

    def _write_local_file(self, content):

        # 保存先フォルダが無ければ作成
        Path(self.path).parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with open(
            self.path,
            "w",
            encoding="utf-8",
            newline=""
        ) as f:

            f.write(content)

    # ==================================================
    # CREATE
    # ==================================================

    def create(self):

        try:

            content = self._encode([])

            if self.storage == "git":

                if not self._save_github_file(content):
                    return self._result(
                        False,
                        "create",
                        f"create failed: {self.path}"
                    )

                return self._result(
                    True,
                    "create",
                    f"create file: {self.path}"
                )

            elif self.storage == "local":

                if Path(self.path).exists():
                    return self._result(
                        True,
                        "exists",
                        f"file exists: {self.path}"
                    )

                self._write_local_file(content)

                return self._result(
                    True,
                    "create",
                    f"create file: {self.path}"
                )

            return self._result(
                False,
                "create",
                f"unsupported storage: {self.storage}"
            )

        except Exception as e:

            return self._result(
                False,
                "create",
                str(e)
            )

    # ==================================================
    # LOAD
    # ==================================================

    def load(self):

        try:

            action = "load"

            if self.storage == "git":

                result = self._get_github_file()

                if not result:
                    empty_content = self._encode([])

                    # 直前のGETで「存在しない」と分かっている(sha不要)ため、
                    # _save_github_file()の再確認GETを経由せず直接PUTする。
                    response = self._put_github_file(empty_content, None)

                    if response.status_code not in (200, 201):
                        return self._result(
                            False,
                            "load",
                            f"create failed: {self.path}"
                        )

                    self._cached_sha = self._extract_sha(response)

                    # 作成した内容は既知なので、再度GETし直さない
                    action = "create_and_load"
                    content = empty_content

                else:
                    content, _ = result

            elif self.storage == "local":

                if not Path(self.path).exists():

                    create_result = self.create()

                    if not create_result["status"]:
                        return create_result

                    action = "create_and_load"

                content = self._read_local_file()

            else:

                return self._result(
                    False,
                    "load",
                    f"unsupported storage: {self.storage}"
                )

            return self._result(
                True,
                action,
                f"{action}: {self.path}",
                self._decode(content)
            )

        except Exception as e:

            return self._result(
                False,
                "load",
                str(e)
            )

    # ==================================================
    # SAVE
    # ==================================================

    def save(
        self,
        data
    ):

        try:

            content = self._encode(data)

            if self.storage == "git":

                if not self._save_github_file(content):
                    return self._result(
                        False,
                        "save",
                        f"save failed: {self.path}"
                    )

                return self._result(
                    True,
                    "save",
                    f"save file: {self.path}"
                )

            elif self.storage == "local":

                self._write_local_file(content)

                return self._result(
                    True,
                    "save",
                    f"save file: {self.path}"
                )

            return self._result(
                False,
                "save",
                f"unsupported storage: {self.storage}"
            )

        except Exception as e:
            return self._result(
                False,
                "save",
                str(e)
            )

    # ==================================================
    # DELETE
    # Local専用
    # ==================================================

    def delete(self):

        try:
            if self.storage != "local":
                return self._result(
                    False,
                    "delete",
                    (
                        "delete is available "
                        "only for local storage"
                    )
                )

            path = Path(self.path)

            if not path.exists():
                return self._result(
                    True,
                    "delete",
                    f"file not found: {self.path}"
                )

            path.unlink()

            return self._result(
                True,
                "delete",
                f"delete file: {self.path}"
            )

        except Exception as e:
            return self._result(
                False,
                "delete",
                str(e)
            )

    # ==================================================
    # APPEND
    # ==================================================

    def append(self, row_data):

        result = self.load()

        if not result["status"]:
            return result

        data = result["data"]
        data.append(row_data)

        return self.save(data)

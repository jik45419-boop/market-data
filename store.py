# -*- coding: utf-8 -*-
"""Google Drive 存取（用你本人的 OAuth 授權，只能存取本程式建立的檔案）"""
import os
import shutil
import time
from pathlib import Path

FOLDER_NAME = os.environ.get("DRIVE_FOLDER", "market-data")
FOLDER_MIME = "application/vnd.google-apps.folder"
HTTP_TIMEOUT = 300      # 秒；預設 60 秒，Drive 偶爾回應慢會「read operation timed out」
TRIES = 4               # 連線逾時／中斷時整個請求重來的次數（另外每次內部還有 num_retries=5）


def _run(fn, what):
    """執行 Drive 請求；遇到逾時或連線中斷，等一下再重來"""
    for i in range(TRIES):
        try:
            return fn()
        except (TimeoutError, ConnectionError, OSError) as e:
            if i == TRIES - 1:
                raise
            wait = 30 * (i + 1)
            print(f"Drive {what} 連線問題（{type(e).__name__}: {e}），{wait} 秒後重試（第 {i + 2} 次）", flush=True)
            time.sleep(wait)


def _clean(key):
    """去掉複製時可能夾帶的引號、空白、換行"""
    v = "".join(os.environ[key].split()).strip("\"',")
    if key == "GDRIVE_REFRESH_TOKEN" and not v.startswith("1//"):
        import re
        m = re.search(r"1//[0-9A-Za-z_\-]+", v)   # 貼了整段回應時，自動取出 refresh token
        if m:
            v = m.group(0)
    return v


class DriveStore:
    def __init__(self):
        missing = [k for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET",
                               "GDRIVE_REFRESH_TOKEN") if not os.environ.get(k, "").strip()]
        if missing:
            raise RuntimeError("GitHub Secrets 尚未設定：" + "、".join(missing))
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            None,
            refresh_token=_clean("GDRIVE_REFRESH_TOKEN"),
            client_id=_clean("GDRIVE_CLIENT_ID"),
            client_secret=_clean("GDRIVE_CLIENT_SECRET"),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        except Exception as e:
            t, c, k = (_clean(x) for x in ("GDRIVE_REFRESH_TOKEN", "GDRIVE_CLIENT_ID",
                                          "GDRIVE_CLIENT_SECRET"))
            diag = (f"refresh_token 長度 {len(t)}、開頭是 1//：{t.startswith('1//')}；"
                    f"client_id 長度 {len(c)}、結尾正確：{c.endswith('.apps.googleusercontent.com')}；"
                    f"client_secret 長度 {len(k)}、開頭是 GOCSPX-：{k.startswith('GOCSPX-')}")
            raise RuntimeError(f"Google 授權失敗（{e}）。檢查：{diag}") from e
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        self.d = build("drive", "v3", http=AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT)),
                       cache_discovery=False)
        self.folder = self._folder()
        self.files = self._list()

    def _folder(self):
        q = (f"name='{FOLDER_NAME}' and mimeType='{FOLDER_MIME}' "
             "and trashed=false")
        res = self.d.files().list(q=q, fields="files(id,name)",
                                  spaces="drive").execute(num_retries=5)
        if res.get("files"):
            return res["files"][0]["id"]
        f = self.d.files().create(
            body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME},
            fields="id").execute(num_retries=5)
        print(f"已在 Drive 建立資料夾 {FOLDER_NAME}", flush=True)
        return f["id"]

    def _list(self):
        out, token = {}, None
        while True:
            res = _run(lambda: self.d.files().list(
                q=f"'{self.folder}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,size)", orderBy="modifiedTime",
                pageSize=1000, pageToken=token).execute(num_retries=5), "列出檔案")   # 同名時以最新的為準
            for f in res.get("files", []):
                out[f["name"]] = f["id"]
            token = res.get("nextPageToken")
            if not token:
                return out

    def exists(self, name):
        return name in self.files

    def download(self, name, path):
        from googleapiclient.http import MediaIoBaseDownload

        def go():
            req = self.d.files().get_media(fileId=self.files[name])
            with open(path, "wb") as f:      # 重試時從頭重新下載
                dl = MediaIoBaseDownload(f, req, chunksize=32 * 1024 * 1024)
                done = False
                while not done:
                    _, done = dl.next_chunk(num_retries=5)
        _run(go, f"下載 {name}")

    def upload(self, path, name, mimetype="application/gzip"):
        from googleapiclient.http import MediaFileUpload
        # 不覆蓋舊檔：覆蓋會讓 Drive 保留舊版本 30 天並佔用空間。改成先上傳新檔，成功後永久刪除舊檔
        old = self.files.get(name)

        def go():
            m = MediaFileUpload(str(path), mimetype=mimetype, resumable=True, chunksize=32 * 1024 * 1024)
            return self.d.files().create(
                body={"name": name, "parents": [self.folder]},
                media_body=m, fields="id").execute(num_retries=5)
        # 若逾時但其實已上傳成功，會多一個同名檔；cleanup() 會刪掉較舊的重複檔
        f = _run(go, f"上傳 {name}")
        self.files[name] = f["id"]
        if old:
            self._purge(old)

    def _purge(self, file_id):
        try:
            self.d.files().delete(fileId=file_id).execute(num_retries=5)
        except Exception as e:   # 刪不掉不影響數據；下次 cleanup 會再清
            print(f"刪除舊檔失敗（{e}），下次清理時再刪", flush=True)


    def delete(self, name):
        """永久刪除這個程式建立的檔案（不進垃圾桶，避免佔用 Drive 空間）"""
        if name in self.files:
            self._purge(self.files.pop(name))

    def cleanup(self):
        """清掉佔空間的東西：本程式放進垃圾桶的檔案、同名重複的舊檔、檔案的舊版本。回傳摘要"""
        n_trash = n_dup = n_rev = 0
        freed = 0
        token = None
        while True:
            res = self.d.files().list(q=f"'{self.folder}' in parents and trashed=true",
                                      fields="nextPageToken, files(id,size)", pageSize=1000,
                                      pageToken=token).execute(num_retries=5)
            for f in res.get("files", []):
                self._purge(f["id"]); n_trash += 1; freed += int(f.get("size", 0))
            token = res.get("nextPageToken")
            if not token:
                break
        seen, token = {}, None
        while True:
            res = self.d.files().list(q=f"'{self.folder}' in parents and trashed=false",
                                      fields="nextPageToken, files(id,name,size)", orderBy="modifiedTime desc",
                                      pageSize=1000, pageToken=token).execute(num_retries=5)
            for f in res.get("files", []):
                if f["name"] in seen:           # 較舊的同名檔
                    self._purge(f["id"]); n_dup += 1; freed += int(f.get("size", 0))
                    continue
                seen[f["name"]] = f["id"]
                revs = self.d.revisions().list(fileId=f["id"], fields="revisions(id,size)").execute(
                    num_retries=5).get("revisions", [])
                for r in revs[:-1]:             # 最後一個是目前版本
                    try:
                        self.d.revisions().delete(fileId=f["id"], revisionId=r["id"]).execute(num_retries=5)
                        n_rev += 1; freed += int(r.get("size", 0))
                    except Exception as e:
                        print(f"刪除 {f['name']} 舊版本失敗：{e}", flush=True)
            token = res.get("nextPageToken")
            if not token:
                break
        self.files = self._list()
        return f"刪除垃圾桶 {n_trash} 個、重複舊檔 {n_dup} 個、舊版本 {n_rev} 個，釋放約 {freed / 1e9:.2f} GB"


class LocalStore:
    """本地測試用：把一個資料夾當作 Drive"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = {p.name: p for p in self.root.iterdir()}

    def exists(self, name):
        return (self.root / name).exists()

    def download(self, name, path):
        shutil.copy(self.root / name, path)

    def upload(self, path, name, mimetype=None):
        shutil.copy(path, self.root / name)
        self.files[name] = self.root / name

    def cleanup(self):
        return "本地測試：不用清理"

    def delete(self, name):
        if (self.root / name).exists():
            (self.root / name).unlink()
        self.files.pop(name, None)


def get_store():
    if os.environ.get("LOCAL_STORE"):
        return LocalStore(os.environ["LOCAL_STORE"])
    return DriveStore()

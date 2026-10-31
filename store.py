# -*- coding: utf-8 -*-
"""Google Drive 存取（用你本人的 OAuth 授權，只能存取本程式建立的檔案）"""
import os
import shutil
from pathlib import Path

FOLDER_NAME = os.environ.get("DRIVE_FOLDER", "market-data")
FOLDER_MIME = "application/vnd.google-apps.folder"


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
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"].strip(),
            client_id=os.environ["GDRIVE_CLIENT_ID"].strip(),
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"].strip(),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self.d = build("drive", "v3", credentials=creds, cache_discovery=False)
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
            res = self.d.files().list(
                q=f"'{self.folder}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,size)",
                pageSize=1000, pageToken=token).execute(num_retries=5)
            for f in res.get("files", []):
                out[f["name"]] = f["id"]
            token = res.get("nextPageToken")
            if not token:
                return out

    def exists(self, name):
        return name in self.files

    def download(self, name, path):
        from googleapiclient.http import MediaIoBaseDownload
        req = self.d.files().get_media(fileId=self.files[name])
        with open(path, "wb") as f:
            dl = MediaIoBaseDownload(f, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)

    def upload(self, path, name):
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(path), mimetype="application/gzip",
                                resumable=True, chunksize=32 * 1024 * 1024)
        if name in self.files:
            self.d.files().update(fileId=self.files[name],
                                  media_body=media).execute(num_retries=5)
        else:
            f = self.d.files().create(
                body={"name": name, "parents": [self.folder]},
                media_body=media, fields="id").execute(num_retries=5)
            self.files[name] = f["id"]


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

    def upload(self, path, name):
        shutil.copy(path, self.root / name)
        self.files[name] = self.root / name


def get_store():
    if os.environ.get("LOCAL_STORE"):
        return LocalStore(os.environ["LOCAL_STORE"])
    return DriveStore()

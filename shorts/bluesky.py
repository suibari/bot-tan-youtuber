#!/usr/bin/env python3
"""夜の動画を Bluesky に投稿する。

夜版は YouTube をやめて Bluesky に出す（YouTube AI に「いろいろ投稿しすぎ」と
言われ、YouTube は朝のクイズだけに絞った）。18:00 にこのモジュールが動画ポストを出し、
その夜のおやすみポスト（bsky-affirmative-bot）が RP とコメントで紹介する。
両者は DB の affirmative_bot.night_videos でつながる（night_videos.py）。

投稿は「投稿用 JSON」（pipeline.py が mp4 の隣に書く）だけを見て行う。
収録と投稿を分けておけば、SKIP_BLUESKY=true で撮って中身を確かめてから、

    ./venv/bin/python shorts/bluesky.py --from /tmp/bottan_XXXX_bluesky.json

で同じものを投稿できる。

動画は Bluesky の動画サービス（video.bsky.app）に上げる。PDS の uploadBlob に
直接上げるより確実に変換・配信される公式の経路。

認証は bot のアカウント（BSKY_DID か BLUESKY_HANDLE と、BSKY_APP_PASSWORD か
BLUESKY_PASSWORD）。DID を優先するのは bsky-affirmative-bot と同じ理由で、
ハンドルは変わりうるから。
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import env_flag  # noqa: E402  (.env もここで読まれる)

ENTRYWAY = "https://bsky.social"
PUBLIC_API = "https://public.api.bsky.app"
VIDEO_SERVICE = "https://video.bsky.app"
VIDEO_SERVICE_DID = "did:web:video.bsky.app"

POST_MAX_GRAPHEMES = 300
HASHTAGS = "#全肯定botたん #Bottan"
# 変換待ちの上限。30秒・16MB の動画で通常は1分以内に終わる
JOB_TIMEOUT_SEC = 600
JOB_POLL_SEC = 3
HTTP_TIMEOUT = 30
UPLOAD_TIMEOUT = 300

_URL_RE = re.compile(r"https?://\S+")


# ──────────────────────────────────────────────
# 文面
# ──────────────────────────────────────────────

def strip_links(text: str) -> str:
    """LLM の文から URL と @ を取り除く。

    添え文に載せてよい URL はシステムが付ける紹介元のリンクだけ（bsky-affirmative-bot の
    assertSourcedUrls と同じ考え方。モデルは「ありそうな」URL を作る）。
    @ はメンションとして通知が飛ぶ形を残さないため（sanitizeBotPostFacets と同じ方針）。
    """
    text = _URL_RE.sub("", text or "")
    text = text.replace("@", "").replace("＠", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def public_post_url(uri: str) -> str | None:
    """at:// URI を、ブラウザで開ける公開 URL にする（bsky 側 scheduledPostContent.ts と同じ規則）。"""
    m = re.match(r"^at://(did:(?:plc|web):[^/]+)/([^/]+)/([^/]+)$", uri or "")
    if not m:
        return None
    did, collection, rkey = m.groups()
    if collection == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{did}/post/{quote(rkey, safe='')}"
    if collection == "com.suibari.nagi.post":
        return f"https://nagi.suibari.com/thread/{quote(did, safe='')}/{quote(rkey, safe='')}"
    if collection == "site.standard.document":
        return f"https://nagi.suibari.com/blog/{quote(did, safe='')}/{quote(rkey, safe='')}"
    return None


def grapheme_len(text: str) -> int:
    """書記素の数の近似。結合文字・異体字セレクタ・ZWJ の続きを前の文字に含める。

    Bluesky の 300 制限は書記素で数える。len() だと絵文字の結合で多めに数えるだけなので
    安全側に倒れるが、こちらのほうが無駄に英訳をリプライへ追い出さない。
    """
    n, joined = 0, False
    for ch in text:
        if joined:
            joined = False
            continue
        if ch == "‍":
            joined = True
            continue
        if unicodedata.combining(ch) or 0xFE00 <= ord(ch) <= 0xFE0F or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue
        n += 1
    return n


def _with_link(body: str, url: str | None) -> tuple[str, list[dict]]:
    """本文の末尾に URL を置き、その範囲に link facet を付ける（バイトオフセット）。"""
    if not url:
        return body, []
    text = f"{body}\n{url}" if body else url
    start = len(text.encode("utf-8")) - len(url.encode("utf-8"))
    facet = {
        "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    }
    return text, [facet]


def build_post_texts(job: dict) -> list[tuple[str, list[dict]]]:
    """動画ポスト（と、収まらなければ英語のリプライ）の本文と facet。

    1本目: 日本語の添え文 + 英語の添え文 + ハッシュタグ + 紹介元のリンク。
    300 を超えるときは英語を2本目（リプライ）へ回す。
    """
    ja = strip_links(job.get("caption_ja") or job.get("hook_ja") or "")
    en = strip_links(job.get("caption_en") or "")
    url = (job.get("source") or {}).get("url")

    parts = [p for p in (ja, en, HASHTAGS) if p]
    first, facets = _with_link("\n\n".join(parts), url)
    if grapheme_len(first) <= POST_MAX_GRAPHEMES:
        return [(first, facets)]

    first, facets = _with_link("\n\n".join(p for p in (ja, HASHTAGS) if p), url)
    if grapheme_len(first) > POST_MAX_GRAPHEMES:
        # 日本語だけでも溢れるのは添え文が長すぎるとき。リンクを残して文を切る
        room = POST_MAX_GRAPHEMES - grapheme_len(f"\n\n{HASHTAGS}\n{url or ''}") - 1
        first, facets = _with_link("\n\n".join([ja[:room] + "…", HASHTAGS]), url)
    out = [(first, facets)]
    if en:
        out.append((en[:POST_MAX_GRAPHEMES], []))
    return out


# ──────────────────────────────────────────────
# XRPC
# ──────────────────────────────────────────────

def fetch_display_names(dids: list[str]) -> dict[str, str]:
    """Bluesky の DID → 表示名（無ければハンドル）。失敗したら空で返す（DID のまま使われる）。"""
    names: dict[str, str] = {}
    dids = list(dict.fromkeys(d for d in dids if d))
    for i in range(0, len(dids), 25):
        try:
            r = requests.get(f"{PUBLIC_API}/xrpc/app.bsky.actor.getProfiles",
                             params=[("actors", d) for d in dids[i:i + 25]], timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            for p in r.json().get("profiles", []):
                names[p["did"]] = (p.get("displayName") or "").strip() or p.get("handle") or p["did"]
        except Exception as e:
            print(f"[Bluesky] 表示名の取得に失敗しました（DID のまま続行）: {e}")
    return names


class Session:
    def __init__(self):
        identifier = os.getenv("BSKY_DID") or os.getenv("BLUESKY_HANDLE")
        password = os.getenv("BSKY_APP_PASSWORD") or os.getenv("BLUESKY_PASSWORD")
        if not identifier or not password:
            raise RuntimeError("Bluesky の認証情報がありません（BSKY_DID/BLUESKY_HANDLE と "
                               "BSKY_APP_PASSWORD/BLUESKY_PASSWORD）")
        r = requests.post(f"{ENTRYWAY}/xrpc/com.atproto.server.createSession",
                          json={"identifier": identifier, "password": password},
                          timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        self.did = data["did"]
        self.jwt = data["accessJwt"]
        self.pds = ENTRYWAY
        for svc in (data.get("didDoc") or {}).get("service", []):
            if svc.get("id", "").endswith("#atproto_pds"):
                self.pds = svc["serviceEndpoint"].rstrip("/")
        print(f"[Bluesky] ログイン: {self.did} (PDS {self.pds})")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.jwt}"}

    def get(self, nsid: str, **params) -> dict:
        r = requests.get(f"{self.pds}/xrpc/{nsid}", params=params, headers=self._headers(),
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def post(self, nsid: str, body: dict) -> dict:
        r = requests.post(f"{self.pds}/xrpc/{nsid}", json=body, headers=self._headers(),
                          timeout=HTTP_TIMEOUT)
        if not r.ok:
            raise RuntimeError(f"{nsid} が失敗しました: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def service_token(self, aud: str, lxm: str, ttl_sec: int = 1800) -> str:
        exp = int(time.time()) + ttl_sec
        return self.get("com.atproto.server.getServiceAuth", aud=aud, lxm=lxm, exp=exp)["token"]


def upload_video(sess: Session, mp4_path: str) -> dict:
    """動画サービスへ上げ、変換が終わった blob を返す。"""
    pds_host = urlparse(sess.pds).hostname
    upload_token = sess.service_token(f"did:web:{pds_host}", "com.atproto.repo.uploadBlob")
    size = Path(mp4_path).stat().st_size

    # 1日の上限（本数・容量）に掛かっていないか先に確かめる。掛かっていたら上げても弾かれる
    limits_token = sess.service_token(VIDEO_SERVICE_DID, "app.bsky.video.getUploadLimits")
    r = requests.get(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getUploadLimits",
                     headers={"Authorization": f"Bearer {limits_token}"}, timeout=HTTP_TIMEOUT)
    if r.ok and r.json().get("canUpload") is False:
        raise RuntimeError(f"Bluesky の動画アップロード上限に達しています: {r.json()}")

    name = f"bottan_night_{int(time.time())}.mp4"
    print(f"[Bluesky] 動画をアップロード中（{size / 1e6:.1f}MB）...")
    with open(mp4_path, "rb") as f:
        r = requests.post(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.uploadVideo",
                          params={"did": sess.did, "name": name},
                          headers={"Authorization": f"Bearer {upload_token}",
                                   "Content-Type": "video/mp4",
                                   "Content-Length": str(size)},
                          data=f, timeout=UPLOAD_TIMEOUT)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    # 同じ動画を上げ直すと 409 で既存のジョブが返る。そのジョブを待てばよい
    job = data.get("jobStatus") or data
    job_id = job.get("jobId")
    if not job_id:
        raise RuntimeError(f"uploadVideo が失敗しました: HTTP {r.status_code} {r.text[:300]}")

    deadline = time.time() + JOB_TIMEOUT_SEC
    while True:
        if job.get("blob"):
            print(f"[Bluesky] 変換完了: {job['blob']['ref']['$link']}")
            return job["blob"]
        if job.get("state") == "JOB_STATE_FAILED":
            raise RuntimeError(f"Bluesky の動画変換に失敗しました: {job.get('error') or job}")
        if time.time() > deadline:
            raise TimeoutError(f"Bluesky の動画変換が {JOB_TIMEOUT_SEC}秒で終わりませんでした: {job}")
        time.sleep(JOB_POLL_SEC)
        r = requests.get(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getJobStatus",
                         params={"jobId": job_id}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        job = r.json().get("jobStatus") or {}
        print(f"[Bluesky] 変換待ち: {job.get('state')} {job.get('progress', '')}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_video_post(sess: Session, job: dict, blob: dict) -> dict:
    """動画ポストを作る。返り値は1本目の {uri, cid}（おやすみポストが RP するのはこれ）。"""
    texts = build_post_texts(job)
    alt = (job.get("hook_ja") or "").strip()
    if job.get("hook_en"):
        alt = f"{alt} / {job['hook_en']}".strip(" /")
    langs = ["ja", "en"] if job.get("caption_en") else ["ja"]

    first_text, first_facets = texts[0]
    record = {
        "$type": "app.bsky.feed.post",
        "text": first_text,
        "createdAt": _now_iso(),
        "langs": langs,
        "embed": {
            "$type": "app.bsky.embed.video",
            "video": blob,
            "alt": f"全肯定botたんの動画。{alt}"[:1000],
            "aspectRatio": {"width": int(job.get("width") or 1080),
                            "height": int(job.get("height") or 1920)},
        },
    }
    if first_facets:
        record["facets"] = first_facets
    root = sess.post("com.atproto.repo.createRecord",
                     {"repo": sess.did, "collection": "app.bsky.feed.post", "record": record})
    root = {"uri": root["uri"], "cid": root["cid"]}

    parent = root
    for text, facets in texts[1:]:
        reply = {"$type": "app.bsky.feed.post", "text": text, "createdAt": _now_iso(),
                 "langs": ["en"], "reply": {"root": root, "parent": parent}}
        if facets:
            reply["facets"] = facets
        res = sess.post("com.atproto.repo.createRecord",
                        {"repo": sess.did, "collection": "app.bsky.feed.post", "record": reply})
        parent = {"uri": res["uri"], "cid": res["cid"]}
    return root


def post_from_job(job_path: str) -> dict:
    """投稿用 JSON の動画を投稿し、DB に記録する。

    同じ JSON を二度投稿しないよう、投稿できたら JSON に posted を書き戻す。
    """
    from common import notify
    import night_videos

    job = json.loads(Path(job_path).read_text())
    if job.get("posted"):
        print(f"[Bluesky] 投稿済みです: {job['posted']['uri']}")
        return job["posted"]
    if not Path(job["mp4"]).exists():
        raise FileNotFoundError(job["mp4"])

    sess = Session()
    blob = upload_video(sess, job["mp4"])
    root = create_video_post(sess, job, blob)
    url = public_post_url(root["uri"]) or root["uri"]
    print(f"[Bluesky] 投稿しました: {url}")

    job["posted"] = {**root, "url": url, "at": _now_iso()}
    Path(job_path).write_text(json.dumps(job, ensure_ascii=False, indent=2, default=str))

    night_videos.save(job, root["uri"], root["cid"])
    notify.bluesky_posted(url, job.get("caption_ja") or "")
    return job["posted"]


def main() -> int:
    ap = argparse.ArgumentParser(description="夜の動画を Bluesky に投稿する")
    ap.add_argument("--from", dest="job", required=True, help="pipeline.py が書いた投稿用 JSON")
    ap.add_argument("--dry-run", action="store_true", help="本文を表示するだけで投稿しない")
    args = ap.parse_args()
    if args.dry_run or env_flag("SKIP_BLUESKY"):
        job = json.loads(Path(args.job).read_text())
        for i, (text, facets) in enumerate(build_post_texts(job), 1):
            print(f"--- {i}本目（{grapheme_len(text)}字）---\n{text}\nfacets={facets}")
        return 0
    post_from_job(args.job)
    return 0


if __name__ == "__main__":
    sys.exit(main())

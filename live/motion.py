"""ARDY によるモーション生成と、その結果を貯めるプール。

ARDY はリアルタイム生成に向かない。サーバ起動に4〜5分、常駐15GB、生成は
GPUでも数秒（CPUなら数十秒）かかり、しかも GEN_LOCK で1本ずつしか処理しない。
コメントが来てから生成していては返事に間に合わないので二段構えにする:

  1. カテゴリ別のプールから即座に1本引いて再生する（レイテンシほぼゼロ）
  2. 並行して motion_en を ARDY へ投げ、できたら次の発話で使い、プールにも足す

プールはディスクに残すので、配信を重ねるほど手持ちが増えて即応の質が上がる。

ARDY が使えない環境（未起動・エンジン未マウント）でも配信は続く。
プールが空ならモーションを送らないだけで、Unity 側は Animator の Idle が出る。
"""

import hashlib
import json
import queue
import random
import threading
import time
from pathlib import Path

from common import ardy
from config import MOTION_POOL_DIR, WORK_DIR, env_float, env_int
from llm import MOTION_CATEGORIES
import safety

# ARDY サーバの起動・待機・停止・生成は common/ardy.py が原典（Shorts と共通）。
# 配信では空きメモリを待たず、既存サーバも必ず落として起動し直す
# （統合前の live/motion.py と同じ振る舞い）。
_FALLBACK = ardy.FALLBACK_MSG_LIVE


def health():
    return ardy.health()


def engine_available() -> bool:
    return ardy.available(_FALLBACK)


def start_server(log_dir=None):
    """ARDY サーバを起動する。起動できなければ None。

    モデル読み込みに4〜5分かかるので、配信の準備フェーズ（20:40）で先に呼ぶこと。
    """
    return ardy.start(mem_wait_sec=0, reuse=False, log_dir=log_dir,
                      fallback_msg=_FALLBACK)


def wait_ready(timeout: float = None) -> bool:
    return ardy.wait_ready(timeout, fallback_msg=_FALLBACK)


def stop_server(proc) -> None:
    ardy.stop(proc)

# 1セグメントの尺[秒]。ARDY は尺の後半で動きが止まる癖があるので短く切る
# （録画パイプラインは VRMA_SEG_TARGET_SEC=2.6 で細切れにしている）
GEN_DURATION = env_float("ARDY_LIVE_DURATION", 3.0)

# 1本の .vrma に何セグメント繋ぐか。録画パイプラインと同じく ARDY 側で
# blendSec のクロスフェードを掛けて1本にしてもらうので、継ぎ目は見えない。
#
# 3秒の単発クリップだと、発話中を途切れさせないために 2.5秒ごとに /motion を
# 投げ続けることになる。Unity は /motion を受けた瞬間にメインスレッドで
# .vrma をパースする（VrmaMotionPlayer.LoadVrma）ので、投げる回数がそのまま
# フレーム落ちの回数になる。3セグメント繋いで約9秒にすれば 8.5秒に1回で済む。
GEN_SEGMENTS = max(1, env_int("LIVE_MOTION_SEGMENTS", 3))

# サイドカー(.json)を持たない古いプール（1本3秒の単発生成）の尺。
# 尺が分からないクリップはこの値で扱う
LEGACY_CLIP_SEC = 3.0

# カテゴリごとの種モーション。プールを作るときに使う。
#
# 書き方は shorts/prompts.py の実測知見に従っている:
#   - 必ず "A woman stands in place" で始める
#   - 上体の動きは「…して、正面に戻る」の往復形だけが効く
#   - 下半身と拍手は使わない（safety.BANNED_MOTION_RE でも遮断される）
#   - 山場の動作は手が胸より上に来るようにする（腰の高さは画面外）
CATEGORY_MOTIONS = {
    "happy": [
        "A woman stands in place and raises both arms straight up above her head.",
        "A woman stands in place and opens both arms out to the sides at chest height.",
        "A woman stands in place and raises one hand straight above her head.",
    ],
    "surprised": [
        "A woman stands in place and brings both hands up to her cheeks.",
        "A woman stands in place and leans her upper body backward, then straightens up.",
        "A woman stands in place and brings one hand up to her mouth.",
    ],
    "thinking": [
        "A woman stands in place and brings one hand up to her chin.",
        "A woman stands in place and tilts her head slowly toward her right shoulder.",
        "A woman stands in place and raises one index finger beside her face.",
    ],
    "encouraging": [
        "A woman stands in place and clasps both hands together in front of her chest.",
        "A woman stands in place and pushes both fists forward at chest height.",
        "A woman stands in place and extends one hand forward at chest height.",
    ],
    "greeting": [
        "A woman stands in place and waves one hand gently beside her face.",
        "A woman stands in place and raises one hand up beside her head.",
        "A woman stands in place and nods her head down to her chest.",
    ],
    "neutral": [
        "A woman stands in place and turns her upper body to her right, then back to the front.",
        "A woman stands in place and leans her upper body to her left, then straightens up.",
        "A woman stands in place and repeatedly nods her head down and up.",
    ],
}


# ── ARDY サーバ ───────────────────────────────────────

def build_segments(text: str, category: str,
                   count: int = None, duration: float = None) -> list:
    """1本ぶんの segments を組む。

    先頭は LLM が返した motion_en（＝いま喋る内容に合った所作）。残りは
    そのカテゴリの種モーションから、先頭と重複しないものを順に足す。
    種が足りなければ持っているぶんだけで作る（1本＝単発生成と同じ）。
    """
    count = GEN_SEGMENTS if count is None else count
    duration = GEN_DURATION if duration is None else duration

    texts = [text]
    seeds = list(CATEGORY_MOTIONS.get(category) or safety.IDLE_MOTIONS)
    random.shuffle(seeds)
    for s in seeds:
        if len(texts) >= count:
            break
        if s not in texts:
            texts.append(s)
    return [{"text": t, "duration": duration} for t in texts]


def generate_vrma(text: str, out_vrma: Path, category: str = "neutral",
                  duration: float = GEN_DURATION, seed: int = None):
    """モーション指示から .vrma を1本作る。戻り値は尺[秒]、失敗なら None。

    text は safety.sanitize_motion を通したものを渡すこと。
    ここでも念のため通すが、呼び出し側で弾けるものは早く弾いたほうがよい。

    GEN_SEGMENTS が2以上なら、カテゴリの種モーションを足して1本に繋ぐ。
    タイムアウトは尺に比例して伸ばす（ARDY_GEN_TIMEOUT の既定300秒は
    3秒クリップ向けなので、連結でも足りるが念のため下限として使う）。
    """
    seed = random.randint(1, 2 ** 31 - 1) if seed is None else seed
    segments = build_segments(text, category, duration=duration)
    if len(segments) == 1:
        return ardy.generate_vrma(text=segments[0]["text"], out_vrma=out_vrma,
                                  duration=segments[0]["duration"], seed=seed,
                                  work_dir=WORK_DIR)
    total = sum(s["duration"] for s in segments)
    return ardy.generate_vrma(out_vrma=out_vrma, segments=segments, seed=seed,
                              blend_sec=ardy.ARDY_BLEND_SEC, work_dir=WORK_DIR,
                              timeout=max(ardy.ARDY_GEN_TIMEOUT, total * 20))


# ── プール ────────────────────────────────────────────

class MotionPool:
    """カテゴリ別の .vrma プール。ディスクに残して配信をまたいで使い回す。"""

    def __init__(self, root=None):
        self.root = Path(root or MOTION_POOL_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        for c in MOTION_CATEGORIES:
            (self.root / c).mkdir(exist_ok=True)
        # 直前に使ったものを覚えておき、同じ動きが続かないようにする
        self._last = {}

    def path_for(self, category: str, text: str) -> Path:
        """同じ指示文からは同じファイル名を作る（作り直しを避ける）。"""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return self.root / category / f"{digest}.vrma"

    @staticmethod
    def _meta_path(vrma_path) -> Path:
        return Path(vrma_path).with_suffix(".json")

    def write_duration(self, vrma_path, seconds: float) -> None:
        """クリップの尺をサイドカーに残す。

        次のモーションをいつ投げるかはこの尺で決めるので、生成した本人しか
        知らない値をディスクに残しておく必要がある（.vrma を読めば分かるが、
        そのために Python 側に glTF パーサを持ちたくない）。
        """
        try:
            self._meta_path(vrma_path).write_text(
                json.dumps({"duration": round(float(seconds), 3)}), encoding="utf-8")
        except OSError as e:
            print(f"[pool] 尺を保存できません（既定値で扱います）: {e}")

    def duration_of(self, vrma_path) -> float:
        """クリップの尺[秒]。サイドカーが無ければ LEGACY_CLIP_SEC。"""
        try:
            meta = json.loads(self._meta_path(vrma_path).read_text(encoding="utf-8"))
            return max(0.5, float(meta["duration"]))
        except Exception:
            return LEGACY_CLIP_SEC

    def list(self, category: str) -> list:
        d = self.root / category
        return sorted(d.glob("*.vrma")) if d.exists() else []

    def count(self, category: str) -> int:
        return len(self.list(category))

    def total(self) -> int:
        return sum(self.count(c) for c in MOTION_CATEGORIES)

    def pick(self, category: str) -> str:
        """カテゴリから1本引く。空なら neutral、それも空なら None。"""
        for c in (category, "neutral"):
            files = self.list(c)
            if not files:
                continue
            if len(files) > 1 and self._last.get(c) in files:
                files = [f for f in files if f != self._last[c]]
            chosen = random.choice(files)
            self._last[c] = chosen
            return str(chosen)
        return None

    def summary(self) -> str:
        return " ".join(f"{c}:{self.count(c)}" for c in MOTION_CATEGORIES)


# ── 非同期生成ワーカー ────────────────────────────────

class ArdyWorker:
    """ARDY への生成依頼を1本ずつ順に処理するワーカー。

    ARDY は GEN_LOCK で排他するので並列に投げても意味がない。
    キューが溜まったら古いものから捨てる（配信は待ってくれない）。
    """

    def __init__(self, pool: MotionPool, max_queue: int = 3):
        self.pool = pool
        self.enabled = False
        self._proc = None
        self._queue = queue.Queue(maxsize=max_queue)
        # プールを厚くするだけの依頼。ARDY が空いているときにしか処理しないので
        # 本数を絞る必要がなく、上限なしでよい。
        # 以前は prewarm も同じキューへ入れていたため、per_category=3（＝最大18件）が
        # maxsize=3 のキューを溢れさせ、15件ぶんの「古い依頼を捨てます」を出して
        # 実際には最後の3本しか作られていなかった
        self._prewarm = queue.Queue()
        self._done = queue.Queue()
        self._thread = None
        self._stop = threading.Event()

    def start(self, wait: bool = True) -> bool:
        """サーバを起動してワーカースレッドを回す。使えなければ False。"""
        self._proc = start_server()
        if self._proc is None and health() is None:
            return False
        if wait and not wait_ready():
            stop_server(self._proc)
            self._proc = None
            return False

        self.enabled = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="ardy")
        self._thread.start()
        return True

    def submit(self, text: str, category: str) -> bool:
        """生成を依頼する。キューが一杯なら古いものを捨てて入れ替える。"""
        if not self.enabled:
            return False
        text = safety.sanitize_motion(text)
        if not text:
            return False
        if self.pool.path_for(category, text).exists():
            return False        # 既に持っている

        item = (text, category)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                print(f"[ARDY] 生成が追いつかないので古い依頼を捨てます: {dropped[0][:50]}")
                self._queue.put_nowait(item)
                return True
            except (queue.Empty, queue.Full):
                return False

    def poll_ready(self) -> tuple:
        """生成が終わった (path, category) を1件取り出す。無ければ None。"""
        try:
            return self._done.get_nowait()
        except queue.Empty:
            return None

    def _next_request(self):
        """次に作るもの。発話由来の依頼を優先し、無ければプール補充を1件。"""
        try:
            return self._queue.get(timeout=1.0)
        except queue.Empty:
            pass
        try:
            return self._prewarm.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._next_request()
            if item is None:
                continue
            text, category = item
            out = self.pool.path_for(category, text)
            started = time.time()
            made = generate_vrma(text, out, category=category)
            if made:
                self.pool.write_duration(out, made)
                print(f"[ARDY] {category} に追加 ({time.time() - started:.1f}秒 / "
                      f"{made:.1f}秒のクリップ): {out.name}")
                self._done.put((str(out), category))

    def prewarm(self, per_category: int = 3) -> int:
        """配信前にプールを厚くする。ARDY の遊休時間を使う。

        既に十分持っているカテゴリは飛ばすので、日を重ねるほど早く終わる。
        カテゴリごとの種モーションから作るので、引いたときに内容と動きが合う。

        戻り値は実際に予約した本数。すでに持っているぶんは予約しないので、
        呼び出し側はこの数だけ poll_ready を待てばよい。
        """
        if not self.enabled:
            return 0
        queued = 0
        for category in MOTION_CATEGORIES:
            seeds = CATEGORY_MOTIONS.get(category) or safety.IDLE_MOTIONS
            shortfall = per_category - self.pool.count(category)
            for i in range(max(0, shortfall)):
                text = safety.sanitize_motion(seeds[i % len(seeds)])
                if not text or self.pool.path_for(category, text).exists():
                    continue
                self._prewarm.put((text, category))
                queued += 1
        if queued:
            print(f"[ARDY] プール補充を {queued} 件予約しました（空き時間に作ります）")
        return queued

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        stop_server(self._proc)
        self._proc = None
        self.enabled = False

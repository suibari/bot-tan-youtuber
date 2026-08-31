"""ARDY（text-to-vrma）エンジンのライフサイクルとモーション生成。

Shorts の収録とライブ配信で同じサーバーを使う。以前は shorts/core.py と
live/motion.py に別々の実装があり、片方にしか無い防御が複数あった
（`import ardy` の事前確認・空きメモリ待ち・`wait_ready` の即時撤退）。
ここでは強いほうを採り、弱いほうの振る舞いは引数で再現できるようにしてある。

  Shorts : start(mem_wait_sec=ARDY_MEM_WAIT_SEC, reuse=ARDY_REUSE)
  ライブ : start(mem_wait_sec=0, reuse=False)   ← 統合前と同じ「待たずに諦める」
"""

import os
import json
import shutil
import signal
import subprocess
import time
from pathlib import Path

import requests

from common.env import env_flag, LOGS_DIR
from common import motion_safety

# エンジン一式（venv 7.2GB + hf-cache 17GB）の置き場。**中身は SSD にあること。**
# ここは venv/bin/python と HF_HOME の親で、起動のたびに全部読み直される。
#
# 2026-08-30 に実体を SSD (/home/suibari/ardy-engine) へ移し、この既定値は
# そこへの symlink になっている。venv には絶対パスが焼き込まれているため
# （site-packages の __editable___ardy_0_2_0_finder.py が ardy/ を直接指す）、
# パスを変えずに symlink で差し替えるのが一番安全。
#
# 実測（1.5GB を direct I/O で読む）:
#   HDD  /mnt/data (WDC WD20EARX)  89 MB/s
#   SSD  /         (KIOXIA SATA)  442 MB/s
# ready までの時間は 175秒 → 101秒 になった。
ARDY_ENGINE_ROOT = os.getenv("ARDY_ENGINE_ROOT", "/mnt/data/ardy-engine")
# テキストエンコーダ(15GB)の置き場。ARDY_ENGINE_ROOT とは別に指定できる。
# HDD 上だと mmap のランダム読みで ready まで530秒以上かかり
# ARDY_READY_TIMEOUT に間に合わないため、ここだけ先に SSD へ移してあった
# （エンジン一式が SSD へ移った今も、別指定できる状態は残しておく）
ARDY_MERGED_BASE = os.getenv("ARDY_MERGED_BASE",
                             str(Path(ARDY_ENGINE_ROOT) / "llm2vec-base-merged"))
ARDY_REPO = os.getenv("ARDY_REPO", "/home/suibari/work/text-to-vrma")
ARDY_PORT = int(os.getenv("ARDY_PORT", "2337"))
ARDY_URL = f"http://127.0.0.1:{ARDY_PORT}"

# true にすると既にポートで動いているサーバーをそのまま使う（開発時用）。
# 既定は false で、古いサーバーは落として起動し直す
ARDY_REUSE = env_flag("ARDY_REUSE")
ARDY_READY_TIMEOUT = float(os.getenv("ARDY_READY_TIMEOUT", "600"))
# 3秒のモーションで実測3〜4秒。ただしGPUが混んでいると20秒、稀に140秒まで伸びる。
# さらに長時間アイドルだったサーバーは最初のリクエストで固まることがある
# （GPU使用率0%のまま返らない）ため、待ち続けずに諦めて動画を優先する。
ARDY_GEN_TIMEOUT = float(os.getenv("ARDY_GEN_TIMEOUT", "300"))

# ARDYサーバーは RSS 約15GB を使う（CPU側に載せる8Bエンコーダが大半）。
# 空きがこれを下回るとスワップスラッシングを起こし、生成がGPU使用率0%のまま返らなくなる。
# 実測: ollama の llama-server が 9.2GB 常駐していた状態で 300秒タイムアウトした。
# swap 2GB のときは 18GB 必要だったが、17GB に増設したため 13GB まで下げている
ARDY_MIN_AVAIL_GB = float(os.getenv("ARDY_MIN_AVAIL_GB", "13"))

# classifier-free guidance（1.0〜6.0）。テキスト追従の強さ。
# 実測: 3.0→4.5→6.0 と上げても腕の振れ幅は 1.8→1.4→1.0度 とむしろ減った。
# 「動かないプロンプト」を追従で救うことはできないので server.py の既定3.0のまま使う。
ARDY_CFG = float(os.getenv("ARDY_CFG", "3.0"))
# 腕の開き具合[度]（0〜20）。モーションではなく静的なオフセットで、
# 実測で 6→12→18 が腕の角度 70→64→58度（体側から離れる方向）に対応した。
# 12 だと脇が開いて男性的に見えるという指摘があったので 8 に下げた（2026-08-15）。
# 0 にはしないこと。リアル体型のモーキャプをアニメ体型に当てる都合で、
# 開きが足りないと腕（袖）が胴にめり込む（retarget.py の ARM_SPREAD_SIGN 参照）
ARDY_ARM_SPREAD = float(os.getenv("ARDY_ARM_SPREAD", "8"))

# セグメントのつなぎ目のクロスフェード長[秒]。server.py の既定は6フレーム(20fps=0.3秒)で、
# 独立生成された別ポーズ同士を繋ぐには短く、「スッと切り替わった」ように見えていた。
# server.py 側は smoothstep で混ぜるので窓の両端で速度が0になる。
# 未パッチのサーバーはこのフィールドを無視するだけなので送っても壊れない。
# Shorts の長尺ブロックと、配信の1本ぶん（数セグメント連結）で同じ値を使う
ARDY_BLEND_SEC = float(os.getenv("ARDY_BLEND_SEC", "0.7"))

# 空きメモリが足りないとき、ここまで待つ[秒]。
# ollama は既定5分のkeep_aliveでモデルを自動解放するので、待てば空くことが多い
ARDY_MEM_WAIT_SEC = float(os.getenv("ARDY_MEM_WAIT_SEC", "360"))
# true にすると生成前に ollama のモデルをアンロードさせる。
# ollama は次のリクエストで自動的に読み直すので停止はしないが、
# そちらのサービスの次回応答が数秒遅くなる。既定は無効（他サービスに触らない）
ARDY_FREE_OLLAMA = env_flag("ARDY_FREE_OLLAMA")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# エンジンが使えないときに何が起きるかは呼び出し側で違うので、文面を差し替えられるようにする
FALLBACK_MSG_SHORTS = "生成モーションはスキップします"
FALLBACK_MSG_LIVE = "プールのモーションだけで配信します"


def url(path: str) -> str:
    return f"{ARDY_URL}{path}"


# ── メモリ ────────────────────────────────────────────

def mem_available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return float("inf")   # 読めないなら判定しない


def _free_ollama() -> None:
    """ollama に読み込み済みモデルを解放させる（keep_alive=0）。失敗しても無視する。"""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        for m in r.json().get("models", []):
            name = m.get("name")
            if not name:
                continue
            requests.post(f"{OLLAMA_URL}/api/generate",
                          json={"model": name, "keep_alive": 0}, timeout=30)
            print(f"[ARDY] ollama のモデルを解放しました: {name} "
                  f"({m.get('size', 0) / 1e9:.1f}GB) — 次のリクエストで自動的に読み直されます")
    except Exception as e:
        print(f"[ARDY] ollama の解放をスキップ（無視）: {e}")


def wait_memory(timeout: float = None, fallback_msg: str = FALLBACK_MSG_SHORTS) -> bool:
    """空きメモリが閾値を超えるまで待つ。超えたら True。

    timeout=0 を渡すと待たずに1回だけ判定する（統合前のライブ配信の振る舞い）。
    """
    if ARDY_FREE_OLLAMA:
        _free_ollama()

    limit = ARDY_MEM_WAIT_SEC if timeout is None else timeout
    deadline = time.time() + limit
    warned = False
    while True:
        avail = mem_available_gb()
        if avail >= ARDY_MIN_AVAIL_GB:
            return True
        if time.time() >= deadline:
            print(f"[ARDY] 空きメモリが足りません ({avail:.1f}GB < {ARDY_MIN_AVAIL_GB}GB)。"
                  f"{fallback_msg}")
            return False
        if not warned:
            print(f"[ARDY] 空きメモリ待ち ({avail:.1f}GB < {ARDY_MIN_AVAIL_GB}GB, 最大{limit:.0f}秒)")
            warned = True
        time.sleep(15)


# ── エンジンの有無 ────────────────────────────────────

_available_cache: bool | None = None


def available(fallback_msg: str = FALLBACK_MSG_SHORTS) -> bool:
    """エンジン一式が揃っているか。別ドライブ未マウント時などに False になる。

    中の `import ardy` は venv を読むので、冷えていると数秒かかる。
    結果をキャッシュして1回で済ませる。
    """
    global _available_cache
    if _available_cache is None:
        _available_cache = _check_available(fallback_msg)
    return _available_cache


def _check_available(fallback_msg: str) -> bool:
    root = Path(ARDY_ENGINE_ROOT)
    # ARDY_MERGED_BASE は別ドライブを指しうるので、どれが欠けたか名指しする
    missing = [str(p) for p in (root / "venv/bin/python",
                                Path(ARDY_MERGED_BASE) if ARDY_MERGED_BASE else Path("/nonexistent"),
                                Path(ARDY_REPO) / "tools/ardy-engine/server.py",
                                Path(ARDY_REPO) / "tools/spec2vrma.mjs")
               if not p.exists()]
    if missing:
        print(f"[ARDY] エンジンが見つかりません（{', '.join(missing)}）。{fallback_msg}")
        return False

    # ファイルが揃っていても、venv の editable install が旧パスを指していると
    # import だけが落ちる（エンジンを別ドライブへ移設したときに実際に起きた）。
    # サーバーはモデル読み込みに4〜5分かけてから /health で error を返すので、
    # 先に import だけ試して即座に切り分ける。
    try:
        r = subprocess.run([str(root / "venv/bin/python"), "-c", "import ardy"],
                           capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[ARDY] エンジンのimport確認ができませんでした: {e}。{fallback_msg}")
        return False
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        print(f"[ARDY] エンジンのimportに失敗しました: {lines[-1] if lines else '原因不明'} "
              f"（{root}/venv に `pip install -e {root}/ardy` で貼り直してください）。{fallback_msg}")
        return False
    return True


# ── サーバーの起動と停止 ──────────────────────────────

def health() -> dict | None:
    try:
        return requests.get(url("/health"), timeout=5).json()
    except Exception:
        return None


def kill_stray_server() -> None:
    """ポートを掴んでいる ARDY サーバーを落とす。自分が起動したものでなくても止める。

    長く生きたサーバーは生成が返らなくなることがあり（GPU使用率0%のままタイムアウト）、
    それを掴むとモーションが丸ごと出なくなる。
    """
    marker = f"{ARDY_ENGINE_ROOT}/venv/bin/python"
    try:
        out = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
    except Exception:
        return
    for line in out.splitlines():
        if "server.py" in line and ("--port" in line) and (marker in line or "ardy-engine" in line):
            pid = line.split(None, 1)[0]
            if not pid.isdigit():
                continue
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"[ARDY] 既存サーバー PID={pid} を停止しました")
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    # ポートが解放されるまで少し待つ
    for _ in range(10):
        if health() is None:
            return
        time.sleep(1)


def start(mem_wait_sec: float = None, reuse: bool = None, log_dir=None,
          fallback_msg: str = FALLBACK_MSG_SHORTS, low_priority: bool = False):
    """ARDYサーバーを起動する。

    既に起動済みで reuse=True ならそれを再利用し None を返す
    （そのサーバーは stop() で落とさない）。エンジンが無い場合も None を返す。
    起動できたかどうかは wait_ready() で判定すること。

    low_priority=True にすると ionice/nice を噛ませて優先度を下げる。読み込みの
    完了を待たずに裏で走らせる録画側で使う。配信側は使わない（配信前に読み込みの
    完了を待つので発話とは競合せず、下げると配信の開始が遅れるだけ）。
    """
    if not available(fallback_msg):
        return None

    # サーバーは約15GB必要。足りないまま起動すると読み込み自体がスワップで
    # 10分以上かかる（実測: 600秒待っても準備完了にならず）ので、空くまで待つ
    if not wait_memory(mem_wait_sec, fallback_msg):
        return None

    h = health()
    if h is not None:
        if ARDY_REUSE if reuse is None else reuse:
            print(f"[ARDY] 既存のサーバーを再利用します (status={h.get('status')})。"
                  f"このサーバーは終了時に停止しません")
            return None
        # 既定では再利用しない。長く生きたサーバーは生成が返らなくなることがあり
        # （GPU使用率0%のままタイムアウト）、それを掴むと丸ごと生成を落とすため、
        # 落として自分で起動し直す。ポートはこの用途専用とみなす
        print("[ARDY] 既存のサーバーを停止して起動し直します "
              "（古いサーバーは生成が返らないことがあるため。ARDY_REUSE=true で再利用可）")
        kill_stray_server()

    root = Path(ARDY_ENGINE_ROOT)
    env = os.environ.copy()
    # これが無いと 8B のテキストエンコーダが GPU に載って CUDA OOM になる。
    # Electron版も同じ値を渡している (electron/ardy-client.cjs)
    env["TEXT_ENCODER_DEVICE"] = "cpu"
    env["HF_HOME"] = str(root / "hf-cache")

    cmd = [str(root / "venv/bin/python"),
           str(Path(ARDY_REPO) / "tools/ardy-engine/server.py"),
           "--port", str(ARDY_PORT),
           "--merged-base", ARDY_MERGED_BASE]
    if low_priority:
        # 読み込みは数分ぶんの CPU と I/O を食い切る。CPU 版 VOICEVOX と重なると
        # 合成が数十秒に伸びて録画が落ちる（2026-08-31 の朝版はこれで全滅した。
        # 実測で iowait 21%、読み込み 43MB/s）。裏で走らせるあいだは譲る。
        # 効くのは主に I/O のほうなので ionice が本命。無い環境では黙って諦める
        prefix = []
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        if shutil.which("nice"):
            prefix += ["nice", "-n", "10"]
        cmd = prefix + cmd
    print(f"[ARDY] サーバー起動: {' '.join(cmd)}")
    # 出力を捨てると起動に失敗したとき /health の error 文字列しか手掛かりが無くなる。
    # トレースバックを残す（プロセス終了時にOSが閉じるのでfpは持ち回らない）
    log_dir = Path(log_dir or LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ardy_{time.strftime('%Y%m%d_%H%M%S')}.log"
    print(f"[ARDY] サーバーログ: {log_path}")
    return subprocess.Popen(cmd, env=env,
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
                            preexec_fn=os.setsid)


def wait_ready(timeout: float = None, fallback_msg: str = FALLBACK_MSG_SHORTS) -> bool:
    """GET /health が status=ok になるまで待つ。モデル読み込みに4〜5分かかる。"""
    # サーバーも立っておらずエンジンも無いなら、待っても上がらない。
    # ここで即抜けないと別ドライブ未マウント時に毎回10分止まる
    if health() is None and (not available(fallback_msg)
                             or mem_available_gb() < ARDY_MIN_AVAIL_GB):
        return False

    limit = ARDY_READY_TIMEOUT if timeout is None else timeout
    deadline = time.time() + limit
    last = None
    while time.time() < deadline:
        h = health()
        if h is not None:
            status = h.get("status")
            if status == "ok":
                print(f"[ARDY] 準備完了 (model={h.get('model')} device={h.get('device')})")
                return True
            if status == "error":
                print(f"[ARDY] 起動に失敗しました: {h.get('error')}")
                return False
            cur = (h.get("stage"), round(h.get("progress") or 0, 2))
            if cur != last:
                print(f"[ARDY] 読み込み中... stage={cur[0]} progress={cur[1]}")
                last = cur
        time.sleep(5)
    print(f"[ARDY] 準備完了になりませんでした（{limit:.0f}秒待機）")
    return False


def stop(proc) -> None:
    """start() が起動したサーバーを落とす。None（再利用時）なら何もしない。"""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            pass
    except Exception as e:
        print(f"[ARDY] 停止時のエラー（無視）: {e}")
    print("[ARDY] サーバーを停止しました")


# ── 生成 ──────────────────────────────────────────────

def generate_spec(out_json, text: str = None, duration: float = None,
                  segments: list = None, seed: int = 0,
                  blend_sec: float = None, timeout: float = None):
    """/generate を叩いて spec JSON をファイルに落とす。

    単発（text + duration）と連結（segments）の2形態がある。連結は ARDY 側が
    各セグメントを履歴なしで独立生成し、終端の位置・向きに次を整列して
    blend_sec 秒でクロスフェードする（server.py の _generate_stitched）。
    履歴を引き継ぐ方式と違って前の動きの慣性に負けないので、単発生成と同じ
    テキスト追従度のままつなぎ目の無い長いモーションが得られる。

    戻り値: 生成された長さ[秒]。失敗時は None。
    """
    payload = {"seed": int(seed), "cfg": ARDY_CFG, "armSpread": ARDY_ARM_SPREAD}
    if segments is not None:
        payload["segments"] = [{"text": s["text"], "duration": float(s["duration"])}
                               for s in segments]
        if blend_sec is not None:
            payload["blendSec"] = blend_sec
        fallback_total = sum(float(s["duration"]) for s in segments)
    else:
        payload["text"] = text
        payload["duration"] = float(duration)
        fallback_total = float(duration)

    try:
        r = requests.post(url("/generate"), json=payload,
                          timeout=ARDY_GEN_TIMEOUT if timeout is None else timeout)
        spec = r.json()
    except Exception as e:
        print(f"[ARDY] 生成に失敗: {e}")
        return None

    if "tracks" not in spec:
        print(f"[ARDY] 生成結果が不正です: {str(spec)[:200]}")
        return None

    Path(out_json).write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    if segments is not None:
        print(f"[ARDY] 生成: {spec.get('duration')}秒 / {len(segments)}セグメント / "
              f"{len(spec['tracks'])}ボーン / seed={seed} cfg={ARDY_CFG} "
              f"armSpread={ARDY_ARM_SPREAD} blendSec={blend_sec}")
    else:
        print(f"[ARDY] 生成: {spec.get('duration')}秒 / {len(spec['tracks'])}ボーン / "
              f"seed={seed} cfg={ARDY_CFG} armSpread={ARDY_ARM_SPREAD}")
    return float(spec.get("duration") or fallback_total)


def to_vrma(spec_json, out_vrma) -> bool:
    """spec JSON を .vrma (GLB) に変換する。three.js しか使わない純JSなのでNodeだけで動く。"""
    try:
        subprocess.run(
            ["node", str(Path(ARDY_REPO) / "tools/spec2vrma.mjs"),
             str(spec_json), str(out_vrma)],
            check=True, capture_output=True, timeout=120)
        return True
    except Exception as e:
        detail = getattr(e, "stderr", b"") or b""
        print(f"[ARDY] .vrma 変換に失敗: {e} {detail[:200]}")
        return False


def generate_vrma(text: str = None, out_vrma=None, duration: float = None,
                  seed: int = 0, work_dir=None,
                  segments: list = None, blend_sec: float = None,
                  timeout: float = None):
    """モーション指示から .vrma を1本作る（生成 → 変換）。

    単発（text + duration）と連結（segments）の2形態がある。連結は ARDY 側が
    各セグメントを独立生成して blend_sec 秒でクロスフェードするので、
    1本のクリップの中に継ぎ目のない複数の所作を入れられる。

    text / segments[*]["text"] は motion_safety.sanitize_motion を通したものを
    渡すこと。ここでも念のため通すが、呼び出し側で弾けるものは早く弾くほうがよい。

    戻り値: 生成された長さ[秒]。失敗時は None（真偽値としても使える）。
    """
    if segments:
        clean = []
        for s in segments:
            t = motion_safety.sanitize_motion(s["text"])
            if t:
                clean.append({"text": t, "duration": float(s["duration"])})
        segments = clean
        if not segments:
            return None
    else:
        text = motion_safety.sanitize_motion(text)
        if not text:
            return None

    out_vrma = Path(out_vrma)
    out_vrma.parent.mkdir(parents=True, exist_ok=True)
    spec_path = Path(work_dir or out_vrma.parent) / f"ardy_spec_{seed}.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        made = generate_spec(spec_path, text=text, duration=duration,
                             segments=segments, seed=seed,
                             blend_sec=blend_sec, timeout=timeout)
        if made is None:
            return None
        if not to_vrma(spec_path, out_vrma):
            return None
    finally:
        spec_path.unlink(missing_ok=True)

    print(f"[ARDY] 生成: {out_vrma.name} ({made:.1f}秒, seed={seed})")
    return made

"""Unity 常駐（-liveMode）の疎通確認。

計画の検証項目「Unity 単体」を自動化したもの。
  - LiveController が応答するか
  - 連続 /speak がキューイングされるか
  - /motion /emotion が通るか
  - 無発話時も落ちないか
"""

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # common/
sys.path.insert(0, str(_ROOT / "live"))  # config, motion, unity_client ...

import unity_client
import unity_live
import voice
from config import WORK_DIR, ensure_dirs

# ARDY が生成した .vrma を優先して使う。プールが空なら Unity 同梱の待機モーション。
# 生成物の再生まで通しで見ないと、spec2vrma の出力を Unity が読めるかが確認できない
def _pick_vrma() -> str:
    from motion import MotionPool
    return (MotionPool().pick("happy")
            or "/home/suibari/bottan-video-dev/Assets/idle_loop.vrma")


VRMA = _pick_vrma()


def main() -> int:
    ensure_dirs()
    u = unity_live.UnityLive(log_path=WORK_DIR / "unity_test.log")
    failures = []

    try:
        st = u.start(ready_timeout=420)
        print(f"[TEST] 起動OK: {st}")
        print(f"[TEST] 使用する .vrma: {VRMA}")

        print("[TEST] 音声を合成します")
        lines = [
            {"ja": "こんばんは、botたんだよ。", "en": "Good evening, this is bot-tan."},
            {"ja": "いま配信のテストをしてるところなんだ。", "en": "I'm testing the live stream right now."},
        ]
        wav1, durs1 = voice.synthesize_lines(lines, WORK_DIR, "test1")
        wav2, durs2 = voice.synthesize_lines(
            [{"ja": "ちゃんと聞こえてるかな。", "en": "Can you hear me okay?"}],
            WORK_DIR, "test2")
        print(f"[TEST] 合成OK: {wav1} 尺={durs1} / {wav2} 尺={durs2}")

        # 2本続けて積んで、キューイングされることを確かめる。
        # WAV のロードは UnityWebRequest 経由の非同期なので、投入直後は
        # まだ speaking にならない。キューに乗ったことだけを先に見る
        unity_client.speak(wav1, 0.8, 0.4)
        unity_client.speak(wav2, -0.3, 0.1)
        time.sleep(0.6)
        st = unity_client.status()
        print(f"[TEST] 2本投入後: {st}")
        if not st.get("speaking") and st.get("speak_queue", 0) == 0:
            failures.append("2本投入したのに再生もキューイングもされていない")

        # 時計が実時間に追随しているか。ここがズレると音声とモーションが合わない
        t0_wall, t0_uptime = time.monotonic(), unity_client.status()["uptime"]

        unity_client.motion(VRMA)
        unity_client.emotion(-0.5, 0.2)
        print("[TEST] /motion /emotion 送信OK")

        total = sum(durs1) + sum(durs2)
        print(f"[TEST] 合計尺 {total:.1f}s ぶん再生を追跡します")
        saw_two_queued = False
        for i in range(int(total) + 15):
            time.sleep(1)
            st = unity_client.status()
            if st.get("speak_queue", 0) >= 1:
                saw_two_queued = True
            print(f"  {i:3d}s speaking={st.get('speaking')} "
                  f"queue={st.get('speak_queue')} motions={st.get('motion_queue')} "
                  f"uptime={st.get('uptime'):.1f} fps={st.get('fps', 0):.1f} "
                  f"rss={u.rss_mb():.0f}MB")
            if not st.get("speaking") and st.get("speak_queue") == 0 and i > total:
                break

        if not saw_two_queued:
            failures.append("2本目がキューに乗った形跡がない")

        # 時計の追随を判定する。実時間の 95% 未満しか進んでいなければ、
        # モーションが音声より遅れて再生される（realtimeSinceStartup に直す前は 70% だった）
        wall = time.monotonic() - t0_wall
        drift = unity_client.status()["uptime"] - t0_uptime
        ratio = drift / wall if wall > 0 else 0.0
        print(f"[TEST] 時計の追随: 実時間{wall:.1f}s に対して uptime {drift:.1f}s "
              f"({ratio*100:.1f}%)")
        if ratio < 0.95:
            failures.append(f"時計が実時間に追随していない（{ratio*100:.1f}%）")

        # 配信できるフレームレートが出ているか。Xvfb（llvmpipe）だと 1.7fps しか
        # 出ず紙芝居になる。GPU 仮想ディスプレイなら 30fps 以上出るはず
        fps = unity_client.status().get("fps", 0)
        print(f"[TEST] フレームレート: {fps:.1f} fps")
        if fps < 24:
            failures.append(
                f"フレームレートが足りない（{fps:.1f} fps）。"
                f"DISPLAY が GPU 仮想ディスプレイになっているか確認すること")

        # 発話が終わったあとも生きているか（配信では黙っている時間のほうが長い）
        time.sleep(5)
        st = unity_client.status()
        print(f"[TEST] 無発話5秒後: {st}")
        if st.get("speaking"):
            failures.append("発話が終わっていない")

        unity_client.motion(VRMA)
        time.sleep(2)
        st = unity_client.status()
        print(f"[TEST] 無発話中の /motion 後: {st}")
        if st.get("motion_queue", 0) < 1:
            failures.append("無発話中に投げたモーションが乗っていない")

    except Exception as e:
        failures.append(f"例外: {e}")
        import traceback
        traceback.print_exc()
    finally:
        u.stop()

    print()
    if failures:
        print("=== 失敗 ===")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("=== すべて成功 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

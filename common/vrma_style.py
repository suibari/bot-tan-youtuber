"""生成モーション(.vrma)の再生時の見た目を決める調整値と Unity 引数。

VrmaMotionPlayer.cs は引数を渡さなければ「改修前の見た目」に戻る既定値を持って
いるので、ここを渡さない＝素の ARDY 出力がそのまま出る。実際、統合前の配信は
`live/unity_live.py` がこれらを1つも渡しておらず、Unity のログが

    [Vrma] keepIdleHands=False elbowBend=0deg wristBend=0deg headTilt=0deg smooth=0s

になっていた。録画パイプラインだけがスムーズに見えた原因のひとつがこれ
（`-vrmaSmooth 0.10` は実測でカクつき -64%）。Shorts と配信で同じ値を使うため、
shorts/core.py から common/ へ移してある。

配信だけ見え方を変えたいときは `LIVE_VRMA_SMOOTH` のように `LIVE_` を頭に付けた
環境変数を置くと、`vrma_unity_args(prefix="LIVE_")` がそちらを優先する。
`.env` はリポジトリに1本だけなので、この上書きが無いと片方の都合でもう片方の
見た目まで動いてしまう。
"""

import os

from common.env import env_float


def _env_float(name: str, default: float, prefix: str = "") -> float:
    """`<prefix><name>` があればそれを、無ければ `<name>` を読む。"""
    if prefix and os.getenv(prefix + name, "").strip():
        return env_float(prefix + name, default)
    return env_float(name, default)


# 分割された .vrma を重ねて配置する量[秒]。
# VrmaMotionPlayer.FadeDuration と必ず一致させること。ずれると継ぎ目で
# 生成モーションが Idle に引き戻され、棒立ちが一瞬挟まる。
# 録画側はチャンクの配置時刻の計算に、配信側は次のモーションを投げる間隔に使う。
VRMA_CHUNK_OVERLAP = 0.5

# 生成モーションの振幅ゲイン。Unity側で Idleポーズからの偏差を増幅する倍率（腕のみ）。
# 既定は 1.0 = 無効。実測で 1.2 も 1.35 も、ARDYが出す「手を頭の近くに上げる」動きを
# 引き伸ばして腕(袖)が顔を覆ってしまい、倍率を下げても改善しなかった。
# 動きを大きくするのはプロンプト側（ジャンプ・大振りの許可）の役目で、
# 生成済みポーズを事後に引き伸ばすこの経路は割に合わない。
# 仕組みは残してあるので、試すときは VRMA_GAIN=1.2 のように環境変数で上書きする
VRMA_GAIN     = env_float("VRMA_GAIN", 1.0)
# 生成モーションの腰の上下移動を反映する倍率。既定0＝無効。
# ジャンプを画に出すための仕組みだが、そのジャンプ自体を VRMA_BANNED_RE で禁止した
# （スカートなので、ARDYが作る予備動作の深いしゃがみで下着が映る）。
# 跳ぶ動作が無い以上ここを有効にする意味がないので0にしてある。
# 衣装が変わって跳べるようになったら VRMA_HIPS_Y=1.0 で復活できる
VRMA_HIPS_Y   = env_float("VRMA_HIPS_Y", 0.0)

# 生成モーションの「体の向き・傾き」をどこまで画に出すか。すべて Unity 側の引数。
#
# ARDY は体のワールド回転（向きも傾きも）を出していて .vrma にもそのまま入っているが、
# VrmaMotionPlayer が既定で全部捨てていた（顔のアップで横を向くと困るため）。
# 捨てるのをやめて、代わりに上限をかけて通す。
#
# VRMA_YAW_LIMIT: 上体をどこまで横に向けてよいか[度]。Idle からの絶対角で切る。
#   ARDY のセグメント連結は前セグメントの終端ヨーに合わせて回転を積み上げるので、
#   相対量で制限すると一度横を向いたまま戻らなくなる。絶対角なら構造的に起きない。
# VRMA_HEAD_COUNTER: 上体が向いたぶんを首で逆に回して顔をカメラに残す割合。
#   1.0 で顔が完全に正面。体は斜め・顔はこちら＝「肩越しに振り返る」画になる。
# VRMA_HEAD_YAW: クリップ由来の首の横振りを通す上限[度]。
#   従来は首のヨーも殺していた。ただし実測では ARDY に「首を横に振れ」と書いても
#   対照より小さい振幅しか出ないので、意図した首振りのためではなく自然さのぶんだけ通す。
# VRMA_BODY_TILT: 腰の傾き(前後左右)の反映倍率。VRMA_GAIN は腕にしか効かないので、
#   上体の傾きの大きさを戻せるのはここだけ。
VRMA_BODY_TILT    = env_float("VRMA_BODY_TILT", 1.0)
VRMA_YAW_LIMIT    = env_float("VRMA_YAW_LIMIT", 35.0)
VRMA_HEAD_YAW     = env_float("VRMA_HEAD_YAW", 15.0)
VRMA_HEAD_COUNTER = env_float("VRMA_HEAD_COUNTER", 0.8)

# ── 女の子らしい所作にするための調整（2026-08-15）
#
# VRMA_KEEP_IDLE_HANDS: 指・親指・つま先をクリップで上書きせず、VRMモデルの
#   Idle ポーズを残す。1=有効。
#   .vrma の骨格には親指とつま先のボーンが無く（vrmaBuilder.js の SKELETON）、
#   人差し指〜小指も ARDY が出力しないので固定カール(14/17/10度)が焼き込まれている
#   だけ。Unity は全95 muscle を無条件に混ぜていたので、生成モーション区間
#   （＝ほぼ全編）でモデル本来の手のポーズが汎用の固定ポーズに置き換わり、
#   親指は Unity 既定(=0)で伸びたまま固定されていた。
#
# VRMA_ELBOW_BEND / VRMA_WRIST_BEND / VRMA_HEAD_TILT: 常時かける姿勢のバイアス[度]。
#   肘がピンと伸びた腕・真っ直ぐな手首・傾かない首は男性的に見える。
#   ARDY 側にこれを指示する手段が無い（プロンプトに書いても動かない）ので、
#   再生側で足す。符号付きなので、向きが逆なら負値を入れる。
#
# VRMA_SMOOTH: クリップのポーズを時間方向に平滑化する時定数[秒]（一次ローパス）。
#   角ばった・キビキビしすぎる動きの角を丸める。
#   実測（0.10秒・フレーム間差分）: 動き量 1.659→1.626（-2%）に対して
#   二階差分＝カクつきは 0.733→0.265（-64%）。**振幅はほぼ落ちない。**
#   代償は位相の遅れで、0.10秒 のとき約0.13秒ぶんモーションが後ろにずれる
#   （遅れは時定数に比例する）。話す内容との同期が気になるなら下げること。
VRMA_KEEP_IDLE_HANDS = int(env_float("VRMA_KEEP_IDLE_HANDS", 1))
VRMA_ELBOW_BEND      = env_float("VRMA_ELBOW_BEND", 8.0)
VRMA_WRIST_BEND      = env_float("VRMA_WRIST_BEND", 6.0)
VRMA_HEAD_TILT       = env_float("VRMA_HEAD_TILT", 4.0)
VRMA_SMOOTH          = env_float("VRMA_SMOOTH", 0.10)


def vrma_unity_args(prefix: str = "") -> list[str]:
    """生成モーションの見た目を決める Unity 引数をまとめて作る。

    Shorts（朝版・夜版）と配信で同じ値を使うので1か所にまとめてある（別々に書くと
    片方だけ直して食い違う）。VrmaMotionPlayer 側の既定値はすべて「従来どおり」なので、
    切り分けたいときはこの戻り値を渡さなければ改修前の見た目に戻る。

    prefix に "LIVE_" を渡すと `LIVE_VRMA_SMOOTH` などの上書きを優先して読む。
    """
    return [
        # カメラを引いた画に見合う大きさにする
        "-vrmaGain", f"{_env_float('VRMA_GAIN', VRMA_GAIN, prefix)}",
        "-vrmaHipsY", f"{_env_float('VRMA_HIPS_Y', VRMA_HIPS_Y, prefix)}",
        # 体の向き・傾き。0 にすれば従来どおり正面固定に戻る
        "-vrmaBodyTilt", f"{_env_float('VRMA_BODY_TILT', VRMA_BODY_TILT, prefix)}",
        "-vrmaYawLimit", f"{_env_float('VRMA_YAW_LIMIT', VRMA_YAW_LIMIT, prefix)}",
        "-vrmaHeadYaw", f"{_env_float('VRMA_HEAD_YAW', VRMA_HEAD_YAW, prefix)}",
        "-vrmaHeadCounter", f"{_env_float('VRMA_HEAD_COUNTER', VRMA_HEAD_COUNTER, prefix)}",
        # 女の子らしい所作にするための調整。0 にすればそれぞれ無効になる
        "-vrmaKeepIdleHands",
        f"{int(_env_float('VRMA_KEEP_IDLE_HANDS', VRMA_KEEP_IDLE_HANDS, prefix))}",
        "-vrmaElbowBend", f"{_env_float('VRMA_ELBOW_BEND', VRMA_ELBOW_BEND, prefix)}",
        "-vrmaWristBend", f"{_env_float('VRMA_WRIST_BEND', VRMA_WRIST_BEND, prefix)}",
        "-vrmaHeadTilt", f"{_env_float('VRMA_HEAD_TILT', VRMA_HEAD_TILT, prefix)}",
        "-vrmaSmooth", f"{_env_float('VRMA_SMOOTH', VRMA_SMOOTH, prefix)}",
    ]

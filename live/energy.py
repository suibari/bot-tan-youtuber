"""energy（biorhythm）の読み出し。

energy の実体は共有DBの `affirmative_bot.bot_state` にある `biorhythm` で、
更新するのは bsky-affirmative-bot の biorhythm_server。配信側は読むだけでよい。

コメント1件ぶんの加算も配信側からは行わない。biorhythm_server の
`liveCommentEnergySync`（apps/biorhythm_server/src/liveCommentEnergySync.ts）が
`bottan_live.comments` を15秒ごとに見て、1件につき内部energy +10 を足し、
`bot_state.biorhythm.liveCommentEnergyCursor` を進める。配信側がやることは
`memory.save_comment()` でコメントを DB に残すことだけ。
ここから `POST /energy` を投げると二重に加算されてしまう。
"""

import memory


def get_energy() -> float:
    """いまの energy を 0〜100 で返す。"""
    return memory.get_biorhythm()["energy"]

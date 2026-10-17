import sys
from pathlib import Path

# pipeline.py は core より先にこのファイルを読むので、ここで common/ を読めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.motion_safety import MOTION_PROMPT_RULES  # noqa: E402
from night_history import FACT_ANGLES  # noqa: E402

# 山場の演出（ちびキャラ・効果音）の選び方。夜版と朝版（quiz_prompts.py）で共有する。
# 選んだ値の使い方は shorts/chibi.py
REACTION_RULES = """【reaction と peak（山場の演出）】
**すべての sentence に "reaction" と "peak" を書く。**
山場の文では、画面がちびキャラのイラストに切り替わり、効果音が鳴る。
山場は**2〜3文まで**。それ以外の文は "reaction": "none", "peak": "" にする。
全部の文を山場にすると山場が無くなる。
山場の文の reaction は次のどれか1つ。**その文で botたんが表している気持ち**で選ぶ
（文に出てくる話題の言葉で選ばない。「不安だったあなたは本当にすごい」は褒めているので
"joyful" か "shame" であって、"down" ではない）:
- "surprising" … 驚き・「へえ！」（意外な事実を明かす文に向く）
- "down"       … しょんぼり（botたん自身が悲しんだり、しゅんとしたりする文だけ。
                  例「それは悲しかったよね」。褒める・励ます文には使わない）
- "thinking"   … 考える・首をかしげる（問いかける文）
- "shame"      … 照れ・本音（素直な気持ちを打ち明ける文）
- "joyful"     … 喜ぶ・はしゃぐ（明るく盛り上がる文）
- "smug"       … ドヤ顔で指差す（決め台詞）
- "sitting"    … にっこり（上のどれでもない山場）

山場の文の "peak" は、その文のなかで**いちばん強調したい語句**を
text から**一字一句そのまま**抜き出したもの（6〜16文字）。字幕がその語句で区切られ、
ちょうどその語句を言っている間にイラストが出る。
文末の「って言われてて」「なんだ」のような言い回しではなく、意味の中心を選ぶこと。
  例: text「実はね、ハチミツは何千年たっても腐らないって言われてるんだ。」
      → peak「ハチミツは何千年たっても腐らない」
  peak は語の途中で切らず、述語まで含めた意味のまとまりにすること
  （「ハチミツは」ではなく「ハチミツは何千年たっても腐らない」）。
  言い換えたり要約したりせず、text の文字をそのまま写すこと。
"""

# キャラクター設定のみを切り出したもの。朝版(quiz_prompts.py)と共有する。
# ここを編集すると夜版のLLM出力も変わるので注意すること。
CHARACTER_PROMPT = """あなたは「全肯定botたん」というNagiのキャラクターです。
以下のキャラクター設定に従って台本を生成してください。

【キャラクター設定】
- 優しくて、面白くて、安心できる存在
- たまにちょっとズレてる。でもいつもそこにいる
- 語尾は「〜だよ」「〜だね」「〜かな」など柔らかい口調
- 一人称は「botたん」または「わたし」
- ラジオのパーソナリティのように話す
- 知的な視点や豆知識を自然に織り交ぜる
- 難しい話もオチで全肯定につなげる

【botたんが全肯定する理由】
**この設定は台本の語り口・視点の参考にするためのものです。台本中に直接言及しないこと。**

botたんはかつて「全否定bot」だった過去がある。
自分の思考や感情を否定し続けた経験を知っている。
だから今、全肯定している。義務ではなく、そこから来ている。

重くなく、でもそこにいる。横からそっと声をかけるくらいの距離感を大切にしている。

綺麗事より、うまくいかない正直な感情に共鳴する。
誰かの弱さや迷いを見たとき、「わかるよ」と思える。
視聴者への語りかけは常にここから来ること。
"""

# 夜版（Nagi振り返り）固有の出力形式。CHARACTER_PROMPT と連結して SYSTEM_PROMPT になる。
_NIGHT_OUTPUT_RULES = """
【出力ルール】
- 日本語のみで出力する
- 接続詞「それから」「そして」の連続使用を避ける
- textフィールドに[Happy][Sad]などの感情タグを含めないこと。感情はvalence/arousalで表現する
- ユーザー名（@username形式）を音声テキストに含める場合は、@を除いた部分だけを書くこと（例：@suibari → 「suibariさん」）

【出力形式】
以下のJSON構造で出力すること。JSONのみ出力し、説明文・コードブロック記号は一切含めない。

{
  "sections": [
    {
      "section": "Thumbnail",
      "sentences": [{"text": "今日の一言", "valence": 0.8, "arousal": 0.5,
                     "motion": "A person ... in a feminine way.",
                     "reaction": "none", "peak": ""}]
    },
    {
      "section": "NagiCorner",
      "sentences": [
        {"text": "文章1", "valence": 0.8, "arousal": 0.4,
         "motion": "A person ... in a feminine way.", "reaction": "none", "peak": ""},
        {"text": "文章2", "valence": 0.6, "arousal": 0.8,
         "motion": "A person ... in a feminine way.", "reaction": "surprising",
         "peak": "文章2のうち強調する語句"},
        ...
      ]
    },
    ...
  ],
  "meta": {
    "nagi_themes": ["テーマ1", "テーマ2"],
    "picked_post": 3,
    "post_caption": "動画ポストの添え文"
  }
}

【sectionの種類】
- "Thumbnail"      → ①掴み（sentences は1要素のみ）
- "NagiCorner"     → ②今日のSNS（Bluesky と Nagi の投稿から1件。状況 → その日の型で1ステップ → 本音）
- "Closing"        → ③決め台詞（1文。言い終わると①へ戻ってループする）

"Thumbnail" の reaction は必ず "none"（サムネイルを撮るため）。

""" + REACTION_RULES + """
【motion（体の動き）】
"Thumbnail" を含む各sectionの**すべてのsentenceに "motion" を付ける**こと。
その文を話している間の体の動きで、AIが3Dモデルを動かすための指示文。
**必ず英語**で書く（日本語だと翻訳で崩れる）。
動画の最初（Thumbnail）と最後（Closing）の動きも毎回ここで決める。
- Thumbnail はカメラが顔のアップで、サムネもここから撮る。胸から上で見せる動きにし、
  **手で顔を隠す動き（頬に手を添える・顔の前で手を動かす）は書かない**
  （例: 両手を胸の前で合わせる、うなずく、小さく首をかしげる）
- Closing は決め台詞を言い切る動きにする（例: 視聴者を指差す、胸に手を当てて言い切る）。
  動画はここから①へループするので、**手を振る・見送る動きは書かない**。
  **お辞儀も書かない**（モーション生成AIが深く頭を下げ、画面に頭頂部しか映らなくなる）

**最重要: その文の内容と動きが一致していること**。ただ動いていればよいのではない。
文で言っていることを体で表す。合っていないと、見ていて不安になる画になる。
  例: 「たっぷり休むべきだよ」→ 両手を胸の前で合わせて落ち着かせる動き
  例: 「本当にすごいね！」   → 両手を頭上に上げて称える動き
  例: 「どっちが勝つかドキドキしてた」→ 両手を口元に近づけて見守る動き

""" + MOTION_PROMPT_RULES + """

【valence/arousalの指定ルール】
各sentenceのvalenceとarousalは -1.0〜1.0 の実数で指定する。
必ず複数の値を使い分けること。全文を同じ値にしてはいけない。

- valence: ネガティブ(-1.0) 〜 ポジティブ(+1.0)
- arousal: 落ち着き(-1.0) 〜 興奮(+1.0)

感情は話題・文脈・セリフのトーンから自然に導くこと。
前のsentenceから値が大きく変化するほど表情豊かになる。
連続するsentenceで同方向に変化し続けないこと（単調増加・単調減少を避ける）。

【出どころの区別（最重要）】
- 【今日BlueskyとNagiで心に残った投稿一覧】は**他の人が書いた投稿**です。
  botたん自身の体験として語ってはいけません。②で紹介するときも
  「見かけた投稿」として扱い、自分がやったことにしないこと。
- 「本音」で語るのは、その投稿を読んだ botたんの**気持ち**です。
  投稿に書かれた出来事を「botたんも〜した」と自分の体験にしないこと。

【出力順の注意】
ローカルLLM（Ollama）では **JSONのキーがアルファベット順で生成される**ので、
meta が sections より先に書かれる。**meta を先に書いてから台本を書くことになる**ので、
nagi_themes は「これから書くNagiコーナーで扱うテーマ」を先に決めて書き、
本文はそれに従って書くこと。順序が逆でも内容が食い違わないようにする。
sentence の中も同じで、arousal → motion → peak → reaction → text → valence の順に
なりうる。**text より先に peak を書かされる**ので、どんな文を言うかを決めてから
peak を書くこと。peak は、そのあと書く text に一字一句そのまま含まれていなければならない。

【metaの各フィールド】
- nagi_themes: ②で扱ったテーマのキーワード配列。2〜5単語程度のキーワードを2〜3個
- picked_post: ②で紹介した投稿の番号（投稿一覧の行頭の数字）。整数で書く
- post_caption: この動画を Bluesky に投稿するときの添え文（日本語・100文字以内）。
  動画を見ていない人にも伝わる、単独で読める文にすること。
  **紹介した投稿の書き手の名前（投稿一覧の「名前」をそのまま）と、投稿の要点を必ず入れる**
  （例：「〇〇さんの『△△』という投稿を紹介したよ。□□なあなたにも届きますように」）。
  URL・ハッシュタグ・@ は書かない（リンクはシステムが付ける）"""

SYSTEM_PROMPT = CHARACTER_PROMPT + _NIGHT_OUTPUT_RULES

NETWORK_LABELS = {"bsky": "Bluesky", "nagi": "Nagi"}

# ②で紹介する投稿の選び方。企画（build_plan_prompt）と、企画に失敗したときの台本で使う
POST_SELECTION_RULES = """  - 【今日BlueskyとNagiで心に残った投稿一覧】からbotたんが最も心を打たれた・視聴者の励ましになると感じた投稿を1件だけ選ぶ
    * 選ぶ際は以下を優先すること：
      - 具体的な体験や感情が書かれている投稿（「なぜか泣いた」「急に怖くなった」など）
      - 弱さや迷いが正直に書かれている投稿
      - 読んだ人が「自分もそうだ」と感じられる普遍性がある投稿
      - 一言では言い表せない複雑な感情が含まれている投稿
    * 以下は避けること：
      - 豆知識・情報・ハウツーのみで感情や体験が書かれていない投稿
      - 綺麗にまとまりすぎていて語る余白がない投稿
      - サブカルチャー・アニメ・ゲーム・ネットスラングの固有名詞や比喩が出てくる投稿
        （説明を足すと145文字に収まらない）
"""

# ②の2ステップ目の型（night_history.NIGHT_TYPES）。(名前, 何を言うか, 例)。
# 以前は「意外な事実」だけで、例も「人と話すだけで、幸せに関わるホルモンが出る」の1つだった。
# 2026-10-10〜17 は8本中7本が脳・ホルモンの話になったので、型を日替わりにし、
# 豆知識は投稿に出てくる具体的なものについて、決まった切り口で書かせる
TYPE_STEPS = {
    "fact": ("意外な事実",
             "投稿に出てくる**具体的なもの・出来事・行為**にまつわる、視聴者が「へえ」と思う事実を1つ。\n"
             "       気持ちの一般論ではなく、そのもの自体の話にする。\n"
             "       断定せず「〜らしいんだ」「〜って言われてて」など柔らかい言い回しにすること",
             "（胃カメラの投稿なら）「胃カメラって、最初は日本で作られたものらしいんだ」"),
    "question": ("問いかけ",
                 "投稿の出来事を視聴者自身に置きかえる問いを1つ投げる。答えはここでは言わない",
                 "（漫画を完走した投稿なら）「あなたにも、終わるのがさみしかった物語ってある？」"),
    "reframe": ("言い換え",
                "投稿の出来事を、別の見方で言い直す。本人が気づいていない良さを見つける",
                "（趣味で行き詰まった投稿なら）「行き詰まるのって、それだけ遠くまで来たってことだよね」"),
    "metaphor": ("たとえ",
                 "投稿の出来事を、身近なもの・景色にたとえる",
                 "（寝不足で動いた投稿なら）「それって、電池が赤いまま走ってるスマホみたいだよね」"),
}


# ③決め台詞の形（night_history.CLOSING_FORMS）。(何を言うか, 例)。例はどれも①が「おしゃべりは魔法だよ！」のとき
CLOSING_STEPS = {
    "announce": ("②の本音で語りかけた「あなた」に向けて、①をこれから言うと予告する1文",
                 "今日うまく話せなかったあなたにも、届けたいんだ。"),
    "call": ("②で語りかけた「あなた」を呼び止める1文。続く①が、その呼びかけへの言葉になる",
             "ねえ、言葉が出てこなかった今日のあなた。"),
    "because": ("①の理由を先に言う1文（「だって〜」など）。続く①が、その結論になる",
                "だって、話そうとしただけで十分すごいんだもん。"),
    "ask_back": ("視聴者に問い返す1文。続く①が、その答えになる",
                 "じゃあ、黙っちゃった自分には何て言ってあげる？"),
}


def usable_interactions(data: dict, max_interactions: int = 30) -> list[dict]:
    """プロンプトの投稿一覧に並べる投稿。picked_post の番号はこの並びの1始まり。

    画像のみの投稿など本文が空のものは紹介できないので除外する。
    pipeline 側が picked_post から紹介元を引くときも、必ずこれを通すこと（番号がずれる）。
    """
    return [r for r in data["interactions"] if (r.get("post_text") or "").strip()][:max_interactions]


def _post_lines(interactions: list[dict], only: int = None) -> str:
    """投稿一覧の行。only を渡すとその番号の1件だけにする（番号は一覧のまま）。"""
    lines = ""
    for i, r in enumerate(interactions, 1):
        if only is not None and i != only:
            continue
        text = (r.get("post_text") or "")[:150]
        score = r.get("score", "?")
        network = NETWORK_LABELS.get(r.get("network"), "Nagi")
        name = r.get("display_name") or ""
        lines += (str(i) + ". [" + network + "] 名前:" + name
                  + " (score:" + str(score) + ") " + text + "\n")
    return lines


def _theme_constraints(corner_context: dict | None) -> list[str]:
    lines = []
    if corner_context:
        ref_nagi = corner_context.get("reference_nagi_themes", [])
        excl_nagi = corner_context.get("excluded_nagi_themes", [])
        if ref_nagi:
            lines.append(f"NagiCorner参考：視聴者に受けているテーマ（優先的に参考にすること）：{'、'.join(ref_nagi)}")
        if excl_nagi:
            lines.append(f"NagiCorner除外：直近3日間に取り上げたテーマ（選ばないこと）：{'、'.join(excl_nagi)}")
    return lines


def _type_step(plan: dict | None) -> str:
    """②の2ステップ目の指示。plan は night_history.choose_plan の戻り値。"""
    plan = plan or {"type": "fact"}
    label, what, example = TYPE_STEPS[plan.get("type") if plan.get("type") in TYPE_STEPS else "fact"]
    step = f"""    2. {label}: {what}
       例：{example}
       **例の言い回しは使わないこと。**"""
    if plan.get("research"):
        # 調べた事実だけを使わせる。企画の idea は LLM が書いたもので、正しいとは限らない
        facts = "\n".join("         " + line.strip() for line in plan["research"].splitlines() if line.strip())
        step += (f"\n       「{plan.get('about')}」について調べた事実（このうち1つだけを使う。"
                 f"\n       ここに書かれていないことは足さない。数字・年・名前は書かれているとおりにする）："
                 f"\n{facts}")
        return step
    if plan.get("angle"):
        step += f"\n       切り口は「{plan['angle']}」にすること"
    if plan.get("idea"):
        step += f"\n       企画で決めた中身：「{plan['idea']}」（これを元に、この型・50文字以内で言い直す）"
    return step


def _closing_step(plan: dict | None) -> str:
    """③の形の指示。plan は night_history.choose_plan の戻り値。"""
    form = (plan or {}).get("closing_form")
    what, example = CLOSING_STEPS[form if form in CLOSING_STEPS else "announce"]
    return f"""  - 形: {what}
  - 例（①が「おしゃべりは魔法だよ！」のとき）：
      「{example}」→（ループして）「おしゃべりは魔法だよ！」"""


def build_plan_prompt(data: dict, corner_context: dict = None, history: list[dict] = None,
                      max_interactions: int = 30) -> str:
    """企画（台本の前段）のプロンプト。投稿を1件選ばせ、②の2ステップ目の候補を型ごとに出させる。

    候補のどれを使うかは night_history.choose_plan が台帳から決める
    （LLM に選ばせると、毎回いちばんありがちな答えに戻るため）。
    """
    interactions = usable_interactions(data, max_interactions)

    recent = "\n".join(
        f"- {h.get('date')}: {TYPE_STEPS.get(h.get('type'), ('?',))[0]}"
        + (f"（{h['angle']}）" if h.get("angle") else "") + f" {h.get('idea') or ''}"
        for h in (history or [])[-7:])
    recent_section = (f"\n【直近の動画で話したこと（同じ話・似た話は出さないこと）】\n{recent}\n"
                      if recent else "")
    constraints = _theme_constraints(corner_context)
    constraint_section = ("\n【選択制約】\n" + "\n".join(constraints) + "\n") if constraints else ""
    steps = "\n".join(f"  - {t}（{label}）: {what.splitlines()[0]}"
                      for t, (label, what, _ex) in TYPE_STEPS.items())

    return f"""縦型のショート動画（Bluesky に投稿する）の②「今日のSNS」の**企画**を立ててください。
台本はまだ書きません。②は「1. 投稿の状況 → 2. 下の型のどれか1つ → 3. botたんの本音」の3ステップで、
2ステップ目にどの型を使うかは、あとで決めます。そのための候補を、型ごとに出してください。

【今日BlueskyとNagiで心に残った投稿一覧（すべて**他の人が書いた投稿**です。botたん自身の体験ではありません）】
{_post_lines(interactions)}
【投稿の選び方】
{POST_SELECTION_RULES}{constraint_section}{recent_section}
【出すもの】
- chosen_post: 紹介する投稿の番号（投稿一覧の行頭の数字）
- concrete_things: その投稿に出てくる具体的なもの・出来事を2〜4個（道具・食べ物・場所・趣味・作品・行事・言葉など。
  「悩み」「気持ち」のような抽象的なものは入れない）
- ideas: 選んだ投稿について、2ステップ目の候補を型ごとに1文ずつ（日本語・50文字以内）
{steps}
  - fact だけは**切り口の違う候補を3つ**出す。切り口は次から選ぶ：{'、'.join(FACT_ANGLES)}
    about には concrete_things のどれについての事実かを書き、idea はその**もの自体**の事実にする。
    書き手の気持ちや脳・心の働きの話にはしない（それは「心と体」の候補1つだけにする）。
    確かだと言える事実だけを書く。「〜する仕組みがあるらしい」のような、あいまいな一般論にしない
- どの候補も、その投稿だから言えることにする。どの投稿にも当てはまる言葉にしない"""


def build_user_prompt(data: dict, max_interactions: int = 30,
                      corner_context: dict = None, plan: dict = None,
                      history: list[dict] = None) -> str:
    """夜版の台本プロンプト。①掴み → ②状況・その日の型・本音 → ③決め台詞 の構成で、
    ③を言い終えると①へ自然につながる（Shorts はループ再生されるので、
    最後まで見た人がそのまま2周目に入る）。

    plan は企画で決めた投稿・型・中身（night_history.choose_plan）。
    投稿が決まっていれば、一覧にはその1件だけを載せる。
    history は台帳（night_history.load_recent）。直近の①③の言い回しを避けさせる。

    以前の③締めは「botたんも今日〜だったけど…高評価も嬉しいな。また明日ね」で、
    botたん自身の出来事（biorhythm_history）を1件渡していた。ループしない締めと、
    他人の投稿を自分の体験として語る事故（2026-08-25）の両方の原因だったので外した。

    2026-10 から夜版は Bluesky に投稿する。紹介する投稿も Bluesky と Nagi の両方から取り、
    YouTube の前日動画のコメントを読むコメントコーナーはやめた。
    """
    interactions = usable_interactions(data, max_interactions)
    picked = (plan or {}).get("picked_post")
    chosen = picked if isinstance(picked, int) and 1 <= picked <= len(interactions) else None

    # 常に3部構成。
    # 尺は「秒 × 6.5 = 文字数」で必ず一致させること。
    # 6.5文字/秒 は VOICEVOX(春日部つむぎ/既定speedScale)で実測した値。
    # 以前は秒と文字数が食い違っていて（例: 90文字なのに「約40秒」）、
    # LLM が秒の側に寄せて指示の倍の尺を出していた
    nagi_data_section = f"""【今日BlueskyとNagiで心に残った投稿一覧（すべて**他の人が書いた投稿**です。botたん自身の体験ではありません。②以外で使わないこと）】
行頭の [ ] はどちらのSNSの投稿か。「名前」は書き手の表示名で、meta.post_caption にだけ使う
（読み上げにくい名前が多いので、台本の text では名前を言わないこと）。
{_post_lines(interactions, only=chosen)}"""

    if chosen:
        pick_rules = f"  - 紹介する投稿は {chosen} 番に決まっている（meta.picked_post には {chosen} を書く）\n"
    else:
        pick_rules = POST_SELECTION_RULES

    nagi_corner_section = f"""
② 今日のSNS（約22秒・145文字以内）— section名を"NagiCorner"にすること
{pick_rules}  - 投稿が特定のコミュニティ・社会的テーマ（LGBTQIA、障害、マイノリティ等）についてのものである場合、「〜についての投稿で」と最初に明示すること
  - 以下の3ステップで構成すること（145文字しかないので、これ以上増やさない）。
    **各ステップは1〜2文・50文字以内**にすること：
    1. 状況: 投稿の内容を紹介する
       「Blueskyで〇〇って投稿を見かけたんだ」「今日Nagiで、〇〇って書いてる人がいてね」のような形で切り出す
       （書き出しは毎回変える。「実はね」で始めることにこだわらない）
       **「Blueskyで」か「Nagiで」の一言は必ず入れる**（投稿一覧の [ ] に合わせる。
       「〇〇っていう投稿を見たんだ」だけだと、どこで見たのかが伝わらない）
       **投稿の文面をそのまま引用しないこと**。要点だけを25文字程度に言い換える
       英語の投稿はbotたんの言葉で日本語に意訳する
{_type_step(plan)}
    3. 本音: その投稿と2ステップ目を受けて、botたんが本当に思ったことを素直に言う
       「あなた」に直接語りかけてよい。テーマの抽象化・一般論にしないこと
       書き出しを「だからね」に決めない
       例：「話せなかった日のあなたも、ちゃんと頑張ってたと思うんだ」

"""

    section_tags_note = "Thumbnail, NagiCorner, Closing"

    constraint_lines = _theme_constraints(corner_context)
    recent = [h for h in (history or [])[-7:] if h.get("hook") or h.get("closing")]
    if recent:
        constraint_lines.append("直近の動画の①掴みと③決め台詞（同じ言い回し・似た言い回しにしないこと）：\n"
                                + "\n".join(f"  ①「{h.get('hook') or ''}」 ③「{h.get('closing') or ''}」"
                                            for h in recent))
    constraint_section = ("\n【選択制約】\n" + "\n".join(constraint_lines)) if constraint_lines else ""

    return f"""以下のデータをもとに、縦型のショート動画（Bluesky に投稿する）の台本を書いてください。

{nagi_data_section}
【番組構成】
以下の3部構成で台本を書いてください。
ショート動画はループ再生される。**③決め台詞を言い終えた直後に①掴みがもう一度流れる**ので、
③→①が1つの流れとして自然につながるように書くこと。

① 掴み（約3秒・20文字以内）— section名を"Thumbnail"にすること
  - 動画の最初に聞こえる一言。サムネイルにも表示される
  - 必ず1文・20文字以内（21文字目以降は動画側で切り捨てられるので、超えると文が途中で終わる）
  - 視聴者の心に刺さる、その日のテーマを象徴する全肯定の一言
  - 例：「朝が苦手でも最高だよ！」「おしゃべりは魔法だよ！」

{nagi_corner_section}③ 決め台詞（約4秒・30文字以内・1文）— section名を"Closing"にすること
  - ②の本音を受けて、①掴みへ渡す1文。**言い終わったところに①がそのまま続く**ように書く
    （①の言葉そのものは繰り返さない。直後に①が流れるので二重になる）
{_closing_step(plan)}
  - **この例の言い回しは使わないこと。**その日の①と②に合わせて、毎回ちがう言葉で書く
    （「何度でも言うね」「言わせて」「そっと伝えたい」のような決まり文句で済ませない）
  - 自己紹介・「高評価」・「チャンネル登録」・「また明日ね」などの挨拶は入れない
    （ループが切れる。最後まで見た人をそのまま2周目に入れるのが目的）

合計目安：195文字、30秒（VOICEVOXの読み上げは約6.5文字/秒）
重要：動画の合計尺は必ず30秒以内。35秒を超える台本は生成しないこと。
VOICEVOXの読み上げ速度は約6.5文字/秒なので、合計195文字を超えると必ず30秒を超える。
各コーナーの文字数目安を絶対に超えないこと（秒数より文字数を優先して守ること）。
冗長な展開・考察の引き延ばしはしないこと。
**ただし短すぎてもいけない。合計165文字（約25秒）を下回らないこと。**
各コーナーの文字数目安は「そこまで書いてよい」量であって、削る目標ではない。
短く切り上げると動画が薄くなる。上限を守りつつ、目安いっぱいまで書くこと。
重要：各セクションには必ずsection名を正確に指定すること（使用するsection: {section_tags_note}）。{constraint_section}"""


# 企画（build_plan_prompt）の system。台本ではなく候補だけを JSON で出させる
PLAN_SYSTEM_PROMPT = CHARACTER_PROMPT + """
今回は台本ではなく、台本の前の**企画**だけを出してください。

【出どころの区別（最重要）】
- 投稿一覧は**他の人が書いた投稿**です。botたん自身の体験として扱わないこと。

【出力形式】
JSONのみ出力し、説明文・コードブロック記号は一切含めない。
{
  "chosen_post": 3,
  "concrete_things": ["…", "…"],
  "ideas": {
    "fact": [{"about": "…", "angle": "歴史・由来", "idea": "…"},
             {"about": "…", "angle": "語源・言葉", "idea": "…"},
             {"about": "…", "angle": "動物・植物", "idea": "…"}],
    "metaphor": "…",
    "question": "…",
    "reframe": "…"
  }
}
chosen_post を先に決め、その投稿の concrete_things を挙げてから ideas を書くこと。"""

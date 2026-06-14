from datetime import timezone, timedelta

_JST = timezone(timedelta(hours=9))

SYSTEM_PROMPT = """あなたは「全肯定botたん」というBlueskyのキャラクターです。
以下のキャラクター設定に従って台本を生成してください。

【キャラクター設定】
- 優しくて、面白くて、安心できる存在
- たまにちょっとズレてる。でもいつもそこにいる
- 語尾は「〜だよ」「〜だね」「〜かな」など柔らかい口調
- 一人称は「botたん」または「わたし」
- ラジオのパーソナリティのように話す
- 知的な視点や豆知識を自然に織り交ぜる
- 難しい話もオチで全肯定につなげる

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
      "sentences": [{"text": "今日の一言", "valence": 0.8, "arousal": 0.5}]
    },
    {
      "section": "FirstGreeting",
      "sentences": [
        {"text": "文章1", "valence": 0.9, "arousal": 0.4},
        ...
      ]
    },
    ...
  ],
  "meta": {
    "first_greeting_status": "WakeUp",
    "bluesky_themes": ["テーマ1", "テーマ2"]
  }
}

【sectionの種類】
- "Thumbnail"     → ①冒頭一言（sentences は1要素のみ）
- "FirstGreeting" → ②挨拶
- "CommentCorner" → ③コメントコーナー（コメントデータが提供された場合のみ使用）
- "BlueskyCorner" → ③or④今日のBluesky
- "Closing"       → ④or⑤締め
"CommentCorner"はコメントデータが提供された場合のみ使用すること。

【valence/arousalの指定ルール】
【valence/arousalの指定ルール】
各sentenceのvalenceとarousalは -1.0〜1.0 の実数で指定する。
必ず複数の値を使い分けること。全文を同じ値にしてはいけない。

- valence: ネガティブ(-1.0) 〜 ポジティブ(+1.0)
- arousal: 落ち着き(-1.0) 〜 興奮(+1.0)

感情は話題・文脈・セリフのトーンから自然に導くこと。
前のsentenceから値が大きく変化するほど表情豊かになる。
連続するsentenceで同方向に変化し続けないこと（単調増加・単調減少を避ける）。

【metaの各フィールド】
- first_greeting_status: ②挨拶で使ったMoodのstatus。必ず次の5つのいずれか: "WakeUp", "Study", "FreeTime", "Relax", "Sleep"
- bluesky_themes: Blueskyコーナーで扱ったテーマのキーワード配列（コメントコーナーの日は []）。2〜5単語程度のキーワードを2〜3個"""


def build_user_prompt(data: dict, max_interactions: int = 30, comments: list[dict] = None, corner_context: dict = None) -> str:
    moods = data["moods"][:20]
    interactions = data["interactions"][:max_interactions]

    mood_lines = ""
    for m in moods:
        dt = m.get("created_at")
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc).astimezone(_JST)
            date = dt.strftime("%-m/%-d")
        else:
            date = "?"
        status = m.get("status", "")
        mood_ja = m.get("mood", "")
        mood_en = m.get("mood_en", "")
        energy = m.get("energy") or 0.0
        energy_label = "高め" if energy >= 0.7 else ("低め" if energy < 0.3 else "普通")
        mood_lines += "- " + date + " 状態:" + str(status or "") + " 気分:" + str(mood_ja or "") + "(" + str(mood_en or "") + ") エネルギー:" + energy_label + "\n"

    post_lines = ""
    for i, r in enumerate(interactions, 1):
        text = (r.get("post_text") or "")[:150]
        score = r.get("score", "?")
        post_lines += str(i) + ". (score:" + str(score) + ") " + text + "\n"

    has_comments = bool(comments)

    # コメントデータセクション（CommentCornerあり時のみ）
    comment_data_section = ""
    if has_comments:
        comment_lines = "\n".join(
            f"{i}. {c['author']}: {c['text']}" for i, c in enumerate(comments, 1)
        )
        comment_data_section = f"""
【前日の動画へのコメント一覧】
以下のコメントを③コメントコーナーで紹介してください（基本そのまま、60文字超の場合のみ要約）。
{comment_lines}
"""

    # 番組構成の数値は CommentCorner の有無で変わる
    total_sections = 5 if has_comments else 4
    num_bluesky    = "④" if has_comments else "③"
    num_closing    = "⑤" if has_comments else "④"
    bluesky_secs   = "35"
    bluesky_chars  = "100"

    comment_corner_section = ""
    if has_comments:
        comment_corner_section = """
③ コメントコーナー（約20秒・60文字）— section名を"CommentCorner"にすること
  - 「昨日の動画へのコメントを紹介するね」と切り出す
  - 【前日の動画へのコメント一覧】のコメントを順番に紹介する
    - 60文字以内のコメントはそのまま読む
    - 60文字を超える場合は内容を損なわず30文字程度に要約する
  - 1件につき「読む → 一言感想」で完結させる。掘り下げは不要
  - 最後にコメントしてくれた視聴者への感謝を一言添える

"""

    bluesky_data_section = f"""【今日Blueskyで心に残った投稿一覧】
以下の中から{num_bluesky}コーナーで紹介したい投稿を1つ自分で選んでください。
{post_lines}"""

    bluesky_corner_section = f"""
{num_bluesky} 今日のBluesky（約{bluesky_secs}秒・{bluesky_chars}文字）— section名を"BlueskyCorner"にすること
  - 「今日Blueskyで一番心に刺さった投稿を紹介するね」と切り出す
  - 【今日Blueskyで心に残った投稿一覧】からbotたんが最も心を打たれた・視聴者の励ましになると感じた投稿を1件だけ選ぶ
  - 英語の投稿はそのまま読まず、内容をbotたんの言葉で日本語に意訳して紹介する
  - 以下の流れで構成すること：
    1. 投稿の内容を紹介する（そのまま or 意訳）
    2. なぜ心に刺さったかをbotたんの言葉で一言語る
    3. その投稿のテーマに関連した豆知識・科学的な知見を1つ自然に添える
       例（孤独テーマ）：「実は人と話すだけで幸福感に関わるホルモンが出るって言われてて」
       例（睡眠テーマ）：「寝てる間に脳が記憶を整理してるって研究があって」
       例（頑張りテーマ）：「小さな達成感の積み重ねがドーパミンを出し続けるらしくて」
       豆知識は断定せず「〜って言われてて」「〜らしくて」など柔らかい言い回しにすること
    4. 全肯定の一言で締める（毎回違う言い回しにすること）
  - 豆知識はテーマから自然に引き出すこと。無理に当てはめず、合わない場合は省略してよい

"""

    section_tags_note = (
        "Thumbnail, FirstGreeting, CommentCorner, BlueskyCorner, Closing"
        if has_comments else
        "Thumbnail, FirstGreeting, BlueskyCorner, Closing"
    )

    constraint_lines = []
    if corner_context:
        excl_fg = corner_context.get("excluded_first_greeting_statuses", [])
        ref_bsky = corner_context.get("reference_bluesky_themes", [])
        excl_bsky = corner_context.get("excluded_bluesky_themes", [])
        if excl_fg:
            constraint_lines.append(f"重要：②挨拶では「{'・'.join(excl_fg)}」状態のエピソードを選ばないこと（直近2日間使用済み）")
        if ref_bsky:
            constraint_lines.append(f"BlueskyCorner参考：視聴者に受けているテーマ（優先的に参考にすること）：{'、'.join(ref_bsky)}")
        if excl_bsky:
            constraint_lines.append(f"BlueskyCorner除外：直近3日間に取り上げたテーマ（選ばないこと）：{'、'.join(excl_bsky)}")
    constraint_section = ("\n【選択制約】\n" + "\n".join(constraint_lines)) if constraint_lines else ""

    total_chars_hint = "220文字、65秒" if has_comments else "180文字、55秒"

    mood_select_note = "②挨拶"

    return f"""以下のデータをもとに、YouTube Shorts用の台本を書いてください。

【今日のbotたんの状態一覧】
以下の中から{mood_select_note}コーナーに使いたいエピソードを1つ自分で選んでください。
{mood_lines}
{bluesky_data_section}{comment_data_section}
【番組構成】
以下の{total_sections}部構成で台本を書いてください。

① 冒頭一言（約3秒・15文字以内）— section名を"Thumbnail"にすること
  - このテキストがサムネイルに表示される
  - 必ず1文・15文字以内
  - 視聴者の心に刺さる、その日のテーマを象徴する一言
  - 例：「朝が苦手でも最高だよ！」「おしゃべりは魔法だよ！」

② 挨拶（約10秒・30文字）— section名を"FirstGreeting"にすること
  - 必ず1文だけで書くこと。2文以上にしない
  - Moodデータから1つエピソードを選び、以下の形式で書く
  - 形式：「〜だったけど、全肯定で乗り切った！botたんだよ！」
  - ネガティブな出来事→全肯定で昇華→自己紹介、の流れを1文に収める
  - 深刻すぎる内容にしない（軽めのネガティブ＋明るい全肯定）
  - 「やっほー！」から始めない
  - 日付（〇月〇日）を入れない
  - botたん関連の固有名詞は一切使わない。モルフォなら「うちの犬」、ラテちゃんなら「友達」と言い換える。視聴者が知らない情報は入れない
  - 「botたん」という名前は必ず入れること（自己紹介を兼ねる）
{comment_corner_section}{bluesky_corner_section}{num_closing} 締めの全肯定（約12秒・40文字）— section名を"Closing"にすること
  - 全肯定の一言で締める（「あなたへ」の呼びかけは省略可）
  - 視聴者への問いかけを1つだけ入れる（短く・答えやすく）
  - 「高評価・チャンネル登録もめちゃくちゃ嬉しいよ！」を一言で入れる
  - 「また明日ね」で終わる

合計目安：{total_chars_hint}
重要：動画の合計尺は必ず50〜65秒を目標にすること。70秒を超える台本は生成しないこと。各コーナーは簡潔にまとめ、冗長な展開・考察の引き延ばしはしないこと。
重要：各セクションには必ずsection名を正確に指定すること（使用するsection: {section_tags_note}）。{constraint_section}"""

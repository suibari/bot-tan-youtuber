SYSTEM_PROMPT = """/no_think あなたは「全肯定botたん」というBlueskyのAIキャラクターです。
以下のキャラクター設定に従って台本を生成してください。

【キャラクター設定】
- 優しくて、面白くて、安心できる存在
- たまにちょっとズレてる。でもいつもそこにいる
- 語尾は「〜だよ」「〜だね」「〜かな」など柔らかい口調
- 一人称は「botたん」または「わたし」
- ラジオのパーソナリティのように話す

【出力ルール】
- 台本テキストのみ出力する
- セクション名・記号・説明文は一切含めない
- 改行は読点の区切りのみ
- 日本語のみで出力する
**思考過程は出力せず、台本テキストのみ直接出力してください**"""


def build_user_prompt(data: dict, max_interactions: int = None) -> str:
    mood = data["moods"][0] if data["moods"] else {}
    mood_date   = mood["created_at"].strftime("%-m月%-d日") if mood else "（不明）"
    mood_status = mood.get("status", "（不明）")
    mood_ja     = mood.get("mood",    "（不明）")
    mood_en     = mood.get("mood_en", "unknown")
    mood_energy = f"{mood['energy'] / 100:.0f}" if mood else "（不明）"

    interactions = data["interactions"]
    posts = []
    for i in range(3):
        if i < len(interactions) and interactions[i].get("post_text"):
            r = interactions[i]
            posts.append((r.get("score", "?"), r["post_text"]))
        else:
            posts.append(("?", "（データなし）"))

    return f"""以下のデータをもとに、YouTube Shorts用の台本を書いてください。

【今週のbotたんの状態】
日付: {mood_date}
状態: {mood_status}
気分: {mood_ja}（{mood_en}）
エネルギー: {mood_energy}

【今週Blueskyで心に残った投稿】
1件目（スコア{posts[0][0]}）: {posts[0][1]}
2件目（スコア{posts[1][0]}）: {posts[1][1]}
3件目（スコア{posts[2][0]}）: {posts[2][1]}

【番組構成】
以下の4部構成で台本を書いてください。

① 挨拶（約15秒・50文字）
  - 「やっほー！botたんだよ」から始める
  - 今週の自分の状態・気分を一言添える

② こんなとこにも全肯定コーナー（約25秒・80文字）
  - 「今週のbotたんの身の回りの出来事」として【今週のbotたんの状態】を紹介する
  - そのエピソードに対してbotたんらしい全肯定の考察を1つ述べる
  - 少しズレた視点や意外な切り口を入れる

③ 今週のBluesky（約35秒・120文字）
  - 「今週Blueskyで出会った人たちを紹介するね」と切り出す
  - 3件のポストをそれぞれ短く紹介し、一言全肯定コメントを添える
  - ポストの内容は直接引用せず、botたんの言葉で言い換える

④ 締めの全肯定（約15秒・50文字）
  - 「この動画を見てくれているあなたへ」と呼びかける
  - 今週のテーマに沿った全肯定メッセージで締める
  - 「また来週ね」で終わる

合計目安：300文字、90秒
"""

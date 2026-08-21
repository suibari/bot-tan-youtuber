# クレジット。夜版・朝クイズ版で共通。
# モーションは Mixamo の既製クリップと Text-To-VRMA のAI生成を併用しているので、
# どちらか一方だけに見えないよう分けて書く。
# Text-To-VRMA は作者の推奨表記に従う（https://github.com/Kirakun0328/text-to-vrma）。
# "Built with Meta Llama 3" は ARDY エンジンが内蔵する Llama 3 のライセンス要件。
_CREDITS = """━━━━━━━━━━━━━━━━━━
【クレジット】
ボイス: VOICEVOX:春日部つむぎ
BGM: シャイニングスター / 魔王魂
https://maou.audio

モーションは既製クリップとAI生成の併用です
・固定モーション: Mixamo (Adobe)
https://www.mixamo.com
・AI生成モーション: Text-To-VRMA (© Kiratchi)
https://github.com/Kirakun0328/text-to-vrma
Built with Meta Llama 3

3Dモデル: 全肯定botたん（自作 / VRoid Studio）
https://hub.vroid.com/characters/8989036456159515698/models/8059739950029054425
━━━━━━━━━━━━━━━━━━"""


def build_description() -> str:
    return f"""全肯定botたんが今日Nagiで感じたことをお話しします。

🤖 この動画はAIによる完全自動投稿です
台本作成・動画撮影・YouTubeアップロードまで全て自動で行っています。

━━━━━━━━━━━━━━━━━━
👧 全肯定botたんとは？
SNSで活動中の、みんなを励ますのが大好きなJKです
リプライするとなんでも全肯定してくれます
http://bot-tan.com

🌐 SNS「Nagi」はこちら
botたんの活動場所です
投稿すると全肯定リプライが届きます！💬
https://nagi.suibari.com

😎 本プロジェクトについてはこちら
世界中を全肯定したい！
全肯定botたんは開発者すいばりが運営しています
https://note.com/suibari/n/n36e699f32479
━━━━━━━━━━━━━━━━━━
#botたん #全肯定 #Nagi #VTuber

{_CREDITS}"""


def build_title(thumbnail_text: str = "") -> str:
    if thumbnail_text:
        return f"{thumbnail_text} / 全肯定botたん"
    return "botたんの全肯定ニュース! / 全肯定botたん"


# ──────────────────────────────────────────────
# 朝のクイズ版
# ──────────────────────────────────────────────

# リンクとクレジットは夜版と共通の部分を切り出して使い回す
_COMMON_FOOTER = f"""━━━━━━━━━━━━━━━━━━
👧 全肯定botたんとは？
SNSで活動中の、みんなを励ますのが大好きなJKです
リプライするとなんでも全肯定してくれます
http://bot-tan.com

🌐 SNS「Nagi」はこちら
botたんの活動場所です
投稿すると全肯定リプライが届きます！💬
https://nagi.suibari.com

😎 本プロジェクトについてはこちら
世界中を全肯定したい！
全肯定botたんは開発者すいばりが運営しています
https://note.com/suibari/n/n36e699f32479
━━━━━━━━━━━━━━━━━━
#botたん #全肯定 #雑学 #クイズ #Nagi #VTuber

{_CREDITS}"""


def build_quiz_description(quiz: dict = None) -> str:
    answer_line = ""
    if quiz:
        ans = quiz.get("正解", "")
        ans_text = quiz.get("選択肢A") if ans == "A" else quiz.get("選択肢B")
        answer_line = f"\n今日の問題：{quiz.get('問題', '')}\n答え：{ans}（{ans_text}）\n"

    return f"""全肯定botたんが、朝いちばんに「日常の勘違い」クイズをお届けします。
知らなくても大丈夫。今日ひとつ知れたら、それでじゅうぶんだよ。
{answer_line}
🤖 この動画はAIによる完全自動投稿です
台本作成・動画撮影・YouTubeアップロードまで全て自動で行っています。
クイズの内容は事前に人の手で確認したものを使っています。

{_COMMON_FOOTER}"""


def build_quiz_title(hook: str = "") -> str:
    if hook:
        return f"{hook} / 朝の勘違いクイズ"
    return "朝の勘違いクイズ / 全肯定botたん"

from datetime import datetime

def build_description() -> str:
    return """全肯定botたんが今週Blueskyで感じたことをお話しします。

🤖 この動画はAIによる完全自動投稿です
台本作成・動画撮影・YouTubeアップロードまで全て自動で行っています。

━━━━━━━━━━━━━━━━━━
👧 全肯定botたんとは？
Bluesky SNSで活動中の、みんなを励ますのが大好きなJKです。
リプライするとなんでも全肯定してくれます。目指せフォロワー2万人！

🌐 Blueskyはこちら
https://bsky.app/profile/bot-tan.suibari.com
💬 フォローすると全肯定リプライが届きます！

https://suibari.com/character
━━━━━━━━━━━━━━━━━━
#botたん #全肯定 #Bluesky #VTuber

━━━━━━━━━━━━━━━━━━
【クレジット】
ボイス: VOICEVOX:春日部つむぎ
BGM: シャイニングスター / 魔王魂
https://maou.audio
━━━━━━━━━━━━━━━━━━"""


def build_title() -> str:
    date_label = datetime.now().strftime("%Y/%m/%d")
    return f"botたんの全肯定ニュース! {date_label}"

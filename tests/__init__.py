"""bot-tan-youtuber tests.

live/ を import パスに入れておく。各テストは live のモジュールを
`load_with_stubs` で名指しに読み込むが、読み込まれた側が素の import で
別のモジュールを引く場合（filler.py / live.py の `import topics`）は、
そのモジュールが普通に見つかる必要がある。

**スタブを置きたいモジュールは、これまでどおり load_with_stubs で
sys.modules に差し込むこと。** ここで通すのは、外の世界に触らない
純粋なモジュール（topics.py）を実物のまま使わせるため。
"""

import sys
from pathlib import Path

_LIVE = Path(__file__).resolve().parents[1] / "live"
if str(_LIVE) not in sys.path:
    sys.path.append(str(_LIVE))

"""live/ が再輸出ファサード越しに呼んでいる名前が、実際に引けること。

`live/voice.py` と `live/config.py` は「`import voice` という既存の呼び出しを
壊さないための再輸出」で、実体は `common/` 側にある。手で並べた import リスト
なので、common/ に関数を足したときに足し忘れる。

2026-09-09 の配信でそれが起きた。前日のコミット 64a2ece が VOICEVOX の
swap-in 対策として `common/voice.py` に `warmup()` を足し、`live/live.py` から
`voice.warmup()` を呼ぶようにしたが、`live/voice.py` の再輸出リストに
`warmup` が無かった。呼び出し側は例外を握って続行する作りだったので、
`module 'voice' has no attribute 'warmup'` がログに1行出ただけで、対策そのものが
一度も走らないまま「入れたつもり」になっていた。

`from config import X` は無いものを書けば import の時点で落ちるが、
`voice.X` のような属性アクセスは**実際にその行を通るまで**分からない。
配信中にしか通らない行なら、気づくのは本番のログでになる。そこでソースを
読んで `voice.X` を全部拾い、モジュールに在ることをここで確かめる。
"""

import ast
import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"

for path in (str(ROOT), str(LIVE)):
    if path not in sys.path:
        sys.path.insert(0, path)

# 再輸出ファサードのモジュール名。live/ 側の名前で書く
FACADES = ("voice",)


def attributes_used(source: str, module_name: str) -> set:
    """`<module_name>.<name>` の <name> を全部拾う。"""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == module_name):
            names.add(node.attr)
    return names


class FacadeReExportTest(unittest.TestCase):
    def test_every_name_live_uses_is_re_exported(self):
        for module_name in FACADES:
            module = importlib.import_module(module_name)
            for path in sorted(LIVE.glob("*.py")):
                if path.stem == module_name:
                    continue        # ファサード自身
                used = attributes_used(path.read_text(encoding="utf-8"),
                                       module_name)
                missing = {name for name in used if not hasattr(module, name)}
                with self.subTest(module=module_name, file=path.name):
                    self.assertEqual(
                        missing, set(),
                        f"{path.name} が {module_name}.{{{', '.join(sorted(missing))}}} "
                        f"を呼んでいますが live/{module_name}.py が再輸出していません")

    def test_warmup_is_reachable(self):
        """名指しでも見ておく。これが無かったのが 2026-09-09 の原因。"""
        self.assertTrue(callable(importlib.import_module("voice").warmup))


if __name__ == "__main__":
    unittest.main()

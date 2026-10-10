"""Run the actual quiz entrypoint with external work stubbed at the boundaries."""
import ast
import json
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


class EndProbe(BaseException):
    pass


class ShortsOrderTest(unittest.TestCase):
    def run_quiz(self, stage='all', interrupt_audio=False, interrupt_motion=False):
        events = []
        source = Path(__file__).parents[1] / 'shorts/quiz_pipeline.py'
        node = next(n for n in ast.parse(source.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'main')
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        args = types.SimpleNamespace(keep_temp=False, stage=stage, timeline='', preview=False,
                                     quiz_id=1, dry_run=False)
        quiz = {'id': 1, '問題': '問題', '正解': 'A'}
        def audio(*args):
            events.append('audio')
            if interrupt_audio:
                raise EndProbe()
            return []
        def generate(*args):
            events.append('motion')
            if interrupt_motion:
                raise EndProbe()
            return [{'file': 'motion.vrma', 'time': 0}]
        core = Mock()
        core.env_flag.return_value=False
        core.VRMA_MOTION_DIR='/tmp/unused-motions'
        core._timed.side_effect=lambda label, fn, *a, **kw: fn(*a, **kw)
        core.ardy_start.side_effect=lambda **kw: events.append('start') or object()
        core.ardy_wait_ready.return_value=True
        core.build_vrma_motions.side_effect=generate
        core.ardy_stop.side_effect=lambda proc: events.append('stop')
        def finished(*args):
            events.append('after-motion')
            raise EndProbe()
        # BGM も GPU を使う（ACE-Step）。音声の後・ARDY の前に1回だけ走ること
        bgm = types.SimpleNamespace(generate=lambda kind, out_dir: events.append('bgm'))
        env = dict(parse_args=lambda _: args, core=core, datetime=datetime, tempfile=tempfile,
                   bgm=bgm,
                   Path=Path, time=time, json=json,
                   quiz_data=types.SimpleNamespace(next_quiz=lambda **kw: quiz,
                                                   build_ending_sentences=lambda: []),
                   generate_quiz_script=lambda quiz: {}, build_audio=audio,
                   # 英訳は ollama を使う。ACE-Step・ARDY が GPU を取る前に済ませること
                   translate_script=lambda quiz, script: events.append('translate'),
                   build_subtitles=lambda segments, *a: [], build_vrma_blocks=lambda *a: ['block'],
                   build_emotions=finished)
        exec(compile(module, str(source), 'exec'), env)
        with tempfile.TemporaryDirectory() as tmp, patch.object(tempfile, 'gettempdir', return_value=tmp), patch('common.unity_license.ensure'):
            try:
                env['main']([])
            except EndProbe:
                pass
        return events

    def test_motion_starts_only_after_audio_and_stops_before_render(self):
        self.assertEqual(self.run_quiz(), ['translate', 'audio', 'bgm', 'start', 'motion', 'stop', 'after-motion'])

    def test_voice_only_never_starts_ardy(self):
        self.assertEqual(self.run_quiz(stage='voice'), ['translate', 'audio'])

    def test_script_only_never_starts_ardy(self):
        self.assertEqual(self.run_quiz(stage='script'), [])

    def test_interrupted_audio_never_starts_ardy(self):
        self.assertEqual(self.run_quiz(interrupt_audio=True), ['translate', 'audio'])

    def test_interrupted_motion_still_stops_ardy(self):
        self.assertEqual(self.run_quiz(interrupt_motion=True), ['translate', 'audio', 'bgm', 'start', 'motion', 'stop'])

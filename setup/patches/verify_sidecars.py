import ast
import os
from pathlib import Path
import importlib.util
import sys
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('staged_tts', os.environ['TTS_SERVER_SOURCE'])
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)

class ServerTest(unittest.TestCase):
    def test_low_vram_has_explicit_code(self):
        engine = server.Engine(server.Settings())
        with patch.object(server.torch.cuda, 'mem_get_info', return_value=(1024, 2048)):
            with self.assertRaises(server.HTTPException) as error:
                engine._load_locked()
        self.assertEqual(error.exception.detail['code'], 'insufficient_vram')

    def test_oom_clears_model_and_returns_retryable_error(self):
        engine = server.Engine(server.Settings())
        runtime = Mock()
        engine.runtime = runtime
        @server.oom_safe
        def failing(self):
            raise server.torch.cuda.OutOfMemoryError('CUDA out of memory')
        with patch.object(server.torch.cuda, 'empty_cache') as clear:
            with self.assertRaises(server.HTTPException) as error:
                failing(engine)
        self.assertEqual(error.exception.detail['code'], 'cuda_oom')
        self.assertFalse(engine.loaded)
        runtime.unload.assert_called_once()
        clear.assert_called()

    def test_background_failure_reaches_client(self):
        engine = server.Engine(server.Settings())
        engine._load_error = server.HTTPException(status_code=503, detail={"code": "insufficient_vram"})
        with patch.object(engine, 'voice_path', return_value='unused'):
            with self.assertRaises(server.HTTPException) as error:
                engine.synthesize('hello', wait_load=False)
        self.assertEqual(error.exception.detail['code'], 'insufficient_vram')
        self.assertIsNone(engine._load_error)

    def test_other_error_is_not_oom(self):
        @server.oom_safe
        def failing(self):
            raise RuntimeError('bad input')
        with self.assertRaisesRegex(RuntimeError, 'bad input'):
            failing(server.Engine(server.Settings()))

    def test_cpu_device_passed_to_both_adapter_loads(self):
        # 実際のfrom_pretrainedメソッドを実行し、PEFTの呼び出しを捕捉する。
        tree = ast.parse(Path(os.environ['ARDY_ENCODER_SOURCE']).read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LLM2Vec')
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'from_pretrained')
        fn.decorator_list=[]
        module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        model = Mock()
        model.peft_config = {}
        model_class = Mock()
        model_class.from_pretrained.return_value = model
        factory=Mock()
        factory._get_model_class.return_value=model_class
        peft=Mock()
        env={'os':os, 'AutoTokenizer':Mock(), 'AutoConfig':Mock(), 'PeftModel':peft}
        with patch.dict(os.environ, TEXT_ENCODER_DEVICE='cpu'):
            exec(compile(module, '<adapter-test>', 'exec'), env)
            env['from_pretrained'](factory, 'base', 'adapter')
        self.assertEqual(peft.from_pretrained.call_count,2)
        for call in peft.from_pretrained.call_args_list:
            self.assertEqual(call.kwargs['torch_device'],'cpu')

unittest.main()

import configparser
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SETUP = Path(__file__).resolve().parents[1] / "setup"


class DisplayServiceTest(unittest.TestCase):
    def test_root_display_service_and_notification_use_separate_execution_paths(self):
        units = configparser.ConfigParser(interpolation=None)
        units.read(SETUP / "bottan-live-display.service", encoding="utf-8")
        self.assertEqual(units["Unit"]["OnFailure"], "bottan-live-display-notify.service")
        self.assertEqual(units["Service"]["User"], "root")
        self.assertEqual(units["Service"]["WorkingDirectory"], "/")
        self.assertEqual(
            units["Service"]["ExecStart"],
            "/usr/local/libexec/bottan/reset_display.sh __USER__",
        )
        self.assertNotIn("__INSTALL_DIR__", (SETUP / "bottan-live-display.service").read_text())

        units.read(SETUP / "bottan-live-display-notify.service", encoding="utf-8")
        self.assertEqual(units["Service"]["User"], "__USER__")
        self.assertEqual(units["Service"]["WorkingDirectory"], "__INSTALL_DIR__")
        self.assertEqual(units["Service"]["NoNewPrivileges"], "yes")
        self.assertTrue(
            units["Service"]["ExecStart"].startswith("__INSTALL_DIR__/venv/bin/python ")
        )

    def test_display_precedes_stream_without_a_direct_xorg_dependency(self):
        units = configparser.ConfigParser(interpolation=None, strict=False)
        units.read(SETUP / "bottan-live.service", encoding="utf-8")
        self.assertEqual(units["Unit"]["Requires"], "bottan-live-display.service")
        self.assertIn("bottan-live-display.service", units["Unit"]["After"].split())
        self.assertNotIn("bottan-live-xorg.service", units["Unit"]["After"].split())


@unittest.skipIf(os.geteuid() == 0, "GL probes run as an unprivileged user")
class DisplayProbeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "LC_ALL": "C"}
        self.stub(
            "glxinfo",
            '[ "$DISPLAY" = :99 ] && [ "$__GL_SYNC_TO_VBLANK" = 0 ] || exit 1\n'
            'printf "OpenGL renderer string: %s\\n" "$TEST_RENDERER"\n',
        )
        self.stub("glxgears", 'printf "300 frames in 5.0 seconds = %s FPS\\n" "$TEST_FPS"\n')

    def stub(self, name, contents):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + contents)
        path.chmod(0o700)

    def probe(self, mode, **overrides):
        return subprocess.run(
            ["/bin/bash", str(SETUP / "probe_display.sh"), mode],
            env={**self.env, **overrides},
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
        )

    def test_hardware_renderer_and_acceptable_fps_pass(self):
        result = self.probe("check", TEST_RENDERER="NVIDIA", TEST_FPS="60.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("60.0fps", result.stdout)

    def test_software_renderer_and_low_fps_gate_the_stream(self):
        for renderer, fps, reason in [
            ("llvmpipe", "5694.0", "CPU 描画"),
            ("NVIDIA", "1.0", "1.0fps"),
        ]:
            with self.subTest(renderer=renderer, fps=fps):
                result = self.probe("check", TEST_RENDERER=renderer, TEST_FPS=fps)
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)

    def test_unavailable_display_fails_readiness_check(self):
        self.stub("xdpyinfo", "exit 1\n")
        result = self.probe("wait", DISPLAY_WAIT_SEC="0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("応答しません", result.stderr)

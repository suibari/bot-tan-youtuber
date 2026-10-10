"""Unity Personal の事前確認と、Hub の保存済み認証による更新。"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from common.env import env_float
from common import notify, xvfb


class UnityLicenseError(RuntimeError):
    """同じ Editor 起動を繰り返しても復旧しないライセンス不良。"""


@dataclass(frozen=True)
class LicenseStatus:
    valid: bool
    deadline: datetime | None
    detail: str


def client_path(editor):
    return Path(editor).parent / 'Data/Resources/Licensing/Client/Unity.Licensing.Client'


def parse_status(output, now=None):
    now = now or datetime.now(timezone.utc)
    # Licensing Client が現在ユーザーに許可した権限だけを扱う。
    for block in output.split('Path:')[1:]:
        rights = {line.strip() for line in block.splitlines()}
        if not {'com.unity.editor', 'com.unity.editor.ui'} <= rights:
            continue
        path = block.splitlines()[0].strip()
        root = ET.parse(path).getroot()
        for group in root.findall('./License/EntitlementGroups/EntitlementGroup'):
            names = {e.get('Id') for e in group.findall('./Entitlements/Entitlement')}
            if not {'com.unity.editor', 'com.unity.editor.ui'} <= names:
                continue
            raw = group.findtext('./Context/UpdateDate')
            if not raw:
                return LicenseStatus(False, None, '更新期限を取得できません')
            deadline = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            return LicenseStatus(deadline > now, deadline, f'更新期限: {deadline.isoformat()}')
    return LicenseStatus(False, None, 'Editor の有効な利用権限がありません')


def check(editor):
    try:
        result = subprocess.run([str(client_path(editor)), '--showEntitlements'],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            return LicenseStatus(False, None, 'Licensing Client の確認に失敗しました')
        return parse_status(result.stdout)
    except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError):
        return LicenseStatus(False, None, 'ライセンスの正常性を確認できません')


def _report_failure(detail):
    state = Path.home() / '.cache/bottan/unity-license-notify.json'
    try:
        previous = json.loads(state.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    if time.time() - previous.get('failed_at', 0) < 86400:
        return
    if notify.send(f'⚠️ Unity ライセンス自動更新失敗: {detail}\n'
                   '本番ユーザーで Unity Hub に再サインインしてください。'):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'failed_at': time.time()}))


def _report_recovered():
    state = Path.home() / '.cache/bottan/unity-license-notify.json'
    if state.exists() and notify.send('✅ Unity ライセンスが復旧しました。'):
        state.unlink(missing_ok=True)


def refresh(editor, before):
    """Hub 起動後にライセンス期限が伸びるまで待つ。トークンは読み取らない。"""
    hub = shutil.which('unityhub')
    if not hub:
        raise UnityLicenseError('Unity Hub がありません')
    # 常駐 Hub を閉じて起動時の認証更新を確実に通す。
    close_hub()
    env = os.environ.copy()
    runtime = f'/run/user/{os.getuid()}'
    if not Path(runtime, 'bus').exists():
        raise UnityLicenseError('ユーザーのセッションバスがありません')
    env['XDG_RUNTIME_DIR'] = runtime
    env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={runtime}/bus'
    env.pop('XAUTHORITY', None)
    display_proc, display = xvfb.start_xvfb(150, 200, reserved=(':99',))
    env['DISPLAY'] = display
    proc = None
    try:
        proc = subprocess.Popen([hub], env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        until = time.monotonic() + env_float('UNITY_LICENSE_REFRESH_TIMEOUT_SEC', 120)
        while time.monotonic() < until:
            status = check(editor)
            if status.valid and status.deadline and (before.deadline is None or status.deadline > before.deadline):
                print(f'[Unity認証] 更新成功: {status.detail}')
                return status
            if proc.poll() is not None:
                raise UnityLicenseError('Unity Hub が更新前に終了しました')
            time.sleep(2)
        raise UnityLicenseError('Hub 更新が時間内に完了しません（再サインインまたは鍵保管庫の確認が必要）')
    finally:
        close_hub()
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        display_proc.terminate()
        try:
            display_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            display_proc.kill()
            display_proc.wait(timeout=5)


def close_hub(grace_sec=5, prefix="/opt/unityhub/"):
    # Editor は対象外。同じユーザーの Hub のみを終了する。
    import signal
    pids = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            command = (proc / 'cmdline').read_bytes().split(b'\0')[0].decode()
            if command.startswith(prefix):
                pids.append(int(proc.name))
        except (OSError, UnicodeDecodeError):
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM and pids:
            until = time.monotonic() + grace_sec
            while time.monotonic() < until and any(Path(f'/proc/{p}').exists() for p in pids):
                time.sleep(.2)
    return len(pids)


def ensure(editor, force=False):
    status = check(editor)
    print(f'[Unity認証] {status.detail}')
    near = status.deadline is None or status.deadline <= datetime.now(timezone.utc) + timedelta(days=env_float('UNITY_LICENSE_REFRESH_DAYS', 7))
    if force or not status.valid or near:
        try:
            status = refresh(editor, status)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            _report_failure(str(error))
            status = check(editor)
            if status.valid:
                print('[Unity認証] 自動更新失敗。有効な保存済みライセンスで続行します')
                return status
    if not status.valid:
        raise UnityLicenseError(status.detail + '。Unity Hub で再サインインしてください')
    _report_recovered()
    return status


def raise_if_license_error(log):
    try:
        text = Path(log).read_text(errors='replace')
    except OSError:
        return
    if any(marker in text for marker in ('No valid Unity Editor license found',
                "'com.unity.editor.ui' was not found", 'LicenseGroupOfflineValidityPeriodIsExpired')):
        raise UnityLicenseError(f'Unity ライセンス不良。起動を中止しました。ログ: {log}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--force-refresh', action='store_true')
    args = ap.parse_args()
    editor = os.getenv('UNITY_EXE', str(Path.home() / 'Unity/Hub/Editor/6000.0.76f1/Editor/Unity'))
    if args.check:
        status = check(editor)
        print(status)
        return 0 if status.valid else 1
    with open('/tmp/bottan-render.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[Unity認証] 録画・配信中のため定期更新を見送ります')
            return 0
        try:
            ensure(editor, force=args.force_refresh)
            return 0
        except UnityLicenseError as error:
            print(f'[Unity認証] {error}')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())

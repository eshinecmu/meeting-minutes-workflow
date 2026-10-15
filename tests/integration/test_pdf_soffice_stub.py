"""F5.9：假 soffice 独立进程执行门控 body；不是 LibreOffice 真转换证据。"""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('failure', ['', 'version', 'conversion', 'version_timeout', 'conversion_timeout'])
def test_gated_body_with_configured_executable(tmp_path, failure):
    # 模拟 Windows 含空格的安装目录；用解释器运行，macOS 也能覆盖挂起行为。
    executable = tmp_path / 'Program Files' / 'LibreOffice' / 'program' / 'soffice.exe'
    executable.parent.mkdir(parents=True)
    executable.write_text('''
import pathlib
import sys
import time
if '--version' in sys.argv:
    if FAILURE == 'version_timeout':
        time.sleep(60)
    print('F5.9 STUB ONLY, not LibreOffice')
    sys.exit(9 if FAILURE == 'version' else 0)
if FAILURE == 'conversion_timeout':
    time.sleep(60)
if FAILURE == 'conversion':
    sys.exit(9)
assert sys.argv[1:4] == ['--headless', '--convert-to', 'pdf']
source = pathlib.Path(sys.argv[-1])
assert source.read_bytes().startswith(b'PK')
# 完整的单页 PDF 对象、xref 与 trailer；仅验证管线，不证明同源排版。
objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
           b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
           b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>',
           b'<< /Length 0 >>\\nstream\\n\\nendstream']
pdf = b'%PDF-1.4\\n'
offsets = []
for i, obj in enumerate(objects, 1):
    offsets.append(len(pdf))
    pdf += f'{i} 0 obj\\n'.encode() + obj + b'\\nendobj\\n'
xref = len(pdf)
pdf += b'xref\\n0 5\\n0000000000 65535 f \\n'
for offset in offsets:
    pdf += f'{offset:010d} 00000 n \\n'.encode()
pdf += f'trailer\\n<< /Size 5 /Root 1 0 R >>\\nstartxref\\n{xref}\\n%%EOF\\n'.encode()
(pathlib.Path(sys.argv[5]) / source.with_suffix('.pdf').name).write_bytes(pdf)
'''.replace("import pathlib", f"FAILURE = {failure!r}\nimport pathlib"), encoding='utf-8')
    # 仅替换固定门控模块的配置加载结果；body、HTTP、G3 与 FileStore 原样运行。
    plugin = tmp_path / 'f59_stub_config.py'
    plugin.write_text(f'''
def pytest_configure(config):
    import subprocess
    import sys
    original = subprocess.run
    def run(command, *args, **kwargs):
        # 仅替换假程序启动方式；用解释器执行脚本，不依赖 shebang/chmod。
        if command and str(command[0]) == {str(executable)!r}:
            probe = '--version' in command
            assert kwargs['timeout'] == (15 if probe else 180)
            # 先断言生产限时，再缩短等待；真正启动并终止休眠子进程。
            if {failure!r} == ('version_timeout' if probe else 'conversion_timeout'):
                kwargs['timeout'] = 0.2
            command = [sys.executable, *command]
        try:
            return original(command, *args, **kwargs)
        except subprocess.TimeoutExpired:
            # API 只给用户清晰错误，不外泄内部异常；测试另记真实超时证据。
            phase = 'probe' if '--version' in command else 'PDF conversion'
            print(f'STUB {{phase}} TimeoutExpired', flush=True)
            raise
    subprocess.run = run


def pytest_collection_modifyitems(items):
    for item in items:
        module = item.module
        if module.__name__ == 'test_pdf_soffice':
            original = module.load_settings
            def configured(*args, **kwargs):
                from pathlib import Path
                cfg = original(*args, **kwargs)
                return cfg.model_copy(update={{'export': cfg.export.model_copy(update={{'soffice_path': Path({str(executable)!r})}})}})
            module.load_settings = configured
''', encoding='utf-8')
    env = dict(os.environ)
    env.pop('PYTEST_ADDOPTS', None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env['PYTHONPATH'] = os.pathsep.join((str(tmp_path), str(ROOT / 'tools'), str(ROOT / 'src')))
    command = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-s', '-ra',
               '-p', 'no:cacheprovider', '-p', 'accept_f5_9', '-p', 'f59_stub_config',
               '--basetemp', str(tmp_path / 'data'), 'tests/integration/test_pdf_soffice.py']
    result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    print('STUB 命令：' + shlex.join(command))
    print(result.stdout)
    assert '1 skipped' not in result.stdout
    if failure:
        assert result.returncode != 0 and ('1 failed' in result.stdout or '1 error' in result.stdout)
        if failure == 'version_timeout':
            assert 'TimeoutExpired' in result.stdout and '1 error' in result.stdout
        if failure == 'conversion_timeout':
            assert 'STUB PDF conversion TimeoutExpired' in result.stdout
            assert "'code': 'E5003'" in result.stdout and '1 failed' in result.stdout
    else:
        assert result.returncode == 0, result.stdout
        assert '1 passed' in result.stdout and 'F5.9 STUB ONLY' in result.stdout
        assert 'DOCX SHA256：' in result.stdout and 'PDF SHA256：' in result.stdout

"""F5.9：假 soffice 独立进程执行门控 body；不是 LibreOffice 真转换证据。"""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('failure', ['', 'version', 'conversion'])
def test_gated_body_with_configured_executable(tmp_path, failure):
    executable = tmp_path / 'soffice-stub'
    executable.write_text(f'#!{sys.executable}\n' + '''
import pathlib
import sys
if '--version' in sys.argv:
    print('F5.9 STUB ONLY, not LibreOffice')
    sys.exit(9 if FAILURE == 'version' else 0)
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
    executable.chmod(0o700)
    # 仅替换固定门控模块的配置加载结果；body、HTTP、G3 与 FileStore 原样运行。
    plugin = tmp_path / 'f59_stub_config.py'
    plugin.write_text(f'''
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
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print('STUB 命令：' + shlex.join(command))
    print(result.stdout)
    assert '1 skipped' not in result.stdout
    if failure:
        assert result.returncode != 0 and ('1 failed' in result.stdout or '1 error' in result.stdout)
    else:
        assert result.returncode == 0, result.stdout
        assert '1 passed' in result.stdout and 'F5.9 STUB ONLY' in result.stdout
        assert 'DOCX SHA256：' in result.stdout and 'PDF SHA256：' in result.stdout

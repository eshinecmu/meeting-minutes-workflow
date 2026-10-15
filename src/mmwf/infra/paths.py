"""持久化及对外的相对路径统一使用 POSIX 分隔符。"""
from pathlib import PurePath, PurePosixPath, PureWindowsPath


def relative_posix(path: str | PurePath) -> str:
    """兼容宿主路径、Windows 路径对象及旧登记中的反斜杠。

    这里只统一表示，不做授权或安全校验；文件访问仍须经过仓储的 resolve。
    仓储相对引用把两种分隔符视为同义：即使宿主是 POSIX，字符串中的反斜杠
    也表示目录，不支持把反斜杠当作文件名的普通字符。这样旧 Windows 登记
    能跨平台读取，且 write/resolve 始终定位同一目标；不应用于任意本地文件名。
    空引用保持为空，避免被 PurePath 转成当前目录。
    """
    if not str(path):
        return ""
    pure = PureWindowsPath(path) if "\\" in str(path) else PurePosixPath(path)
    return pure.as_posix()


def pdf_bindings(entries: dict) -> dict:
    """PDF 来源登记：映射键和来源引用必须使用同一种路径表示。"""
    return {relative_posix(rel): ({**entry,
        'source_docx_path': relative_posix(entry['source_docx_path'])}
        if isinstance(entry, dict) and isinstance(entry.get('source_docx_path'), str)
        else entry) for rel, entry in entries.items()}


def export_paths(paths: dict) -> dict:
    """归档、图状态及幂等输入共用的交付引用；不改正文或哈希。"""
    result = dict(paths)
    for key in ('docx', 'filename'):
        if result.get(key):
            result[key] = relative_posix(result[key])
    if isinstance(result.get('doc'), dict):
        result['doc'] = dict(result['doc'])
        if result['doc'].get('rel_path'):
            result['doc']['rel_path'] = relative_posix(result['doc']['rel_path'])
    if isinstance(result.get('pdf'), dict):
        result['pdf'] = pdf_bindings(result['pdf'])
    if isinstance(result.get('artifact_sha256'), dict):
        result['artifact_sha256'] = {relative_posix(k): v for k, v in result['artifact_sha256'].items()}
    return result

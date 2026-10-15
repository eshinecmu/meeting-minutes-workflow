"""领域层：纯 Python 业务逻辑，禁止框架依赖（SAD C01）。

本层只依赖标准库与纯算法依赖。`python tools/check_layers.py` 会静态
检查本目录下不得出现 fastapi / sqlite3 / httpx / torch / funasr / ollama 等。

模块导航：
    errors       —— 统一错误码与业务异常（SAD §9.3），全项目共用
    asr_types    —— 转写结果的数据类型与三条不变量（T1-05）
    glossary     —— 术语库匹配与专名纠正（T1-07）
    extraction   —— 分块分层抽取（T1-08）
    minutes      —— 纪要结构化与版式映射（T1-09）
    grounding    —— 逐句溯源与无依据结论拦截（C04）
    docx         —— GB/T 9704-2012 版式规则（T1-09）
"""

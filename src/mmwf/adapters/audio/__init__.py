"""音频处理适配器（T1-06）。

把"外部音频工具"封在这里，上层只依赖本包的接口：

    ffmpeg      —— ffmpeg / ffprobe 的定位与调用
                  （时长探测、切分、统一 16kHz 单声道 wav）

为什么必须有这一层
------------------
`subprocess` 与"文件路径拼进命令行"是外部输入进入系统的入口。
集中在适配层的好处：

  · 所有 ffmpeg 失败都在这里有统一翻译（E1002/E1003），不会漏出裸异常；
  · `check_layers.py` 保证领域层碰不到 subprocess；
  · 将来换成别的工具（比如某种纯 Python 解码栈）只需换这个包。
"""

from mmwf.adapters.audio.ffmpeg import (
    DEFAULT_TIMEOUT_SECONDS,
    FFmpegAdapter,
    FFmpegNotFoundError,
    FFmpegTools,
    locate,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FFmpegAdapter",
    "FFmpegNotFoundError",
    "FFmpegTools",
    "locate",
]

"""
通用产出物收集器 — 改进版
三源合一：工具返回值解析 + 管家自我报告 + 磁盘兜底扫描

改进点：
1. 时间窗口放宽（-3秒缓冲 + >= 而非 >）
2. 未知扩展名大文件也收集
3. 自动探测 AstrBot 数据目录
4. 扫描日志输出，方便排查漏扫
5. 增加更多路径提取模式（JSON key-value、data/ 相对路径、/tmp 路径）
6. 扩展名白名单补充
7. 主动报告的文件无条件接受，扫描发现的文件仅接受已知媒体类型（防止 .db-wal 等垃圾文件）
"""
import os
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("astrbot_plugin_transparent_maid")

# ============================================================
# 通用媒体类型映射 — 新增类型只需在此加一行
# ============================================================
MEDIA_EXTENSIONS = {
    # 图片
    '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
    '.gif': 'image', '.bmp': 'image', '.svg': 'image', '.tiff': 'image',
    '.psd': 'image', '.ico': 'image', '.heic': 'image', '.heif': 'image',
    # 音频
    '.mp3': 'audio', '.wav': 'audio', '.ogg': 'audio', '.m4a': 'audio',
    '.aac': 'audio', '.flac': 'audio', '.wma': 'audio', '.opus': 'audio',
    '.silk': 'audio', '.amr': 'audio',
    # 视频
    '.mp4': 'video', '.avi': 'video', '.mkv': 'video', '.mov': 'video',
    '.webm': 'video', '.flv': 'video',
    # 文档
    '.pdf': 'document', '.doc': 'document', '.docx': 'document',
    '.txt': 'document', '.csv': 'document', '.xlsx': 'document',
    # 常见产出格式补充
    '.md': 'document', '.json': 'document', '.html': 'document',
    '.htm': 'document', '.xml': 'document', '.zip': 'document',
    '.tar': 'document', '.gz': 'document', '.7z': 'document',
}

IMAGE_EXTS = {ext for ext, t in MEDIA_EXTENSIONS.items() if t == 'image'}
AUDIO_EXTS = {ext for ext, t in MEDIA_EXTENSIONS.items() if t == 'audio'}
VIDEO_EXTS = {ext for ext, t in MEDIA_EXTENSIONS.items() if t == 'video'}
DOC_EXTS = {ext for ext, t in MEDIA_EXTENSIONS.items() if t == 'document'}

# 时间缓冲：允许比 task_start_time 早这么多秒的文件也被收集
_MTIME_BUFFER_SECONDS = 3

# 未知文件收集的大小阈值（字节），大于此值的未知类型文件也会收集（已废弃，现在扫描时只接受已知类型）
_UNKNOWN_FILE_SIZE_THRESHOLD = 2048

# 自动探测的 AstrBot 数据根目录候选
_ASTRBOT_DATA_ROOTS = [
    "/AstrBot/data",
    "./data",
]

# 需要跳过的目录名（不递归进入）
_SKIP_DIRS = {
    '.', '__pycache__', 'node_modules', '.git', '.venv', 'venv',
    '.cache', 'cache', '.idea', '.vscode',
}


@dataclass
class CollectedMedia:
    """收集到的媒体文件"""
    images: list = field(default_factory=list)
    audios: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    _seen: set = field(default_factory=set)

    def add(self, fpath: str, force_accept: bool = False) -> bool:
        """添加一个文件路径，自动分类。返回是否为新文件
        
        Args:
            fpath: 文件绝对路径
            force_accept: 是否强制接受（绕过扩展名检查）。True 时无论扩展名都接受。
                          用于主动报告（report_done / parse_tool_result）的文件。
                          磁盘扫描时应设为 False，只接受已知媒体类型。
        """
        if not fpath:
            return False
        # 标准化路径
        fpath = os.path.abspath(fpath)
        if fpath in self._seen:
            return False
        if not os.path.isfile(fpath):
            return False
        self._seen.add(fpath)

        ext = Path(fpath).suffix.lower()
        media_type = MEDIA_EXTENSIONS.get(ext)

        # 如果不是已知媒体类型，且不是强制接受，则拒绝
        if not force_accept and media_type is None:
            logger.debug(f"[文件收集] 忽略未知扩展名文件（非强制接受）: {fpath}")
            return False

        if media_type == 'image':
            self.images.append(fpath)
        elif media_type == 'audio':
            self.audios.append(fpath)
        elif media_type == 'video':
            self.videos.append(fpath)
        elif media_type == 'document':
            self.documents.append(fpath)
        elif force_accept:
            # 强制接受但扩展名未知 → 归入 documents
            self.documents.append(fpath)
            logger.debug(f"[文件收集] 强制接受未知类型文件: {fpath}")
        else:
            # 理论上不会走到这里，因为上面已经 return False
            return False
        return True

    @property
    def has_anything(self) -> bool:
        return bool(self.images or self.audios or self.videos or self.documents)

    @property
    def total_count(self) -> int:
        return len(self.images) + len(self.audios) + len(self.videos) + len(self.documents)

    def summary(self) -> str:
        parts = []
        if self.images:    parts.append(f"{len(self.images)}图")
        if self.audios:    parts.append(f"{len(self.audios)}音")
        if self.videos:    parts.append(f"{len(self.videos)}视")
        if self.documents: parts.append(f"{len(self.documents)}档")
        return "+".join(parts) if parts else "空"

    def log_summary(self):
        if not self.has_anything:
            logger.info("[铃兰女仆] 📦 收集结果：无产出文件")
            return
        logger.info(f"[铃兰女仆] 📦 收集结果：{self.summary()}")
        if self.images:    logger.info(f"[铃兰女仆] 🖼️ 图片: {[os.path.basename(f) for f in self.images]}")
        if self.audios:    logger.info(f"[铃兰女仆] 🎵 音频: {[os.path.basename(f) for f in self.audios]}")
        if self.videos:    logger.info(f"[铃兰女仆] 🎬 视频: {[os.path.basename(f) for f in self.videos]}")
        if self.documents: logger.info(f"[铃兰女仆] 📄 文档: {[os.path.basename(f) for f in self.documents]}")


class MediaCollector:
    """三源合一产出物收集器（改进版）

    源1: parse_tool_result — 从工具返回值中正则提取文件路径（force_accept=True）
    源2: add_reported_files — 管家通过 report_done(files=...) 报告的路径（force_accept=True）
    源3: scan_disk — 磁盘兜底扫描，只接受已知媒体类型（force_accept=False）
    """

    def __init__(self, task_start_time: float, scan_dirs: list = None):
        self.task_start_time = task_start_time
        self.collected = CollectedMedia()
        # 自动探测有效扫描目录
        self.scan_dirs = self._resolve_scan_dirs(scan_dirs)

    @staticmethod
    def _resolve_scan_dirs(scan_dirs: list | None) -> list[str]:
        """解析并验证扫描目录，自动补全候选路径"""
        dirs = list(scan_dirs) if scan_dirs else []

        # 自动补全候选路径
        for candidate in _ASTRBOT_DATA_ROOTS:
            abs_candidate = os.path.abspath(candidate)
            if abs_candidate not in dirs and os.path.isdir(abs_candidate):
                dirs.append(abs_candidate)

        # 去重 + 过滤不存在的目录
        seen = set()
        valid = []
        for d in dirs:
            abs_d = os.path.abspath(d)
            if abs_d in seen:
                continue
            seen.add(abs_d)
            if os.path.isdir(abs_d):
                valid.append(abs_d)
            else:
                logger.debug(f"[文件收集] 扫描目录不存在，跳过: {abs_d}")

        logger.debug(f"[文件收集] 最终扫描目录: {valid}")
        return valid

    def parse_tool_result(self, tool_name: str, tool_result) -> None:
        """源1：从工具返回值中提取文件路径（强制接受）"""
        if not tool_result:
            return
        result_str = str(tool_result)

        # 模式1: IMAGE_PATH: /path / AUDIO_PATH: /path / FILE_PATH: /path 等
        for keyword in ('IMAGE_PATH', 'AUDIO_PATH', 'FILE_PATH', 'VIDEO_PATH',
                        'DOCUMENT_PATH'):
            for match in re.finditer(rf'{keyword}:\s*(\S+)', result_str):
                self.collected.add(match.group(1).strip(), force_accept=True)

        # 模式2: 带引号的路径（工具返回 JSON 中的路径字段）
        all_exts_joined = '|'.join(re.escape(ext) for ext in MEDIA_EXTENSIONS.keys())
        for keyword in ('path', 'file_path', 'image_path', 'audio_path',
                        'video_path', 'output_path', 'output', 'file',
                        'result_path', 'save_path', 'url'):
            # 匹配 "key": "/some/path.xxx" 或 "key": "C:\some\path.xxx"
            for match in re.finditer(
                rf'"{keyword}"\s*:\s*"([^"]+\.(?:{all_exts_joined}))"',
                result_str, re.IGNORECASE
            ):
                self.collected.add(match.group(1).strip(), force_accept=True)

        # 模式3: 通用路径匹配 — 包含已知媒体扩展名的绝对路径
        path_pattern = rf'(?:(?:/[^\s"\'<>]+)|(?:[A-Z]:\\[^\s"\'<>]+))\.(?:{all_exts_joined})'
        for match in re.finditer(path_pattern, result_str, re.IGNORECASE):
            self.collected.add(match.group(0).strip(), force_accept=True)

        # 模式4: data/ 开头的相对路径（AstrBot 常见格式）
        for match in re.finditer(
            rf'(data/[^\s"\'<>\)]+\.(?:{all_exts_joined}))',
            result_str, re.IGNORECASE
        ):
            rel_path = match.group(0).strip()
            abs_path = os.path.abspath(rel_path)
            if os.path.isfile(abs_path):
                self.collected.add(abs_path, force_accept=True)

        # 模式5: /tmp 开头的临时文件路径
        for match in re.finditer(
            rf'(/tmp/[^\s"\'<>\)]+\.\w+)',
            result_str
        ):
            abs_path = match.group(0).strip()
            if os.path.isfile(abs_path):
                self.collected.add(abs_path, force_accept=True)

    def add_reported_files(self, files: list) -> None:
        """源2：管家通过 report_done 报告的文件路径（强制接受）"""
        if not files:
            return
        for f in files:
            self.collected.add(f, force_accept=True)

    def scan_disk(self) -> None:
        """源3：磁盘兜底扫描（只接受已知媒体类型，拒绝未知扩展名）"""
        if not self.scan_dirs:
            logger.warning("[文件收集] ⚠️ 无可用扫描目录")
            return

        before_count = self.collected.total_count
        cutoff_time = self.task_start_time - _MTIME_BUFFER_SECONDS

        scanned_dirs = 0
        scanned_files = 0

        for scan_dir in self.scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            try:
                for root, dirs, files in os.walk(scan_dir):
                    # 跳过不必要的目录
                    dirs[:] = [
                        d for d in dirs
                        if d not in _SKIP_DIRS and not d.startswith('.')
                    ]

                    for fname in files:
                        fpath = os.path.join(root, fname)
                        scanned_files += 1
                        try:
                            mtime = os.path.getmtime(fpath)
                            # 关键改进：>= 而非 >，且有缓冲窗口
                            if mtime >= cutoff_time:
                                ext = Path(fname).suffix.lower()
                                # 只接受已知媒体扩展名，其他一律忽略（force_accept=False）
                                if ext in MEDIA_EXTENSIONS:
                                    self.collected.add(fpath, force_accept=False)
                                    logger.debug(
                                        f"[文件收集] 磁盘发现新文件: {fpath} "
                                        f"(mtime={mtime:.1f}, cutoff={cutoff_time:.1f})"
                                    )
                                else:
                                    # 未知扩展名：直接忽略
                                    logger.debug(
                                        f"[文件收集] 磁盘忽略未知类型文件: {fpath} ({ext})"
                                    )
                        except OSError:
                            continue
                scanned_dirs += 1
            except OSError:
                continue

        # 扫描统计日志
        new_count = self.collected.total_count - before_count
        logger.debug(
            f"[文件收集] 磁盘扫描完成: 扫描{scanned_dirs}个目录、"
            f"{scanned_files}个文件, 新增{new_count}个产出文件, "
            f"时间窗口>cutoff={cutoff_time:.1f}"
        )

    def finalize(self) -> CollectedMedia:
        """完成收集（含磁盘扫描），返回结果"""
        self.scan_disk()
        self.collected.log_summary()
        return self.collected
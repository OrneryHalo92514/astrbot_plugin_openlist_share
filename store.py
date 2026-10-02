"""上传记录持久化：记录每个文件的上传者信息，供查询时展示。

数据存储于 AstrBot 的 data/plugin_data/<plugin_name>/upload_records.json。
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.utils.io import ensure_dir


class RecordStore:
    """上传记录存取。"""

    def __init__(self, plugin_name: str):
        self.data_dir = Path(StarTools.get_data_dir(plugin_name))
        self.records_file = self.data_dir / "upload_records.json"
        ensure_dir(self.data_dir)

    def _load(self) -> List[Dict]:
        try:
            if self.records_file.exists():
                data = json.loads(self.records_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning(f"读取上传记录失败: {e}")
        return []

    def _save(self, records: List[Dict]) -> None:
        try:
            self.records_file.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存上传记录失败: {e}")

    def add(self, record: Dict) -> None:
        """新增或按路径覆盖一条记录。"""
        records = self._load()
        path = record.get("path", "")
        records = [r for r in records if r.get("path") != path]
        records.append(record)
        self._save(records)

    def find(self, path: str) -> Optional[Dict]:
        """按文件完整路径查找记录。"""
        for r in self._load():
            if r.get("path") == path:
                return r
        return None

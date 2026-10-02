"""OpenList API 客户端。

OpenList 为 AList 的社区分支，API 与 AList 兼容：
- 登录    POST /api/auth/login          -> {"code": 200, "data": {"token": ...}}
- 列表    POST /api/fs/list             -> data.content: [{name, is_dir, size, ...}]
- 信息    POST /api/fs/get              -> data: {name, is_dir, size, raw_url, sign, ...}
- 搜索    POST /api/fs/search
- 上传    PUT  /api/fs/put              头部 File-Path 指定目标路径
- 建目录  POST /api/fs/mkdir
"""

import posixpath
from typing import Dict, List, Optional
from urllib.parse import quote

import aiohttp

from astrbot.api import logger


class OpenlistAuthError(Exception):
    """OpenList 登录/认证失败。"""


class OpenlistClient:
    """OpenList API 客户端。建议使用 async with 上下文管理器创建。"""

    def __init__(
        self,
        base_url: str,
        public_base_url: str = "",
        username: str = "",
        password: str = "",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.public_base_url = (public_base_url or "").rstrip("/") if public_base_url else ""
        self.username = username or ""
        self.password = password or ""
        self.token = ""
        self._session: Optional[aiohttp.ClientSession] = None

    # ---------------- 会话管理 ----------------

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        if self.username and self.password and not self.token:
            if not await self.login():
                raise OpenlistAuthError("OpenList 登录失败，请检查插件设置中的用户名和密码")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _headers(self) -> Dict[str, str]:
        headers = {}
        if self.token:
            headers["Authorization"] = self.token
        return headers

    def _norm(self, path: str) -> str:
        """标准化 OpenList 路径。"""
        raw = (path or "").strip().replace("\\", "/")
        if not raw:
            return "/"
        if not raw.startswith("/"):
            raw = "/" + raw
        normalized = posixpath.normpath(raw)
        if normalized in ("", "."):
            return "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized.lstrip("/")
        return normalized

    def _with_public(self, url: str) -> str:
        """把内网地址替换为公网地址（若配置且匹配）。"""
        if (
            self.public_base_url
            and url.startswith(self.base_url + "/")
            and len(url) > len(self.base_url)
        ):
            return self.public_base_url + url[len(self.base_url):]
        return url

    # ---------------- 认证 ----------------

    async def login(self) -> bool:
        """登录并获取 token。"""
        try:
            async with self.session.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self.username, "password": self.password},
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        self.token = result.get("data", {}).get("token", "")
                        return bool(self.token)
                    logger.error(
                        f"OpenList 登录失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}"
                    )
                    return False
                logger.error(f"OpenList 登录失败 - HTTP 状态: {resp.status}")
                return False
        except Exception as e:
            logger.error(f"OpenList 登录失败: {e}")
            return False

    # ---------------- 文件操作 ----------------

    async def list_files(
        self, path: str = "/", page: int = 1, per_page: int = 50
    ) -> Optional[Dict]:
        """获取目录下的文件列表。"""
        try:
            async with self.session.post(
                f"{self.base_url}/api/fs/list",
                json={
                    "path": self._norm(path),
                    "password": "",
                    "page": page,
                    "per_page": per_page,
                    "refresh": False,
                },
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    logger.error(
                        f"获取文件列表失败 - code: {result.get('code')}, "
                        f"message: {result.get('message', '未知错误')}, 路径: {path}"
                    )
                    return None
                logger.error(f"获取文件列表失败 - HTTP {resp.status}, 路径: {path}")
                return None
        except Exception as e:
            logger.error(f"获取文件列表失败: {e}, 路径: {path}")
            return None

    async def get_file_info(self, path: str) -> Optional[Dict]:
        """获取单个文件/目录的信息。"""
        try:
            async with self.session.post(
                f"{self.base_url}/api/fs/get",
                json={"path": self._norm(path), "password": ""},
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        return result.get("data")
                    logger.error(
                        f"获取文件信息失败 - code: {result.get('code')}, "
                        f"message: {result.get('message', '未知错误')}, 路径: {path}"
                    )
                    return None
                logger.error(f"获取文件信息失败 - HTTP {resp.status}, 路径: {path}")
                return None
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}, 路径: {path}")
            return None

    async def search_files(
        self, keyword: str, path: str = "/", per_page: int = 50
    ) -> List[Dict]:
        """按关键词搜索文件。"""
        try:
            async with self.session.post(
                f"{self.base_url}/api/fs/search",
                json={
                    "parent": self._norm(path),
                    "keywords": keyword,
                    "scope": 0,
                    "page": 1,
                    "per_page": per_page,
                },
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200:
                        content = result.get("data", {}).get("content")
                        return content if content is not None else []
                return []
        except Exception as e:
            logger.error(f"搜索文件失败: {e}, 关键词: {keyword}")
            return []

    async def mkdir(self, path: str) -> bool:
        """创建目录（目录已存在时也视为成功）。"""
        try:
            async with self.session.post(
                f"{self.base_url}/api/fs/mkdir",
                json={"path": self._norm(path)},
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get("code") == 200 or result.get("code") == 405:
                        return True
                return False
        except Exception as e:
            logger.error(f"创建目录失败: {e}, 路径: {path}")
            return False

    async def ensure_dir(self, path: str) -> bool:
        """逐级创建目录。"""
        normalized = self._norm(path)
        if normalized == "/":
            return True
        current = ""
        for part in normalized.strip("/").split("/"):
            current = f"{current}/{part}"
            if not await self.mkdir(current):
                return False
        return True

    async def upload_file(self, local_path: str, target_dir: str, filename: str) -> bool:
        """上传本地文件到 OpenList 的 target_dir 目录下。"""
        from pathlib import Path

        local = Path(local_path)
        if not local.exists():
            logger.error(f"上传文件不存在: {local_path}")
            return False
        file_size = local.stat().st_size
        target_path = f"{self._norm(target_dir).rstrip('/')}/{filename}"
        encoded_path = quote(target_path, safe="/")
        headers = {"File-Path": encoded_path}
        if self.token:
            headers["Authorization"] = self.token

        try:
            with local.open("rb") as file_obj:
                async with self.session.put(
                    f"{self.base_url}/api/fs/put",
                    data=file_obj,
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("code") == 200:
                            logger.info(f"OpenList 上传成功: {target_path} ({file_size}B)")
                            return True
                        logger.error(
                            f"上传失败 - code: {result.get('code')}, message: {result.get('message', '未知错误')}"
                        )
                        return False
                    logger.error(f"上传失败 - HTTP 状态: {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"OpenList PUT 请求失败: {e}, 目标: {target_path}")
            return False

    async def get_share_link(self, path: str) -> Optional[str]:
        """获取文件的分享（直链）链接；目录返回 None。"""
        info = await self.get_file_info(path)
        if not info or info.get("is_dir", True):
            return None
        raw_url = info.get("raw_url")
        if raw_url:
            return self._with_public(raw_url)
        sign = info.get("sign")
        encoded = quote(self._norm(path).encode("utf-8"))
        base = self.public_base_url or self.base_url
        if sign:
            return f"{base}/d{encoded}?sign={sign}"
        return f"{base}/d{encoded}"

"""OpenList 共享插件 - AstrBot

功能：
- 上传：群成员引用（回复）一条文件消息并发送 /ol up [目录]，插件会先交给 LLM 审核，
  审核通过后上传到 OpenList，文件名备注上传者信息，并在群里 @ 上传者告知结果。
- 查询：/ol ls 列出文件并返回分享链接（不下载文件本体），支持搜索与详情。
- 权限：插件设置中可配置允许使用的群（多群）；OpenList 用户名/密码等敏感信息只存
  在插件设置里，绝不会在群聊中展示。

灵感与 API 参考：https://github.com/Foolllll-J/astrbot_plugin_openlistfile
"""

import asyncio
import json
import posixpath
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Plain, Reply, Video
from astrbot.api.star import Context, Star

from .client import OpenlistAuthError, OpenlistClient
from .store import RecordStore

# 适合送给 LLM 审核内容预览的文本类扩展名
TEXT_PREVIEW_EXTS = {
    ".txt", ".md", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf",
    ".cfg", ".toml", ".py", ".js", ".java", ".c", ".cpp", ".h", ".go", ".rs",
    ".php", ".rb", ".sh", ".bash", ".html", ".htm", ".css", ".sql", ".csv",
    ".properties", ".env", ".ts", ".vue", ".jsx",
}

# 默认 LLM 审核系统提示词（可在插件设置中覆盖）
DEFAULT_REVIEW_SYSTEM_PROMPT = (
    "你是一个网盘文件上传审核员。你会收到待上传文件的信息（文件名、大小、类型、"
    "上传者）以及可能的部分文本内容预览。请判断该文件是否适合上传到共享网盘，"
    "主要检查：\n"
    "1. 是否包含违法、色情、暴力、恐怖、赌博、毒品等违禁内容；\n"
    "2. 是否包含明显的恶意代码、病毒、木马、钓鱼脚本；\n"
    "3. 文件名或内容是否包含诈骗、侵权、泄露隐私等不当信息；\n"
    "4. 文本预览是否包含敏感或违规内容。\n"
    "请只输出一行 JSON，格式为："
    '{"approved": true或false, "reason": "简短原因"}。approved 为 true 表示允许上传。'
)

DEFAULT_UPLOAD_PATH = "/"


def _fmt_size(size: Optional[int]) -> str:
    """格式化文件大小。"""
    if size is None:
        return "未知"
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "未知"
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    return f"{size / (1024 * 1024 * 1024):.2f}GB"


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "")
    return cleaned.strip(" .") or "file"


class OpenListSharePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.plugin_name = "astrbot_plugin_openlist_share"
        self.records = RecordStore(self.plugin_name)
        self._upload_sem = asyncio.Semaphore(3)

    # ================= 配置读取 =================

    def _c(self, section: str, key: str, default=None):
        try:
            sec = self.config.get(section)
            if isinstance(sec, dict):
                return sec.get(key, default)
        except Exception:
            pass
        return default

    def _openlist_url(self) -> str:
        return str(self._c("connection", "openlist_url", "") or "").strip()

    def _public_url(self) -> str:
        return str(self._c("connection", "public_openlist_url", "") or "").strip()

    def _username(self) -> str:
        return str(self._c("connection", "username", "") or "").strip()

    def _password(self) -> str:
        return str(self._c("connection", "password", "") or "").strip()

    def _allowed_groups(self) -> List[str]:
        raw = self._c("permission", "allowed_groups", []) or []
        return [str(g).strip() for g in raw if str(g).strip()]

    def _allow_private(self) -> bool:
        return bool(self._c("permission", "allow_private_chat", False))

    def _default_upload_path(self) -> str:
        return self._norm_path(self._c("upload", "default_upload_path", DEFAULT_UPLOAD_PATH))

    def _max_upload_size_mb(self) -> int:
        try:
            return max(0, int(self._c("upload", "max_upload_size_mb", 100)))
        except (TypeError, ValueError):
            return 100

    def _allowed_extensions(self) -> List[str]:
        raw = str(self._c("upload", "allowed_extensions", "") or "").strip()
        return [e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()]

    def _rename_enabled(self) -> bool:
        return bool(self._c("upload", "rename_with_uploader", True))

    def _rename_template(self) -> str:
        return str(self._c("upload", "rename_template", "[{uploader}]{filename}") or "")

    def _review_enabled(self) -> bool:
        return bool(self._c("llm_review", "enable", True))

    def _review_provider(self) -> str:
        return str(self._c("llm_review", "provider", "") or "").strip()

    def _review_system_prompt(self) -> str:
        p = str(self._c("llm_review", "system_prompt", "") or "").strip()
        if not p or p == "<default>":
            return DEFAULT_REVIEW_SYSTEM_PROMPT
        return p

    def _review_preview_chars(self) -> int:
        try:
            return max(0, int(self._c("llm_review", "text_preview_chars", 2000)))
        except (TypeError, ValueError):
            return 2000

    def _max_list_items(self) -> int:
        try:
            return max(1, min(100, int(self._c("display", "max_list_items", 30))))
        except (TypeError, ValueError):
            return 30

    # ================= 通用工具 =================

    def _norm_path(self, path: str) -> str:
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

    def _join_path(self, parent: str, name: str) -> str:
        parent = self._norm_path(parent)
        name = (name or "").strip()
        if parent == "/":
            return f"/{name}"
        return f"{parent.rstrip('/')}/{name}"

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    def _check_permission(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        """校验当前会话是否允许使用插件。返回 (是否允许, 拒绝原因)。"""
        group_id = self._get_group_id(event)
        if group_id:
            allowed = self._allowed_groups()
            if allowed and group_id not in allowed:
                return False, "❌ 本群未授权使用该插件，请联系管理员在插件设置中添加群号。"
            return True, ""
        if self._allow_private():
            return True, ""
        return False, "❌ 本插件仅限授权群聊使用。"

    def _get_sender_name(self, event: AstrMessageEvent) -> str:
        """获取发送者昵称（带兜底）。"""
        try:
            name = event.get_sender_name()
            if name:
                return str(name)
        except Exception:
            pass
        try:
            return str(event.get_sender_id())
        except Exception:
            return "未知用户"

    def _create_client(self) -> OpenlistClient:
        return OpenlistClient(
            base_url=self._openlist_url(),
            public_base_url=self._public_url(),
            username=self._username(),
            password=self._password(),
        )

    def _extract_quoted_components(self, event: AstrMessageEvent) -> List:
        """从引用（回复）消息中提取文件/图片/视频组件。

        用户「引用文件 + 发送 /ol up」时，被引用的消息内容在 Reply.chain 中。
        若当前消息直接携带文件组件，也一并支持。
        """
        components: List = []
        for msg in event.get_messages():
            if isinstance(msg, Reply):
                chain = getattr(msg, "chain", None) or []
                for comp in chain:
                    if isinstance(comp, (File, Image, Video)):
                        components.append(comp)
        if not components:
            for comp in event.get_messages():
                if isinstance(comp, (File, Image, Video)):
                    components.append(comp)
        return components

    async def _download_component(self, comp) -> Optional[str]:
        """把文件组件下载到本地，返回本地路径；失败返回 None。"""
        try:
            if isinstance(comp, File):
                return await comp.get_file()
            if isinstance(comp, (Image, Video)):
                return await comp.convert_to_file_path()
        except Exception as e:
            logger.error(f"下载文件组件失败: {e}")
        return None

    def _resolve_filename(self, comp, local_path: str) -> str:
        """从组件或本地路径解析出原始文件名。"""
        if isinstance(comp, File):
            name = (getattr(comp, "name", "") or "").strip()
            file_ = (getattr(comp, "file_", "") or "").strip()
            if not name and file_:
                name = Path(file_).name
            if name:
                return name
        if local_path:
            return Path(local_path).name
        return f"file_{int(time.time())}"

    # ================= LLM 审核 =================

    def _parse_review_result(self, text: str) -> Tuple[bool, str]:
        """解析 LLM 审核结果 JSON。解析失败时默认拒绝（安全侧）。"""
        if not text:
            return False, "审核结果为空"
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return False, "审核结果格式无法解析"
        try:
            data = json.loads(match.group(0))
            approved = bool(data.get("approved", False))
            reason = str(data.get("reason", "") or "").strip() or ("通过" if approved else "未通过")
            return approved, reason
        except Exception:
            return False, "审核结果格式无法解析"

    async def _llm_review(
        self, event: AstrMessageEvent, filename: str, file_size: int, preview: str
    ) -> Tuple[bool, str]:
        """调用 LLM 审核文件。返回 (是否通过, 原因)。"""
        try:
            provider_id = self._review_provider()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            if not provider_id:
                return False, "LLM 审核模型不可用，请联系管理员检查配置"

            system_prompt = self._review_system_prompt()
            user_prompt = (
                f"文件名：{filename}\n"
                f"大小：{_fmt_size(file_size)}\n"
                f"类型：{Path(filename).suffix.lower() or '未知'}\n"
                f"上传者：{self._get_sender_name(event)}\n"
                f"目标目录：{self._default_upload_path()}"
            )
            if preview:
                user_prompt += f"\n\n文件内容预览（前 {len(preview)} 字）：\n{preview}"
            user_prompt += (
                '\n\n请只输出一行 JSON：{"approved": true或false, "reason": "简短原因"}'
            )

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = getattr(llm_resp, "completion_text", "") or ""
            return self._parse_review_result(text)
        except Exception as e:
            logger.error(f"LLM 审核调用失败: {e}")
            return False, f"LLM 审核调用出错：{e}"

    # ================= 上传流程 =================

    async def _process_upload(self, event: AstrMessageEvent, comp, target_path: str):
        """单个文件的审核 + 上传流程。"""
        user_id = event.get_sender_id()
        uploader = self._get_sender_name(event)

        yield event.plain_result("📋 已收到文件，正在审核，请稍候……")

        # 1. 下载文件
        local_path = await self._download_component(comp)
        if not local_path or not Path(local_path).exists():
            yield event.plain_result("❌ 无法获取文件，请重新发送。")
            return

        try:
            filename = self._resolve_filename(comp, local_path)
            file_size = Path(local_path).stat().st_size

            # 2. 扩展名 / 大小限制
            ext = Path(filename).suffix.lower().lstrip(".")
            allowed = self._allowed_extensions()
            if allowed and ext and ext not in allowed:
                yield event.chain_result([
                    At(qq=user_id),
                    Plain(f"\n❌ 文件类型不允许上传：{filename}\n允许的扩展名：{', '.join(allowed)}"),
                ])
                return
            max_size = self._max_upload_size_mb()
            if max_size > 0 and file_size > max_size * 1024 * 1024:
                yield event.chain_result([
                    At(qq=user_id),
                    Plain(
                        f"\n❌ 文件过大：{_fmt_size(file_size)} > 上限 {max_size}MB"
                    ),
                ])
                return

            # 3. LLM 审核
            preview = ""
            if (
                self._review_preview_chars() > 0
                and Path(filename).suffix.lower() in TEXT_PREVIEW_EXTS
                and file_size <= 64 * 1024  # 仅预览 64KB 以内的文本
            ):
                try:
                    preview = Path(local_path).read_text(
                        encoding="utf-8", errors="ignore"
                    )[ : self._review_preview_chars()]
                except Exception:
                    preview = ""

            if self._review_enabled():
                approved, reason = await self._llm_review(
                    event, filename, file_size, preview
                )
                if not approved:
                    yield event.chain_result([
                        At(qq=user_id),
                        Plain(f"\n❌ 文件审核未通过：{filename}\n原因：{reason}"),
                    ])
                    return

            # 4. 上传到 OpenList
            final_name = filename
            if self._rename_enabled():
                template = self._rename_template()
                if template:
                    try:
                        final_name = template.format(
                            uploader=_sanitize_filename(uploader),
                            filename=filename,
                        )
                    except KeyError:
                        final_name = f"[{uploader}]{filename}"
            final_name = _sanitize_filename(final_name)
            full_path = self._join_path(target_path, final_name)

            async with self._upload_sem:
                try:
                    async with self._create_client() as client:
                        if not await client.ensure_dir(target_path):
                            yield event.plain_result("❌ 无法创建目标目录，请检查 OpenList 权限。")
                            return
                        if not await client.upload_file(
                            local_path, target_path, final_name
                        ):
                            yield event.chain_result([
                                At(qq=user_id),
                                Plain(f"\n❌ 上传失败：{filename}\n请稍后重试或联系管理员。"),
                            ])
                            return
                        share_link = await client.get_share_link(full_path)
                except OpenlistAuthError as e:
                    logger.error(f"OpenList 认证失败: {e}")
                    yield event.plain_result("❌ 上传失败：OpenList 连接配置异常，请联系管理员。")
                    return
                except Exception as e:
                    logger.error(f"上传异常: {e}")
                    yield event.plain_result("❌ 上传失败：发生未知错误，请稍后重试。")
                    return

            # 5. 记录上传者信息（本地持久化）
            self.records.add({
                "path": full_path,
                "name": final_name,
                "original_name": filename,
                "uploader_id": user_id,
                "uploader_name": uploader,
                "size": file_size,
                "time": int(time.time()),
                "group_id": self._get_group_id(event),
            })

            # 6. 群内 @ 上传者，告知成功
            link_text = f"🔗 分享链接：{share_link}" if share_link else ""
            yield event.chain_result([
                At(qq=user_id),
                Plain(
                    f"\n✅ 上传成功！\n📄 文件：{final_name}\n"
                    f"📂 目录：{full_path}\n{link_text}"
                ),
            ])
            logger.info(
                f"文件上传成功: user={uploader}({user_id}) path={full_path} "
                f"review={'通过' if self._review_enabled() else '关闭'}"
            )
        finally:
            try:
                if Path(local_path).exists():
                    Path(local_path).unlink()
            except OSError:
                pass

    # ================= 查询渲染 =================

    async def _render_items(
        self, items: List[Dict], base_path: str, title: str = None
    ) -> str:
        """把文件列表渲染成文本（含分享链接与上传者备注）。

        base_path 是计算文件完整路径用的真实路径；title 是展示标题（默认同 base_path）。
        """
        lines = [f"📁 {title or base_path}（共 {len(items)} 项）"]
        client = self._create_client()
        try:
            async with client:
                for i, item in enumerate(items[: self._max_list_items()], 1):
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    is_dir = bool(item.get("is_dir", False))
                    parent = str(item.get("parent") or base_path)
                    full = self._join_path(parent, name)
                    if is_dir:
                        lines.append(f"{i}. 📁 {name}/")
                        continue
                    size = item.get("size")
                    rec = self.records.find(full)
                    note = f" ｜上传者：{rec['uploader_name']}" if rec else ""
                    lines.append(f"{i}. 📄 {name}（{_fmt_size(size)}）{note}")
                    link = await client.get_share_link(full)
                    if link:
                        lines.append(f"   🔗 {link}")
        except Exception as e:
            logger.error(f"渲染文件列表失败: {e}")
            lines.append("（部分文件分享链接获取失败）")
        if len(items) > self._max_list_items():
            lines.append(f"（仅显示前 {self._max_list_items()} 项）")
        return "\n".join(lines)

    # ================= 命令组 =================

    @filter.command_group("ol", alias=["网盘", "openlist"])
    def ol_group(self):
        """OpenList 共享命令组。"""
        pass

    @ol_group.command("help", alias=["帮助"])
    async def help_cmd(self, event: AstrMessageEvent):
        ok, reason = self._check_permission(event)
        if not ok:
            yield event.plain_result(reason)
            return
        yield event.plain_result(
            "📦 OpenList 共享插件\n\n"
            "📤 上传：引用（回复）一条文件消息，再发送\n"
            "　/ol up [目标目录]\n"
            "　文件会先经 LLM 审核，通过后自动上传并在群里 @ 你告知结果。\n\n"
            "📂 查询：\n"
            "　/ol ls [路径]　　　列出文件与分享链接\n"
            "　/ol search 关键词　搜索文件\n"
            "　/ol info 路径　　　查看文件详情与分享链接\n\n"
            "🔗 查询结果直接返回分享链接，不下载文件。"
        )

    @ol_group.command("up", alias=["上传", "upload"])
    async def up_cmd(self, event: AstrMessageEvent, target: str = ""):
        """引用文件后上传（需 LLM 审核）。"""
        ok, reason = self._check_permission(event)
        if not ok:
            yield event.plain_result(reason)
            return
        if not self._openlist_url():
            yield event.plain_result("❌ OpenList 地址未配置，请联系管理员在插件设置中配置。")
            return

        target_path = self._norm_path(target) if (target or "").strip() else self._default_upload_path()
        components = self._extract_quoted_components(event)
        if not components:
            yield event.plain_result(
                "📤 请先引用（回复）一条包含文件的消息，再发送本指令。\n"
                "用法：引用文件 + /ol up [目标目录]"
            )
            return
        for comp in components:
            async for result in self._process_upload(event, comp, target_path):
                yield result

    @ol_group.command("ls", alias=["列表", "list"])
    async def ls_cmd(self, event: AstrMessageEvent, path: str = ""):
        """列出目录内容，文件附带分享链接。"""
        ok, reason = self._check_permission(event)
        if not ok:
            yield event.plain_result(reason)
            return
        if not self._openlist_url():
            yield event.plain_result("❌ OpenList 地址未配置，请联系管理员在插件设置中配置。")
            return
        path = self._norm_path(path)
        try:
            async with self._create_client() as client:
                data = await client.list_files(path, per_page=self._max_list_items())
                if data is None:
                    yield event.plain_result(f"❌ 无法访问目录：{path}")
                    return
                items = data.get("content") or []
        except Exception as e:
            logger.error(f"列目录失败: {e}")
            yield event.plain_result(f"❌ 列目录失败：{path}")
            return
        rendered = await self._render_items(items, path)
        yield event.plain_result(rendered)

    @ol_group.command("search", alias=["搜索"])
    async def search_cmd(self, event: AstrMessageEvent, keyword: str):
        """按关键词搜索文件。"""
        ok, reason = self._check_permission(event)
        if not ok:
            yield event.plain_result(reason)
            return
        if not self._openlist_url():
            yield event.plain_result("❌ OpenList 地址未配置，请联系管理员在插件设置中配置。")
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/ol search 关键词")
            return
        try:
            async with self._create_client() as client:
                items = await client.search_files(keyword, per_page=self._max_list_items())
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            yield event.plain_result("❌ 搜索失败，请稍后重试。")
            return
        if not items:
            yield event.plain_result(f"未找到与「{keyword}」相关的文件。")
            return
        # 搜索结果的真实路径根为 "/"，展示标题单独设置
        rendered = await self._render_items(
            items, "/", title=f"搜索结果：{keyword}"
        )
        yield event.plain_result(rendered)

    @ol_group.command("info", alias=["信息"])
    async def info_cmd(self, event: AstrMessageEvent, path: str):
        """查看文件详情与分享链接。"""
        ok, reason = self._check_permission(event)
        if not ok:
            yield event.plain_result(reason)
            return
        if not self._openlist_url():
            yield event.plain_result("❌ OpenList 地址未配置，请联系管理员在插件设置中配置。")
            return
        path = (path or "").strip()
        if not path:
            yield event.plain_result("用法：/ol info /目录/文件名")
            return
        path = self._norm_path(path)
        try:
            async with self._create_client() as client:
                info = await client.get_file_info(path)
                if not info:
                    yield event.plain_result(f"❌ 未找到：{path}")
                    return
                link = None if info.get("is_dir", False) else await client.get_share_link(path)
        except Exception as e:
            logger.error(f"获取文件信息失败: {e}")
            yield event.plain_result("❌ 获取信息失败，请稍后重试。")
            return
        rec = self.records.find(path)
        lines = [
            f"📄 {info.get('name', '')}",
            f"路径：{path}",
            f"类型：{'📁 目录' if info.get('is_dir', False) else '📄 文件'}",
            f"大小：{_fmt_size(info.get('size'))}",
            f"修改时间：{info.get('modified') or '未知'}",
        ]
        if rec:
            lines.append(f"上传者：{rec['uploader_name']}（{rec['uploader_id']}）")
            lines.append(f"上传时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['time']))}")
        if link:
            lines.append(f"🔗 分享链接：{link}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        """插件卸载清理。"""
        self.records = None
        logger.info("OpenList 共享插件已卸载")

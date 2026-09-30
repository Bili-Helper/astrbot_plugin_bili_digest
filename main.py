"""AstrBot entry point for the video digest assistant."""

import asyncio
import hashlib
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import regex as re
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .config import fingerprint, settings
from .extract import canonical_url, current_text, field, find_links, kind, mentioned, triggered
from .net import expand, public_url
from .pipeline import Pipeline
from .process import DigestError
from .store import Archive

HELP = """Bilibili 省流助手
• @机器人 来个省流 <视频链接>
• 回复视频分享卡片，再 @机器人 来个省流
• 省流记录 [关键词]：查看本会话最近记录
• 省流查看 <记录ID>：读取本会话的完整摘要
• 省流帮助：显示帮助
管理员可在插件配置中修改关键词、启用 BV 号/自动/正则触发和调整分析方案。
默认群聊需要 @机器人；私聊可直接使用。"""


class BiliDigest(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.c = settings(config)
        self.pattern = (
            re.compile(self.c["trigger_regex"]) if self.c["trigger_mode"] == "regex" else None
        )
        self.root = StarTools.get_data_dir("astrbot_plugin_bili_digest")
        self.archive = Archive(self.root)
        self.semaphore = asyncio.Semaphore(self.c["max_concurrent"])
        self.pending = 0
        self.locks = {}
        self.last_request = OrderedDict()
        self.tasks = set()

    async def send(self, event, text):
        await event.send(event.plain_result(text))

    async def execute(self, event, url, document):
        scope = event.unified_msg_origin
        async with self.semaphore:
            host = urlsplit(url).hostname
            if host in ("b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"):
                url = canonical_url(await expand(url, self.c["allowed_domains"]))
            else:
                await public_url(url, self.c["allowed_domains"])
            provider = self.c["provider_id"]
            if not provider:
                try:
                    provider = await self.context.get_current_chat_provider_id(umo=scope)
                except Exception:
                    provider = ""
            # Include the STT provider identity as well as the chat provider in cache isolation.
            stt_id = ""
            if self.c["asr_backend"] == "astrbot":
                try:
                    if hasattr(self.context, "get_using_stt_provider_async"):
                        stt = await self.context.get_using_stt_provider_async(umo=scope)
                    else:
                        stt = self.context.get_using_stt_provider(umo=scope)
                    stt_id = str(stt.meta().id) if stt else ""
                except Exception:
                    pass
            key = hashlib.sha256(
                (fingerprint(self.c, provider + "|" + stt_id, scope) + url).encode()
            ).hexdigest()
            document["canonical_url"] = url
            document["provider_id"] = provider
            with self.archive.connect() as db:
                db.execute("UPDATE runs SET cache_key=? WHERE id=?", (key, document["id"]))
            if key not in self.locks:
                self.locks[key] = [asyncio.Lock(), 0]
            lock_info = self.locks[key]
            lock_info[1] += 1
            try:
                async with lock_info[0]:
                    cached = self.archive.cached(key, self.c["cache_hours"])
                    if cached:
                        for name in (
                            "metadata",
                            "segments",
                            "summary",
                            "source",
                            "source_detail",
                            "warnings",
                        ):
                            document[name] = cached.get(name)
                        document["content_created_at"] = cached.get(
                            "content_created_at", cached["finished_at"]
                        )
                        document["cache_hit"] = True
                        document["cached_from"] = cached["id"]
                    else:
                        temporary = self.root / "tmp"
                        temporary.mkdir(exist_ok=True)
                        with tempfile.TemporaryDirectory(prefix="digest_", dir=temporary) as work:
                            pipeline = Pipeline(self.context, self.c, Path(work), scope, provider)
                            try:
                                document.update(await pipeline.run(url))
                            finally:
                                document["attempts"] = pipeline.attempts
                                if pipeline.info_data and "metadata" not in document:
                                    from .media import metadata

                                    document["metadata"] = metadata(pipeline.info_data, url)
                        document["content_created_at"] = time.time()
                        document["cache_hit"] = False
                    document["status"] = "success"
                    # Persist before releasing the URL lock so waiting requests can reuse it.
                    self.archive.finish(document)
            finally:
                lock_info[1] -= 1
                if lock_info[1] == 0:
                    del self.locks[key]

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        components = event.get_messages()
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        is_private = not bool(event.get_group_id())
        if self.c["require_mention"] and not mentioned(components, event.get_self_id()):
            if not (is_private and self.c["private_without_mention"]):
                return
        text = current_text(components).strip()
        scope = event.unified_msg_origin
        if text in ("省流帮助", "/省流帮助"):
            event.stop_event()
            await self.send(event, HELP)
            return
        command = text.lstrip("/").split(maxsplit=1)
        if command and command[0] in ("省流记录", "省流查看"):
            event.stop_event()
            argument = command[1].strip() if len(command) == 2 else ""
            if command[0] == "省流记录":
                docs = self.archive.recent(scope, argument)
                lines = [
                    f"{d['id'][:8]} | {d['status']} | {d.get('metadata', {}).get('title', d['url'])[:100]}"
                    for d in docs
                ]
                await self.send(
                    event, "本会话最近记录：\n" + ("\n".join(lines) or "暂无匹配记录。")
                )
            else:
                doc = self.archive.get(scope, argument)
                if not doc:
                    await self.send(event, "未找到本会话中的唯一记录，请提供至少 8 位记录 ID。")
                else:
                    content = doc.get("summary") or doc.get("error") or doc["status"]
                    for offset in range(0, len(content), self.c["reply_chars"]):
                        await self.send(event, content[offset : offset + self.c["reply_chars"]])
            return
        if not triggered(text, self.c, self.pattern):
            return
        # Prefer a URL in the current message over one in the quoted message.
        direct = [c for c in components if kind(c) != "reply"]
        replies = [c for c in components if kind(c) == "reply"]
        links = find_links(direct, self.c["allowed_domains"], self.c["bare_bv"])
        if not links:
            links = find_links(replies, self.c["allowed_domains"], self.c["bare_bv"])
        if not links and replies and event.get_platform_name() == "aiocqhttp":
            bot = getattr(event, "bot", None)
            if bot:
                for reply in replies[:1]:
                    try:
                        payload = await asyncio.wait_for(
                            bot.call_action("get_msg", message_id=int(field(reply, "id"))), 10
                        )
                        links = find_links(
                            payload.get("message", payload),
                            self.c["allowed_domains"],
                            self.c["bare_bv"],
                        )
                    except Exception:
                        logger.warning("BiliDigest: quoted message lookup failed")
        if not links:
            if self.c["trigger_mode"] != "auto":
                event.stop_event()
                await self.send(
                    event,
                    "没有找到支持的视频链接。请在本条消息附上链接，或回复包含视频链接的分享卡片。",
                )
            return
        event.stop_event()
        sender = str(event.get_sender_id())
        user_key = scope + ":" + sender
        now = time.monotonic()
        if now - self.last_request.get(user_key, -1e10) < self.c["user_cooldown_seconds"]:
            await self.send(event, "请求过于频繁，请稍后再试。")
            return
        if self.pending >= self.c["max_pending"]:
            await self.send(event, "省流任务已满，请稍后再试。")
            return
        self.last_request[user_key] = now
        self.last_request.move_to_end(user_key)
        while len(self.last_request) > 4096:
            self.last_request.popitem(last=False)
        self.pending += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        document = None
        try:
            document = self.archive.start(scope, "", links[0], sender)
            await self.send(
                event, "正在整理视频内容…" + (" 本次处理第一个链接。" if len(links) > 1 else "")
            )
            await asyncio.wait_for(
                self.execute(event, links[0], document), self.c["job_timeout_seconds"]
            )
        except asyncio.CancelledError:
            if document and document["status"] != "success":
                document.update(status="cancelled", error="任务被取消或插件停止。")
                self.archive.finish(document)
            raise
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, DigestError)
                else (
                    "任务超时，请调整时长限制或处理方案。"
                    if isinstance(error, asyncio.TimeoutError)
                    else "处理失败，请检查下载器、模型服务和网络配置。"
                )
            )
            logger.warning("BiliDigest: request failed (%s)", type(error).__name__)
            if document:
                document.update(status="failed", error=reason)
                self.archive.finish(document)
            await self.send(event, reason + (f"\n记录：{document['id'][:8]}" if document else ""))
            return
        finally:
            self.pending -= 1
            self.tasks.discard(task)
        title = document.get("metadata", {}).get("title", "视频省流")
        summary = document["summary"]
        if len(summary) > self.c["reply_chars"]:
            summary = (
                summary[: self.c["reply_chars"]]
                + "\n…完整内容可用「省流查看 "
                + document["id"][:8]
                + "」读取。"
            )
        warnings = "\n".join(document.get("warnings") or [])
        answer = (
            f"{title}\n\n{summary}\n\n来源：{document['source']}"
            f"{'（缓存）' if document.get('cache_hit') else ''}\n{warnings}"
            f"\n{document.get('canonical_url', document['url'])}\n记录：{document['id'][:8]}"
        )
        try:
            await self.send(event, answer)
        except Exception:
            # Preserve a successful archive even when the messaging platform rejects delivery.
            document["delivery_error"] = "消息发送失败，可通过省流记录查询。"
            self.archive.finish(document)
            logger.warning("BiliDigest: summary delivery failed")

    async def terminate(self):
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

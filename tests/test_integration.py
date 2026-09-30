"""Contract tests with AstrBot-shaped events/providers; no running bot required."""

import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_bili_digest.process import DigestError

URL = "https://www.bilibili.com/video/BV1xx411c7us"


class FakeStar:
    def __init__(self, context):
        self.context = context


def load_entry():
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event_api = types.ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object
    event_api.filter = SimpleNamespace(
        EventMessageType=SimpleNamespace(ALL=1), event_message_type=lambda *a, **kw: lambda f: f
    )
    star_api = types.ModuleType("astrbot.api.star")
    star_api.Context = object
    star_api.Star = FakeStar
    star_api.StarTools = SimpleNamespace(get_data_dir=lambda name: None)
    with patch.dict(
        sys.modules,
        {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api,
            "astrbot.api.event": event_api,
            "astrbot.api.star": star_api,
        },
    ):
        return importlib.import_module("astrbot_plugin_bili_digest.main")


entry = load_entry()


class Event:
    def __init__(self, components, scope="group:a", group="a", sender="u"):
        self.components = components
        self.unified_msg_origin = scope
        self.group = group
        self.sender = sender
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self.components

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "42"

    def get_group_id(self):
        return self.group

    def get_platform_name(self):
        return "aiocqhttp"

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)


def message(text="来个省流 " + URL, at=True):
    return ([{"type": "at", "data": {"qq": "42"}}] if at else []) + [
        {"type": "text", "data": {"text": text}}
    ]


class EntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_patch = patch.object(
            entry.StarTools, "get_data_dir", return_value=Path(self.tmp.name)
        )
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.net_patch = patch.object(entry, "public_url", AsyncMock())
        self.net_patch.start()
        self.addCleanup(self.net_patch.stop)
        self.ctx = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="model"),
            get_using_stt_provider_async=AsyncMock(return_value=None),
        )
        self.plugin = entry.BiliDigest(self.ctx, {"user_cooldown_seconds": 0})
        self.runner = SimpleNamespace(
            run=AsyncMock(
                return_value={
                    "summary": "测试摘要",
                    "metadata": {"title": "视频标题"},
                    "segments": [],
                    "source": "subtitle",
                    "source_detail": {},
                    "warnings": [],
                }
            ),
            attempts=[],
            info_data=None,
        )
        self.pipe_patch = patch.object(entry, "Pipeline", return_value=self.runner)
        self.pipe_patch.start()
        self.addCleanup(self.pipe_patch.stop)

    async def test_direct_link_and_archive(self):
        event = Event(message())
        await self.plugin.on_message(event)
        self.assertTrue(event.stopped)
        self.assertIn("测试摘要", event.sent[-1])
        docs = self.plugin.archive.recent("group:a")
        self.assertEqual(docs[0]["status"], "success")
        self.assertEqual(self.plugin.pending, 0)

    async def test_group_requires_mention(self):
        event = Event(message(at=False))
        await self.plugin.on_message(event)
        self.assertEqual(event.sent, [])
        self.runner.run.assert_not_called()

    async def test_private_without_mention(self):
        event = Event(message(at=False), scope="private:u", group="")
        await self.plugin.on_message(event)
        self.runner.run.assert_awaited_once()

    async def test_reply_json_card(self):
        event = Event(
            message("来个省流")
            + [
                {
                    "type": "reply",
                    "chain": [
                        {
                            "type": "json",
                            "data": {"data": json.dumps({"meta": {"detail_1": {"qqdocurl": URL}}})},
                        }
                    ],
                }
            ]
        )
        await self.plugin.on_message(event)
        self.runner.run.assert_awaited_once_with(URL)

    async def test_onebot_reply_lookup(self):
        event = Event(message("来个省流") + [{"type": "reply", "data": {"id": "123"}}])
        event.bot = SimpleNamespace(
            call_action=AsyncMock(
                return_value={"message": [{"type": "text", "data": {"text": URL}}]}
            )
        )
        await self.plugin.on_message(event)
        event.bot.call_action.assert_awaited_once_with("get_msg", message_id=123)
        self.runner.run.assert_awaited_once_with(URL)

    async def test_cache_still_records_every_request(self):
        await self.plugin.on_message(Event(message()))
        await self.plugin.on_message(Event(message()))
        self.runner.run.assert_awaited_once()
        docs = self.plugin.archive.recent("group:a")
        self.assertEqual(len(docs), 2)
        self.assertTrue(docs[0]["cache_hit"])

    async def test_concurrent_duplicate_single_flight(self):
        async def delayed(url):
            await asyncio.sleep(0.05)
            return {
                "summary": "one",
                "metadata": {},
                "segments": [],
                "source": "subtitle",
                "warnings": [],
            }

        self.runner.run.side_effect = delayed
        await asyncio.gather(
            self.plugin.on_message(Event(message(), sender="a")),
            self.plugin.on_message(Event(message(), sender="b")),
        )
        self.runner.run.assert_awaited_once()
        self.assertEqual(self.plugin.locks, {})

    async def test_failure_is_saved(self):
        self.runner.run.side_effect = DigestError("no subtitle or STT")
        event = Event(message())
        await self.plugin.on_message(event)
        self.assertEqual(self.plugin.archive.recent("group:a")[0]["status"], "failed")
        self.assertIn("no subtitle", event.sent[-1])

    async def test_history_cannot_read_other_session(self):
        await self.plugin.on_message(Event(message()))
        doc = self.plugin.archive.recent("group:a")[0]
        event = Event(message("省流查看 " + doc["id"]), scope="group:b", group="b")
        await self.plugin.on_message(event)
        self.assertNotIn("测试摘要", event.sent[-1])

    async def test_queue_limit(self):
        self.plugin.c["max_pending"] = 1
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(url):
            started.set()
            await release.wait()
            return {
                "summary": "ok",
                "metadata": {},
                "segments": [],
                "source": "subtitle",
                "warnings": [],
            }

        self.runner.run.side_effect = blocked
        first = asyncio.create_task(self.plugin.on_message(Event(message())))
        await started.wait()
        second = Event(message(), sender="b")
        await self.plugin.on_message(second)
        self.assertIn("已满", second.sent[-1])
        release.set()
        await first

    async def test_terminate_cancels_and_records(self):
        started = asyncio.Event()

        async def blocked(url):
            started.set()
            await asyncio.Event().wait()

        self.runner.run.side_effect = blocked
        task = asyncio.create_task(self.plugin.on_message(Event(message())))
        await started.wait()
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.plugin.archive.recent("group:a")[0]["status"], "cancelled")
        self.assertEqual(self.plugin.pending, 0)

    async def test_auto_mode_ignores_unrelated_chat(self):
        self.plugin.c["trigger_mode"] = "auto"
        self.plugin.c["require_mention"] = False
        event = Event(message("今天吃什么", at=False))
        await self.plugin.on_message(event)
        self.assertFalse(event.stopped)
        self.assertEqual(event.sent, [])

    async def test_short_link_keeps_part(self):
        event = Event(message("来个省流 https://b23.tv/example"))
        with patch.object(entry, "expand", AsyncMock(return_value=URL + "?p=2&tracking=x")):
            await self.plugin.on_message(event)
        self.runner.run.assert_awaited_once_with(URL + "?p=2")

    async def test_delivery_failure_keeps_successful_archive(self):
        event = Event(message())
        event.send = AsyncMock(side_effect=[None, RuntimeError("delivery failed")])
        await self.plugin.on_message(event)
        doc = self.plugin.archive.recent("group:a")[0]
        self.assertEqual(doc["status"], "success")
        self.assertIn("delivery_error", doc)

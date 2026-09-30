import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_bili_digest.config import DEFAULTS, fingerprint, settings
from astrbot_plugin_bili_digest.extract import (
    allowed_url,
    canonical_url,
    current_text,
    find_links,
    mentioned,
    triggered,
)
from astrbot_plugin_bili_digest.media import Media, parse_subtitles
from astrbot_plugin_bili_digest.pipeline import Pipeline
from astrbot_plugin_bili_digest.process import DigestError, LimitError, run_process
from astrbot_plugin_bili_digest.store import Archive

BV = "BV1xx411c7us"
URL = "https://www.bilibili.com/video/" + BV
SRT = "1\n00:00:01,200 --> 00:00:03,400\n测试内容\n\n2\n00:00:04,000 --> 00:00:06,000\n第二段\n"


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.c = settings({})

    def test_defaults_match_schema(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(DEFAULTS, {k: v["default"] for k, v in schema.items()})

    def test_reply_json_card(self):
        card = {"meta": {"detail_1": {"qqdocurl": URL + "?p=2&share_source=qq"}}}
        value = SimpleNamespace(
            type="Reply", chain=[SimpleNamespace(type="Json", data=json.dumps(card))]
        )
        self.assertEqual(find_links(value, self.c["allowed_domains"]), [URL + "?p=2"])

    def test_escaped_cq_card(self):
        message = (
            "[CQ:json,data="
            + json.dumps({"url": URL}).replace("/", r"\/").replace('"', "&quot;")
            + "]"
        )
        self.assertEqual(find_links(message, self.c["allowed_domains"]), [URL])

    def test_xml_card(self):
        self.assertEqual(
            find_links('<msg url="' + URL + '?p=3&amp;foo=x"/>', self.c["allowed_domains"]),
            [URL + "?p=3"],
        )

    def test_punctuation_and_bare_domain(self):
        self.assertEqual(
            find_links("看（bilibili.com/video/" + BV + "）。", self.c["allowed_domains"]), [URL]
        )

    def test_dedup_does_not_drop_page(self):
        self.assertEqual(
            find_links(URL + "?p=2 " + URL + "?p=2", self.c["allowed_domains"], True),
            [URL + "?p=2"],
        )

    def test_bv_opt_in(self):
        self.assertEqual(find_links(BV, self.c["allowed_domains"]), [])
        self.assertEqual(find_links(BV, self.c["allowed_domains"], True), [URL])

    def test_invalid_placeholder_bv(self):
        self.assertEqual(find_links("BV12345", self.c["allowed_domains"], True), [])

    def test_domain_boundaries(self):
        for url in (
            "https://bilibili.com.evil.example/a",
            "http://localhost/a",
            "file:///etc/passwd",
            "https://evil.example/" + BV,
            "https://bilibili.com@evil.example/a",
            "https://bilibili.com:8888/a",
        ):
            with self.subTest(url=url):
                self.assertFalse(allowed_url(url, self.c["allowed_domains"]))
                self.assertEqual(find_links(url, self.c["allowed_domains"], True), [])

    def test_percent_encoded_card(self):
        self.assertEqual(
            find_links(
                {"jumpUrl": URL.replace(":", "%3A").replace("/", "%2F")}, self.c["allowed_domains"]
            ),
            [URL],
        )

    def test_mentions_only_current_chain(self):
        self.assertFalse(
            mentioned([{"type": "reply", "chain": [{"type": "at", "data": {"qq": 99}}]}], "99")
        )
        self.assertTrue(mentioned([{"type": "at", "data": {"qq": 99}}], "99"))
        self.assertFalse(mentioned([{"type": "at", "data": {"qq": "all"}}], "99"))

    def test_keywords_only_current_text(self):
        components = [
            SimpleNamespace(type="Reply", chain=[SimpleNamespace(type="Plain", text="来个省流")]),
            SimpleNamespace(type="Plain", text="普通聊天"),
        ]
        self.assertFalse(triggered(current_text(components), self.c))

    def test_trigger_modes(self):
        import regex as re

        self.assertTrue(triggered("来个省流 " + URL, self.c))
        self.assertFalse(triggered(BV, self.c))
        self.assertTrue(triggered(BV, settings({"bare_bv": True})))
        self.assertTrue(triggered(URL, settings({"trigger_mode": "auto"})))
        self.assertTrue(
            triggered(
                "总结一下",
                settings({"trigger_mode": "regex", "trigger_regex": "总结"}),
                re.compile("总结"),
            )
        )

    def test_config_validation(self):
        for bad in (
            {"strategies": ["invented"]},
            {"max_concurrent": 0},
            {"trigger_mode": "regex"},
            {"keywords": "abc"},
        ):
            with self.assertRaises(ValueError):
                settings(bad)

    def test_cache_configuration_and_scope(self):
        self.assertNotEqual(fingerprint(self.c, "p", "a"), fingerprint(self.c, "p", "b"))
        self.assertNotEqual(fingerprint(self.c, "p", "a"), fingerprint(self.c, "p2", "a"))

    def test_youtube_normalization(self):
        self.assertEqual(
            canonical_url("https://youtu.be/abc?t=5"), "https://www.youtube.com/watch?v=abc"
        )


class SubtitleTests(unittest.TestCase):
    def test_srt_timestamps(self):
        segments = parse_subtitles(SRT, "srt")
        self.assertEqual(segments[0], {"start": 1.2, "end": 3.4, "text": "测试内容"})

    def test_vtt_markup(self):
        self.assertEqual(
            parse_subtitles(
                "WEBVTT\n\n00:01.000 --> 00:02.000\n<v A>hello &amp; world</v>\n", "vtt"
            )[0]["text"],
            "hello & world",
        )

    def test_bcc(self):
        self.assertEqual(
            parse_subtitles('{"body":[{"from":1,"to":2,"content":"hi"}]}', "json")[0]["text"], "hi"
        )

    def test_json3(self):
        data = '{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"hello"}]}]}'
        self.assertEqual(parse_subtitles(data, "json3")[0]["end"], 3)

    def test_danmaku_is_not_transcript(self):
        self.assertEqual(parse_subtitles('<i><d p="1">comment</d></i>', "xml"), [])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a = Archive(self.temp.name)

    def test_roundtrip_and_export(self):
        d = self.a.start("group-a", "key", URL, "u")
        d.update(status="success", summary="摘要", metadata={"title": "测试"})
        self.a.finish(d)
        self.assertEqual(self.a.cached("key", 1)["summary"], "摘要")
        self.assertEqual(self.a.get("group-a", d["id"][:8])["id"], d["id"])
        self.assertEqual(
            json.loads((self.a.records / (d["id"] + ".json")).read_text(encoding="utf-8"))[
                "summary"
            ],
            "摘要",
        )

    def test_session_isolation(self):
        d = self.a.start("group-a", "key", URL, "u")
        self.assertIsNone(self.a.get("group-b", d["id"]))
        self.assertEqual(self.a.recent("group-b"), [])

    def test_failed_never_cached(self):
        d = self.a.start("a", "key", URL, "u")
        d.update(status="failed", error="no subtitles")
        self.a.finish(d)
        self.assertIsNone(self.a.cached("key", 1))

    def test_cache_hit_does_not_extend_ttl(self):
        d = self.a.start("a", "key", URL, "u")
        d.update(status="success", content_created_at=time.time() - 7200, cache_hit=True)
        self.a.finish(d)
        self.assertIsNone(self.a.cached("key", 1))

    def test_crash_recovery(self):
        d = self.a.start("a", "key", URL, "u")
        recovered = Archive(self.temp.name)
        self.assertEqual(recovered.get("a", d["id"])["status"], "interrupted")
        self.assertTrue((recovered.records / (d["id"] + ".json")).exists())

    def test_search(self):
        d = self.a.start("a", "key", URL, "u")
        d.update(status="success", summary="关于 Python 的教程")
        self.a.finish(d)
        self.assertEqual(len(self.a.recent("a", "python")), 1)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = SimpleNamespace(
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="一句话结论"))
        )
        self.p = Pipeline(self.ctx, settings({}), Path(self.temp.name), "s", "llm")
        self.p.media.info = AsyncMock(return_value={"title": "title", "duration": 10})
        self.p.media.subtitles = AsyncMock(
            return_value=([{"start": 1, "end": 2, "text": "事实"}], {"language": "zh"})
        )

    async def test_subtitle_first_no_download(self):
        self.p.media.download = AsyncMock()
        d = await self.p.run(URL)
        self.assertEqual(d["source"], "subtitle")
        self.p.media.download.assert_not_called()
        self.assertEqual(d["segments"][0]["text"], "事实")

    async def test_fallback_to_asr(self):
        self.p.media.subtitles.side_effect = DigestError("missing")
        self.p.transcribe = AsyncMock(return_value=[{"start": 0, "end": 2, "text": "音频事实"}])
        d = await self.p.run(URL)
        self.assertEqual(d["source"], "asr")
        self.assertEqual(d["attempts"][0]["status"], "failed")

    async def test_all_failed(self):
        self.p.media.subtitles.side_effect = DigestError("missing")
        self.p.transcribe = AsyncMock(side_effect=DigestError("no stt"))
        with self.assertRaises(DigestError):
            await self.p.run(URL)
        self.assertEqual(len(self.p.attempts), 2)

    async def test_empty_model_rejected(self):
        self.ctx.llm_generate.return_value.completion_text = ""
        with self.assertRaises(DigestError):
            await self.p.ask("input")

    async def test_map_reduce_covers_all_text(self):
        self.p.c["chunk_chars"] = 2000
        await self.p.summarize("A" * 2000 + "B" * 2000 + "TAIL", "title")
        calls = self.ctx.llm_generate.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertIn("TAIL", calls[2].kwargs["prompt"])

    async def test_no_silent_truncation(self):
        self.p.c["max_source_chars"] = 2000
        with self.assertRaises(DigestError):
            await self.p.summarize("X" * 2001, "title")

    async def test_duration_limit_does_not_fallback(self):
        self.p.media.info.side_effect = LimitError("too long")
        self.p.transcribe = AsyncMock()
        with self.assertRaises(LimitError):
            await self.p.run(URL)
        self.p.transcribe.assert_not_called()

    async def test_cancel_propagates(self):
        self.p.media.info.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.p.run(URL)

    async def test_bibigpt_independent_of_local_tools(self):
        self.p.c["strategies"] = ["bibigpt"]
        with patch(
            "astrbot_plugin_bili_digest.pipeline.bibigpt",
            AsyncMock(return_value={"summary": "external"}),
        ):
            result = await self.p.run(URL)
        self.assertEqual(result["summary"], "external")
        self.p.media.info.assert_not_called()

    async def test_danmaku_skipped_and_language_priority(self):
        media = Media(settings({}), Path(self.temp.name))
        segments, detail = await media.subtitles(
            {
                "subtitles": {
                    "danmaku": [{"ext": "xml", "data": "<i/>"}],
                    "en": [{"ext": "srt", "data": SRT.replace("测试内容", "English")}],
                    "zh-CN": [{"ext": "srt", "data": SRT}],
                }
            }
        )
        self.assertEqual(detail["language"], "zh-CN")
        self.assertEqual(segments[0]["text"], "测试内容")

    async def test_segment_offsets_for_astrbot_stt(self):
        self.p.media.download = AsyncMock(return_value=Path("audio"))
        self.p.media.audio_chunks = AsyncMock(return_value=[Path("0.wav"), Path("1.wav")])
        self.p.info_data = {"duration": 450}
        self.ctx.get_using_stt_provider_async = AsyncMock(
            return_value=SimpleNamespace(get_text=AsyncMock(return_value="speech"))
        )
        result = await self.p.transcribe(URL)
        self.assertEqual(result[1]["start"], 300)
        self.assertEqual(result[1]["end"], 450)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_subprocess_success(self):
        self.assertEqual(
            (await run_process([sys.executable, "-c", "print('ok')"], 5)).strip(), "ok"
        )

    async def test_failure_hides_stderr_secrets(self):
        with self.assertRaises(DigestError) as ctx:
            await run_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('SECRET_COOKIE',file=sys.stderr); sys.exit(2)",
                ],
                5,
            )
        self.assertNotIn("SECRET", str(ctx.exception))

    async def test_timeout_kills_process(self):
        start = time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):
            await run_process([sys.executable, "-c", "import time; time.sleep(60)"], 0.15)
        self.assertLess(time.monotonic() - start, 10)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg unavailable")
    async def test_real_ffmpeg_audio_and_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            path = work / "fixture.mp4"
            await run_process(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=320x240:r=10:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=2",
                    "-shortest",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "aac",
                    path,
                ],
                30,
            )
            media = Media(settings({"frame_count": 2}), work)
            self.assertGreater(await media.duration(path), 1.9)
            self.assertEqual(len(await media.audio_chunks(path)), 1)
            self.assertEqual(len(await media.frames(path)), 2)


if __name__ == "__main__":
    unittest.main()

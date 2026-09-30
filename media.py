"""Download bounded media and normalize timestamped subtitles."""

import html
import json
import re
import sys
from pathlib import Path

from .net import fetch
from .process import DigestError, LimitError, run_process


def parse_subtitles(text, ext):
    result = []
    if ext in ("json", "json3"):
        data = json.loads(text)
        if "body" in data:
            result = [
                {"start": float(x["from"]), "end": float(x["to"]), "text": x["content"]}
                for x in data["body"]
            ]
        else:
            for event in data.get("events", []):
                value = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
                if value:
                    start = event.get("tStartMs", 0) / 1000
                    result.append(
                        {
                            "start": start,
                            "end": start + event.get("dDurationMs", 0) / 1000,
                            "text": value,
                        }
                    )
    else:
        pattern = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})")
        lines = text.replace("\r", "").splitlines()
        for i, line in enumerate(lines):
            if "-->" not in line:
                continue
            stamps = pattern.findall(line)
            if len(stamps) != 2:
                continue
            times = [
                int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in stamps
            ]
            payload = []
            for following in lines[i + 1 :]:
                if not following.strip():
                    break
                if "-->" in following:
                    break
                payload.append(following)
            value = html.unescape(re.sub(r"<[^>]+>", "", " ".join(payload))).strip()
            if value:
                result.append({"start": times[0], "end": times[1], "text": value})
    cleaned = []
    for segment in result:
        segment["text"] = str(segment["text"]).strip()
        if segment["text"] and (not cleaned or cleaned[-1]["text"] != segment["text"]):
            cleaned.append(segment)
    return cleaned


class Media:
    def __init__(self, config, work):
        self.c = config
        self.work = Path(work)
        self.budget = config["max_download_mb"] * 1024 * 1024

    def ytdlp(self):
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-warnings",
            "--no-progress",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
            "--extractor-retries",
            "2",
            "--use-extractors",
            "default,-generic",
            "--no-cache-dir",
        ]
        if self.c["cookies_file"]:
            # yt-dlp can update its cookie jar; use an isolated copy for concurrent jobs.
            import shutil

            cookie_copy = self.work / "cookies.txt"
            if not cookie_copy.exists():
                shutil.copyfile(self.c["cookies_file"], cookie_copy)
            args += ["--cookies", str(cookie_copy)]
        return args

    async def info(self, url):
        output = await run_process(
            self.ytdlp()
            + [
                "--simulate",
                "--dump-single-json",
                "--write-subs",
                "--sub-langs",
                "all,-danmaku",
                "--",
                url,
            ],
            self.c["step_timeout_seconds"],
            cwd=self.work,
        )
        info = json.loads(output)
        if info.get("_type") in ("playlist", "multi_video"):
            entries = info.get("entries") or []
            if len(entries) != 1:
                raise DigestError("请发送单个视频或明确的分 P 链接。")
            info = entries[0]
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise DigestError("暂不支持直播或尚未开始的视频。")
        duration = float(info.get("duration") or 0)
        if duration > self.c["max_duration_seconds"]:
            raise LimitError("视频时长超过设置的上限。")
        return info

    async def subtitles(self, info):
        tracks = []
        languages = self.c["subtitle_languages"]
        for automatic, group in (
            (False, info.get("subtitles", {})),
            (True, info.get("automatic_captions", {})),
        ):
            for lang, formats in group.items():
                if lang == "danmaku":
                    continue
                rank = next(
                    (
                        i
                        for i, p in enumerate(languages)
                        if lang.lower() == p.lower() or lang.lower().startswith(p.lower() + "-")
                    ),
                    len(languages),
                )
                for track in formats:
                    if track.get("ext") in ("srt", "vtt", "json", "json3"):
                        tracks.append((rank, automatic or lang.startswith("ai-"), lang, track))
        tracks.sort(key=lambda x: (x[0], x[1]))
        for _, automatic, language, track in tracks:
            try:
                content = track.get("data")
                if content is None:
                    raw, _ = await fetch(track["url"])
                    content = raw.decode("utf-8-sig")
                segments = parse_subtitles(content, track["ext"])
                if segments:
                    return segments, {"language": language, "automatic": automatic}
            except Exception:
                continue
        raise DigestError("未取得可用字幕；部分 B 站字幕需要有效登录 Cookie。")

    async def download(self, url, video=False):
        directory = self.work / ("video" if video else "audio")
        directory.mkdir(exist_ok=True)
        existing = [
            p
            for p in directory.glob("media.*")
            if p.suffix in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".ogg", ".flv")
        ]
        if existing:
            return existing[0]
        if self.c["downloader"] == "lux":
            args = [self.c["lux_path"], "-o", str(directory), "-O", "media"]
            # Lux accepts a cookie string/file, while yt-dlp uses Netscape cookies.
            if self.c["cookies_file"]:
                args += ["-c", self.c["cookies_file"]]
            args += [url]
        else:
            format_spec = "bv*[height<=480]+ba/b[height<=480]/b" if video else "ba/b"
            args = self.ytdlp() + [
                "--max-filesize",
                str(self.budget),
                "-f",
                format_spec,
                "--merge-output-format",
                "mp4",
                "-o",
                str(directory / "media.%(ext)s"),
                "--",
                url,
            ]
        if self.c["downloader"] == "yt-dlp" and self.c["ffmpeg_path"] != "ffmpeg":
            args[3:3] = ["--ffmpeg-location", self.c["ffmpeg_path"]]
        await run_process(
            args,
            self.c["step_timeout_seconds"],
            cwd=self.work,
            monitor_dir=self.work,
            max_bytes=self.budget,
        )
        files = [
            p
            for p in directory.glob("media.*")
            if p.suffix in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus", ".ogg", ".flv")
        ]
        if len(files) != 1:
            raise DigestError("下载未产生单个完整媒体文件，或文件超过限制。")
        if files[0].stat().st_size > self.budget:
            raise DigestError("下载文件超过大小限制。")
        return files[0]

    async def duration(self, path):
        output = await run_process(
            [
                self.c["ffprobe_path"],
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            30,
        )
        duration = float(json.loads(output)["format"]["duration"])
        if duration <= 0 or duration > self.c["max_duration_seconds"]:
            raise LimitError("实际媒体时长无效或超过上限。")
        return duration

    async def audio_chunks(self, path):
        await self.duration(path)
        directory = self.work / "chunks"
        directory.mkdir(exist_ok=True)
        seconds = self.c["audio_segment_seconds"]
        await run_process(
            [
                self.c["ffmpeg_path"],
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "segment",
                "-segment_time",
                str(seconds),
                "-reset_timestamps",
                "1",
                directory / "%05d.wav",
            ],
            self.c["step_timeout_seconds"],
            monitor_dir=self.work,
            max_bytes=self.budget,
        )
        files = sorted(directory.glob("*.wav"))
        if not files:
            raise DigestError("视频没有可转录的音轨。")
        return files

    async def frames(self, path):
        duration = await self.duration(path)
        result = []
        for i in range(self.c["frame_count"]):
            seconds = duration * (i + 0.5) / self.c["frame_count"]
            target = self.work / f"frame_{i:03d}.jpg"
            await run_process(
                [
                    self.c["ffmpeg_path"],
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(seconds),
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=768:-2",
                    "-q:v",
                    "4",
                    target,
                ],
                30,
                monitor_dir=self.work,
                max_bytes=self.budget,
            )
            if not target.exists():
                raise DigestError("无法提取视频画面。")
            result.append((seconds, target))
        return result


def metadata(info, url):
    keys = (
        "id",
        "title",
        "uploader",
        "uploader_id",
        "channel",
        "duration",
        "timestamp",
        "upload_date",
        "description",
        "thumbnail",
        "extractor_key",
        "view_count",
        "like_count",
        "chapters",
    )
    result = {key: info.get(key) for key in keys if info.get(key) is not None}
    result["webpage_url"] = url
    return result

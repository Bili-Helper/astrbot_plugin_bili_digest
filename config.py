"""Configuration shared by the plugin and command-line workers."""

import hashlib
import json

DEFAULTS = {
    "keywords": ["来个省流"],
    "trigger_mode": "keyword",
    "require_mention": True,
    "private_without_mention": True,
    "bare_bv": False,
    "trigger_regex": "",
    "allowed_domains": [
        "bilibili.com",
        "b23.tv",
        "bili2233.cn",
        "youtube.com",
        "youtu.be",
        "douyin.com",
        "ixigua.com",
        "acfun.cn",
    ],
    "strategies": ["subtitle", "asr"],
    "provider_id": "",
    "vision_provider_id": "",
    "asr_backend": "astrbot",
    "downloader": "yt-dlp",
    "lux_path": "lux",
    "cookies_file": "",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "subtitle_languages": ["zh-CN", "zh-Hans", "zh", "ai-zh", "en"],
    "whisper_model": "small",
    "whisper_device": "cpu",
    "whisper_compute_type": "int8",
    "asr_language": "",
    "audio_segment_seconds": 300,
    "gemini_api_key": "",
    "gemini_model": "",
    "bibigpt_api_key": "",
    "max_duration_seconds": 3600,
    "max_download_mb": 300,
    "max_concurrent": 2,
    "max_pending": 8,
    "user_cooldown_seconds": 15,
    "job_timeout_seconds": 1200,
    "step_timeout_seconds": 300,
    "chunk_chars": 12000,
    "max_source_chars": 300000,
    "frame_count": 8,
    "cache_hours": 168,
    "reply_chars": 1800,
    "summary_prompt": "用中文写省流：先给一句话结论，再列3至8条关键内容；有依据时附时间点，最后说明适合谁看。避免空泛套话。",
}


def settings(config):
    result = {**DEFAULTS, **dict(config)}
    for key in ("keywords", "allowed_domains", "strategies", "subtitle_languages"):
        if not isinstance(result[key], list) or not all(isinstance(x, str) for x in result[key]):
            raise ValueError(f"{key} 必须是字符串列表")
    ranges = {
        "audio_segment_seconds": (30, 600),
        "max_duration_seconds": (1, 21600),
        "max_download_mb": (10, 4096),
        "max_concurrent": (1, 8),
        "max_pending": (1, 100),
        "job_timeout_seconds": (30, 7200),
        "step_timeout_seconds": (5, 1800),
        "chunk_chars": (2000, 40000),
        "max_source_chars": (2000, 2000000),
        "frame_count": (1, 32),
        "reply_chars": (200, 4000),
        "cache_hours": (0, 8760),
        "user_cooldown_seconds": (0, 3600),
    }
    for key, (low, high) in ranges.items():
        result[key] = int(result[key])
        if not low <= result[key] <= high:
            raise ValueError(f"{key} 必须在 {low} 和 {high} 之间")
    choices = {
        "trigger_mode": {"keyword", "regex", "auto"},
        "asr_backend": {"astrbot", "faster-whisper"},
        "downloader": {"yt-dlp", "lux"},
    }
    for key, values in choices.items():
        if result[key] not in values:
            raise ValueError(f"不支持的 {key}")
    if not result["strategies"] or set(result["strategies"]) - {
        "subtitle",
        "asr",
        "frames",
        "gemini",
        "bibigpt",
    }:
        raise ValueError("strategies 只能包含 subtitle/asr/frames/gemini/bibigpt")
    if result["trigger_mode"] == "regex" and not result["trigger_regex"]:
        raise ValueError("正则触发模式需要 trigger_regex")
    return result


def fingerprint(config, provider_id, scope):
    # Hash secrets as part of the cache identity; never persist their values.
    payload = {"version": 1, "config": config, "provider": provider_id, "scope": scope}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

#!/usr/bin/env python3
"""Run the upstream Douyin preprocessor, then fall back to Chrome DOM scraping.

The fallback is intentionally narrow:
- use headless Chrome to render the public Douyin page
- extract page title, author, chapter points, and the playable aweme URL
- extract audio with ffmpeg
- transcribe with whisper or a configured ASR command
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_DESC_RE = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]+)"',
    re.IGNORECASE | re.DOTALL,
)
SOURCE_RE = re.compile(r'<source[^>]+src="([^"]+)"', re.IGNORECASE)
CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.IGNORECASE)
OG_URL_RE = re.compile(r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"', re.IGNORECASE)
PUBLISH_TIME_RE = re.compile(r"发布时间：([^<]+)")
CHAPTER_RE = re.compile(
    r'<div class="McB9zXIU">([^<]+)</div><div class="fQx33EPI">([^<]+)</div>',
    re.IGNORECASE,
)
AUTHOR_FROM_DESC_RE = re.compile(r"-\s*([^-\s]+)\s*于\d{8}", re.IGNORECASE)
DOUYIN_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Douyin preprocessor with browser fallback.")
    parser.add_argument("url")
    parser.add_argument("--cookies")
    parser.add_argument("--yt-dlp-bin", default="yt-dlp")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument("--asr-command")
    parser.add_argument("--ocr-command")
    parser.add_argument("--vision-command")
    parser.add_argument("--ocr-lang", default="chi_sim+eng")
    parser.add_argument("--frame-limit", default="8")
    parser.add_argument("--scene-threshold", default="0.35")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    upstream = run_upstream(args)
    if upstream and is_upstream_successful(upstream):
        print(json.dumps(upstream, ensure_ascii=False, indent=2))
        return 0

    fallback_payload, exit_code = run_browser_fallback(args, upstream)
    print(json.dumps(fallback_payload, ensure_ascii=False, indent=2))
    return exit_code


def run_upstream(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    upstream_script = resolve_upstream_script()
    if upstream_script is None:
        return None

    command = [sys.executable or "python3", str(upstream_script), args.url]
    passthrough = [
        ("--cookies", args.cookies),
        ("--yt-dlp-bin", args.yt_dlp_bin),
        ("--ffmpeg-bin", args.ffmpeg_bin),
        ("--ffprobe-bin", args.ffprobe_bin),
        ("--asr-command", args.asr_command),
        ("--ocr-command", args.ocr_command),
        ("--vision-command", args.vision_command),
        ("--ocr-lang", args.ocr_lang),
        ("--frame-limit", str(args.frame_limit)),
        ("--scene-threshold", str(args.scene_threshold)),
    ]
    for flag, value in passthrough:
        if value:
            command.extend([flag, value])

    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout = (completed.stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        warnings = payload.get("warnings")
        if not isinstance(warnings, list):
            payload["warnings"] = []
        errors = payload.get("errors")
        if not isinstance(errors, list):
            payload["errors"] = []
        payload["_upstream_exit_code"] = completed.returncode
        stderr_text = (completed.stderr or "").strip()
        if stderr_text:
            payload["_upstream_stderr"] = stderr_text[-1200:]
        return payload
    return None


def resolve_upstream_script() -> Optional[Path]:
    configured = os.getenv("DOUYIN_PRIMARY_PREPROCESS_SCRIPT")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    default_path = (
        Path.home()
        / ".codex"
        / "skills"
        / "douyin-to-jianguoyun-note"
        / "scripts"
        / "process_douyin_video.py"
    )
    return default_path if default_path.exists() else None


def is_upstream_successful(payload: Dict[str, Any]) -> bool:
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    transcript = str(payload.get("transcript") or "").strip()
    return not errors and bool(transcript)


def run_browser_fallback(
    args: argparse.Namespace,
    upstream: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], int]:
    payload = base_payload(args.url)
    merge_upstream_messages(payload, upstream)
    payload["warnings"].append("已切换到浏览器 DOM 回退模式，绕过 yt-dlp 提取。")

    try:
        dom_html = render_douyin_page(args.url)
        parsed = parse_rendered_dom(dom_html, args.url)
        payload.update(
            {
                "resolved_url": parsed["resolved_url"],
                "video_id": parsed["video_id"],
                "author": parsed["author"],
                "key_visual_points": parsed["key_visual_points"],
            }
        )
        if parsed["description"]:
            payload["detected_subtitles"] = [parsed["description"]]

        with tempfile.TemporaryDirectory(prefix="douyin-browser-fallback-") as tmpdir:
            work_dir = Path(tmpdir)
            audio_path = extract_audio_from_browser_url(
                parsed["play_url"],
                work_dir / "audio.wav",
                ffmpeg_bin=args.ffmpeg_bin,
            )
            payload["duration"] = probe_duration_from_media(audio_path, args.ffprobe_bin)
            payload["transcript"] = transcribe_audio(
                audio_path,
                work_dir / "asr",
                asr_command=args.asr_command or os.getenv("DOUYIN_TO_NOTE_ASR_COMMAND"),
            ).strip()

        if not payload["transcript"]:
            payload["warnings"].append("浏览器回退拿到了媒体，但仍然没有生成 transcript。")
            if not payload["errors"]:
                payload["errors"].append("浏览器回退未能生成可用逐字稿。")
            return payload, 1
        return payload, 0
    except Exception as exc:
        payload["errors"].append(f"浏览器回退失败：{exc}")
        return payload, 1


def base_payload(source_url: str) -> Dict[str, Any]:
    return {
        "source_url": source_url,
        "resolved_url": None,
        "video_id": None,
        "author": None,
        "duration": None,
        "transcript": "",
        "detected_subtitles": [],
        "key_frames": [],
        "key_visual_points": [],
        "errors": [],
        "warnings": [],
    }


def merge_upstream_messages(payload: Dict[str, Any], upstream: Optional[Dict[str, Any]]) -> None:
    if not upstream:
        return
    warnings = upstream.get("warnings")
    if isinstance(warnings, list):
        for item in warnings:
            text = str(item).strip()
            if text:
                payload["warnings"].append(text)
    errors = upstream.get("errors")
    if isinstance(errors, list):
        for item in errors:
            text = str(item).strip()
            if text:
                payload["warnings"].append(text)


def resolve_browser_bin() -> str:
    configured = os.getenv("DOUYIN_BROWSER_BIN")
    if configured:
        return configured
    mac_default = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac_default.exists():
        return str(mac_default)
    return "google-chrome"


def render_douyin_page(url: str) -> str:
    if not DOUYIN_URL_RE.match(url.strip()):
        raise RuntimeError("链接无效：请输入单个可访问的抖音视频链接。")
    browser_bin = resolve_browser_bin()
    command = [
        browser_bin,
        "--headless=new",
        "--disable-gpu",
        "--virtual-time-budget=15000",
        "--dump-dom",
        url,
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        stderr_text = (completed.stderr or "").strip()[-1200:]
        raise RuntimeError(stderr_text or "Chrome headless 无法渲染抖音页面。")
    html_text = completed.stdout or ""
    if "<video" not in html_text:
        raise RuntimeError("浏览器回退未在页面里找到 video 标签。")
    return html_text


def parse_rendered_dom(dom_html: str, source_url: str) -> Dict[str, Any]:
    title_match = TITLE_RE.search(dom_html)
    title_text = html.unescape(title_match.group(1)).strip() if title_match else ""
    title_text = title_text.rsplit(" - 抖音", 1)[0].strip()

    desc_match = META_DESC_RE.search(dom_html)
    description = html.unescape(desc_match.group(1)).strip() if desc_match else ""

    raw_sources = [html.unescape(item).strip() for item in SOURCE_RE.findall(dom_html)]
    play_url = choose_play_url(raw_sources)
    if not play_url:
        raise RuntimeError("浏览器回退未找到可播放媒体地址。")

    publish_match = PUBLISH_TIME_RE.search(dom_html)
    publish_time = publish_match.group(1).strip() if publish_match else ""

    author = ""
    if description:
        author_match = AUTHOR_FROM_DESC_RE.search(description)
        if author_match:
            author = author_match.group(1).strip()

    chapters = []
    seen = set()
    for timestamp, chapter_title in CHAPTER_RE.findall(dom_html):
        ts = html.unescape(timestamp).strip()
        chapter = html.unescape(chapter_title).strip()
        if not ts or not chapter:
            continue
        key = (ts, chapter)
        if key in seen:
            continue
        seen.add(key)
        chapters.append(f"[{ts}] {chapter}")

    if publish_time:
        chapters.append(f"发布时间: {publish_time}")
    if title_text:
        chapters.insert(0, f"标题: {title_text}")

    resolved_url = choose_resolved_url(dom_html, source_url)
    video_id = extract_video_id(resolved_url)
    return {
        "resolved_url": resolved_url,
        "video_id": video_id,
        "author": author or None,
        "description": description or title_text,
        "play_url": play_url,
        "key_visual_points": chapters,
    }


def choose_resolved_url(dom_html: str, source_url: str) -> str:
    for pattern in (CANONICAL_RE, OG_URL_RE):
        match = pattern.search(dom_html)
        if match:
            candidate = html.unescape(match.group(1)).strip()
            if candidate.startswith("https://"):
                return candidate
    return source_url


def extract_video_id(url: str) -> Optional[str]:
    match = VIDEO_ID_RE.search(url or "")
    if match:
        return match.group(1)
    parsed = urlparse(url or "")
    segment = parsed.path.rstrip("/").split("/")[-1]
    return segment or None


def choose_play_url(urls: Sequence[str]) -> str:
    candidates = [url for url in urls if url.startswith("https://")]
    for url in candidates:
        if "/aweme/v1/play/" in url:
            return url
    for url in candidates:
        if "mime_type=video_mp4" in url:
            return url
    return candidates[0] if candidates else ""


def extract_audio_from_browser_url(play_url: str, audio_path: Path, *, ffmpeg_bin: str) -> Path:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        play_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(audio_path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0 or not audio_path.exists():
        stderr_text = (completed.stderr or "").strip()[-1200:]
        raise RuntimeError(stderr_text or "ffmpeg 未能从浏览器播放地址提取音频。")
    return audio_path


def probe_duration_from_media(media_path: Path, ffprobe_bin: str) -> Optional[float]:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        return float((completed.stdout or "").strip())
    except ValueError:
        return None


def transcribe_audio(audio_path: Path, output_dir: Path, *, asr_command: Optional[str]) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    if asr_command:
        return run_text_adapter(
            command_template=asr_command,
            placeholders={"audio_path": str(audio_path), "output_dir": str(output_dir)},
        )

    command = [
        sys.executable or "python3",
        "-m",
        "whisper",
        str(audio_path),
        "--model",
        os.getenv("DOUYIN_ASR_MODEL", "base"),
        "--language",
        os.getenv("DOUYIN_ASR_LANGUAGE", "zh"),
        "--output_format",
        "json",
        "--output_dir",
        str(output_dir),
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        stderr_text = (completed.stderr or "").strip()[-1200:]
        raise RuntimeError(stderr_text or "whisper 转写失败。")
    json_files = sorted(output_dir.glob("*.json"))
    if not json_files:
        return ""
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"].strip()
    return ""


def run_text_adapter(command_template: str, placeholders: Dict[str, str]) -> str:
    try:
        formatted = command_template.format(**placeholders)
    except KeyError as exc:
        raise RuntimeError(f"asr 命令模板缺少占位符 {exc}.") from exc

    command = shlex.split(formatted)
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        stderr_text = (completed.stderr or "").strip()[-1200:]
        raise RuntimeError(stderr_text or "外部 ASR 命令执行失败。")
    stdout = (completed.stdout or "").strip()
    if stdout:
        return parse_textual_output(stdout)

    for path in sorted(Path(placeholders["output_dir"]).glob("*")):
        if path.suffix.lower() in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
        if path.suffix.lower() == ".json":
            return parse_textual_output(path.read_text(encoding="utf-8", errors="ignore"))
    return ""


def parse_textual_output(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return output.strip()

    if isinstance(payload, dict):
        if isinstance(payload.get("text"), str):
            return payload["text"].strip()
        if isinstance(payload.get("transcript"), str):
            return payload["transcript"].strip()
    return output.strip()


if __name__ == "__main__":
    raise SystemExit(main())

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_douyin_video_browser_fallback.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("douyin_browser_fallback", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DouyinBrowserFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_parse_rendered_dom_prefers_canonical_video_url_for_shortlinks(self) -> None:
        dom_html = """
        <html>
          <head>
            <title>近期的AI革命新闻与人类重启 - 抖音</title>
            <meta name="description" content="近期的AI革命新闻与人类重启 - 水球泡于20260329发布在抖音">
            <link rel="canonical" href="https://www.douyin.com/video/7622664633823907118">
          </head>
          <body>
            <video>
              <source src="https://www.douyin.com/aweme/v1/play/?video_id=v123">
            </video>
            <div class="McB9zXIU">00:12</div><div class="fQx33EPI">脑机接口纳入医保</div>
          </body>
        </html>
        """

        parsed = self.module.parse_rendered_dom(dom_html, "https://v.douyin.com/cAx5c_NfQ5I/")

        self.assertEqual(parsed["resolved_url"], "https://www.douyin.com/video/7622664633823907118")
        self.assertEqual(parsed["video_id"], "7622664633823907118")
        self.assertEqual(parsed["author"], "水球泡")
        self.assertEqual(
            parsed["play_url"],
            "https://www.douyin.com/aweme/v1/play/?video_id=v123",
        )
        self.assertIn("[00:12] 脑机接口纳入医保", parsed["key_visual_points"])

    def test_run_browser_fallback_produces_transcript_after_upstream_failure(self) -> None:
        args = SimpleNamespace(
            url="https://v.douyin.com/cAx5c_NfQ5I/",
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            asr_command="python3 fake_asr.py {audio_path} {output_dir}",
        )
        upstream = {
            "warnings": ["yt-dlp 被抖音拦住了"],
            "errors": ["Fresh cookies are needed"],
            "transcript": "",
        }
        dom_html = """
        <html>
          <head>
            <title>近期的AI革命新闻与人类重启 - 抖音</title>
            <meta name="description" content="近期的AI革命新闻与人类重启 - 水球泡于20260329发布在抖音">
            <link rel="canonical" href="https://www.douyin.com/video/7622664633823907118">
          </head>
          <body>
            <video>
              <source src="https://www.douyin.com/aweme/v1/play/?video_id=v123">
            </video>
            <div class="McB9zXIU">00:12</div><div class="fQx33EPI">脑机接口纳入医保</div>
          </body>
        </html>
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "audio.wav"
            audio_path.write_bytes(b"RIFFfake")
            with patch.object(self.module, "render_douyin_page", return_value=dom_html), patch.object(
                self.module,
                "extract_audio_from_browser_url",
                return_value=audio_path,
            ) as extract_audio, patch.object(
                self.module,
                "probe_duration_from_media",
                return_value=42.5,
            ), patch.object(
                self.module,
                "transcribe_audio",
                return_value="第一句\\n第二句",
            ) as transcribe:
                payload, exit_code = self.module.run_browser_fallback(args, upstream)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resolved_url"], "https://www.douyin.com/video/7622664633823907118")
        self.assertEqual(payload["video_id"], "7622664633823907118")
        self.assertEqual(payload["author"], "水球泡")
        self.assertEqual(payload["duration"], 42.5)
        self.assertEqual(payload["transcript"], "第一句\\n第二句")
        self.assertIn("yt-dlp 被抖音拦住了", payload["warnings"])
        self.assertIn("Fresh cookies are needed", "\n".join(payload["warnings"]))
        self.assertEqual(payload["errors"], [])
        extract_audio.assert_called_once()
        transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()

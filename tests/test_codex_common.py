import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_common import (
    CodexRunner,
    PromptPreparation,
    capture_workspace_snapshot_for_prompt,
    prepare_prompt_with_douyin_materials,
    run_prompt_with_workspace_write_recovery,
    verify_workspace_snapshot_for_prompt,
)


class CodexRunnerPhaseParsingTests(unittest.TestCase):
    def test_parse_exec_json_ignores_commentary_and_keeps_final_answer(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "text": "我先去看一下目录结构。",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "text": "笔记已经写进去了。",
                        },
                    }
                ),
            ]
        )

        thread_id, text = CodexRunner._parse_exec_json(stdout)

        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(text, "笔记已经写进去了。")

    def test_parse_exec_json_keeps_unphased_agent_messages(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-2"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "普通最终回复。",
                        },
                    }
                ),
            ]
        )

        thread_id, text = CodexRunner._parse_exec_json(stdout)

        self.assertEqual(thread_id, "thread-2")
        self.assertEqual(text, "普通最终回复。")


class WorkspaceWriteVerificationTests(unittest.TestCase):
    def test_verify_workspace_snapshot_flags_missing_changes_for_obsidian_write_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "学习笔记").mkdir(parents=True, exist_ok=True)

            prompt = "把这条内容整理成 Obsidian 笔记，写入 学习笔记/AI行业观察，并更新 _index.md。"
            before = capture_workspace_snapshot_for_prompt(prompt, root)
            verification = verify_workspace_snapshot_for_prompt(prompt, root, before, answer="")

            self.assertTrue(verification.required)
            self.assertFalse(verification.has_changes)
            self.assertEqual(verification.changed_paths, [])

    def test_verify_workspace_snapshot_detects_actual_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            note_path = root / "学习笔记" / "AI行业观察" / "note.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)

            prompt = "请同步到 Obsidian，写入 markdown 笔记。"
            before = capture_workspace_snapshot_for_prompt(prompt, root)
            note_path.write_text("# hello\n", encoding="utf-8")
            verification = verify_workspace_snapshot_for_prompt(prompt, root, before, answer="")

            self.assertTrue(verification.required)
            self.assertTrue(verification.has_changes)
            self.assertEqual(verification.changed_paths, ["学习笔记/AI行业观察/note.md"])
            self.assertFalse(verification.claimed_paths_present)

    def test_verify_workspace_snapshot_accepts_existing_note_and_index_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            note_path = root / "学习笔记" / "AI行业观察" / "2026-03-30-近期AI革命新闻与人类重启.md"
            index_path = note_path.parent / "_index.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text("# 近期AI革命新闻与人类重启\n", encoding="utf-8")
            index_path.write_text(
                "- [[2026-03-30-近期AI革命新闻与人类重启.md]] - existing entry\n",
                encoding="utf-8",
            )

            prompt = "3.00 复制打开抖音，帮我同步到obsidian"
            answer = (
                "已同步到 Obsidian。\n\n"
                "文件路径是 2026-03-30-近期AI革命新闻与人类重启.md，"
                "存放在 `学习笔记/AI行业观察/`。"
            )
            before = capture_workspace_snapshot_for_prompt(prompt, root)
            verification = verify_workspace_snapshot_for_prompt(prompt, root, before, answer=answer)

            self.assertTrue(verification.required)
            self.assertFalse(verification.has_changes)
            self.assertEqual(
                verification.confirmed_paths,
                ["学习笔记/AI行业观察/2026-03-30-近期AI革命新闻与人类重启.md"],
            )
            self.assertTrue(verification.claimed_paths_present)

    def test_verify_workspace_snapshot_requires_claimed_note_path_to_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            index_path = root / "学习笔记" / "AI行业观察" / "_index.md"
            index_path.parent.mkdir(parents=True, exist_ok=True)

            prompt = "帮我同步到obsidian"
            before = capture_workspace_snapshot_for_prompt(prompt, root)
            index_path.write_text(
                "- [[2026-03-30-近期AI革命新闻与人类重启.md]] - dangling entry\n",
                encoding="utf-8",
            )
            answer = "已同步到 Obsidian，文件路径是 2026-03-30-近期AI革命新闻与人类重启.md。"
            verification = verify_workspace_snapshot_for_prompt(prompt, root, before, answer=answer)

            self.assertTrue(verification.has_changes)
            self.assertTrue(verification.claimed_paths_present)
            self.assertFalse(verification.is_verified)


class RecordingCodexRunner:
    def __init__(self) -> None:
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None):
        self.calls.append((prompt, str(cwd), session_id))
        return ("thread-1", "done", "", 0)


class DouyinPromptPreparationTests(unittest.TestCase):
    def test_prepare_prompt_with_douyin_materials_injects_transcript_and_metadata(self) -> None:
        prompt = (
            "把这条抖音整理成 Obsidian 笔记并同步进去："
            "https://v.douyin.com/cAx5c_NfQ5I/"
        )
        payload = {
            "resolved_url": "https://www.douyin.com/video/7622664633823907118",
            "video_id": "7622664633823907118",
            "author": "水球泡",
            "transcript": "第一句\n第二句",
            "detected_subtitles": ["第一句", "第二句"],
            "key_visual_points": ["[00:01] 封面写着 AI 革命新闻"],
            "warnings": [],
            "errors": [],
        }

        with patch(
            "codex_common.run_douyin_preprocessor",
            return_value=payload,
        ):
            prepared = prepare_prompt_with_douyin_materials(prompt, Path("/tmp/vault"))

        self.assertIsNone(prepared.abort_message)
        self.assertIn("原始用户请求", prepared.prompt)
        self.assertIn("抖音预处理材料", prepared.prompt)
        self.assertIn("https://www.douyin.com/video/7622664633823907118", prepared.prompt)
        self.assertIn("第一句\n第二句", prepared.prompt)
        self.assertIn("[00:01] 封面写着 AI 革命新闻", prepared.prompt)
        self.assertIn(prompt, prepared.prompt)

    def test_prepare_prompt_with_douyin_materials_aborts_when_transcript_missing(self) -> None:
        prompt = "帮我把这个抖音视频整理成逐字稿：https://v.douyin.com/cAx5c_NfQ5I/"
        payload = {
            "resolved_url": "https://www.douyin.com/video/7622664633823907118",
            "video_id": "7622664633823907118",
            "author": "水球泡",
            "transcript": "",
            "detected_subtitles": [],
            "key_visual_points": [],
            "warnings": ["当前环境没有可用的 asr 组件；可通过 --asr-command 或 DOUYIN_TO_NOTE_ASR_COMMAND 注入。"],
            "errors": [],
        }

        with patch(
            "codex_common.run_douyin_preprocessor",
            return_value=payload,
        ):
            prepared = prepare_prompt_with_douyin_materials(prompt, Path("/tmp/vault"))

        self.assertIsNotNone(prepared.abort_message)
        self.assertIn("没有拿到可用逐字稿", prepared.abort_message)
        self.assertIn("ASR", prepared.abort_message.upper())

    def test_run_prompt_short_circuits_when_preparation_aborts(self) -> None:
        root = Path(tempfile.mkdtemp())
        codex = RecordingCodexRunner()

        execution = run_prompt_with_workspace_write_recovery(
            codex,
            prompt="帮我整理抖音：https://v.douyin.com/cAx5c_NfQ5I/",
            cwd=root,
            prompt_preparer=lambda prompt, cwd: PromptPreparation(
                prompt=prompt,
                abort_message="抖音预处理失败：缺少 fresh cookies.txt。",
            ),
        )

        self.assertEqual(codex.calls, [])
        self.assertEqual(execution.return_code, 0)
        self.assertIn("fresh cookies.txt", execution.answer)

    def test_run_prompt_uses_prepared_prompt_for_codex_execution(self) -> None:
        root = Path(tempfile.mkdtemp())
        codex = RecordingCodexRunner()

        execution = run_prompt_with_workspace_write_recovery(
            codex,
            prompt="原始请求",
            cwd=root,
            session_id="sess-1",
            prompt_preparer=lambda prompt, cwd: PromptPreparation(
                prompt=f"{prompt}\n\n预处理材料: 第一段逐字稿",
                abort_message=None,
            ),
        )

        self.assertEqual(execution.return_code, 0)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(codex.calls[0][2], "sess-1")
        self.assertIn("预处理材料: 第一段逐字稿", codex.calls[0][0])


if __name__ == "__main__":
    unittest.main()

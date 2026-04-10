from pathlib import Path

from personal_assistant_bridge import build_sidecar_sync_prompt, load_personal_assistant_config


def test_load_personal_assistant_config_reads_assistant_workspace() -> None:
    cfg = load_personal_assistant_config(
        {
            "PERSONAL_ASSISTANT_CWD": "/tmp/assistant",
            "PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED": "1",
        }
    )

    assert cfg is not None
    assert cfg.cwd == Path("/tmp/assistant")
    assert cfg.side_sync_enabled is True


def test_build_sidecar_sync_prompt_mentions_source_workspace() -> None:
    prompt = build_sidecar_sync_prompt(
        user_text="今天先把专利提纲改完",
        source_cwd=Path("/tmp/project-a"),
        skill_path=Path("/tmp/assistant/skills/personal-assistant-chat/SKILL.md"),
        state_dir=Path("/tmp/assistant/state"),
    )

    assert "project-a" in prompt
    assert "personal-assistant-chat" in prompt
    assert "不要向用户发送第二条飞书消息" in prompt

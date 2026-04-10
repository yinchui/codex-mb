#!/usr/bin/env python3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class PersonalAssistantConfig:
    cwd: Path
    side_sync_enabled: bool


def _parse_enabled(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized not in {"0", "false", "no", "off"}


def load_personal_assistant_config(env_map: Mapping[str, str]) -> Optional[PersonalAssistantConfig]:
    raw_cwd = str(env_map.get("PERSONAL_ASSISTANT_CWD") or "").strip()
    if not raw_cwd:
        return None
    raw_enabled = str(env_map.get("PERSONAL_ASSISTANT_SIDE_SYNC_ENABLED") or "1")
    return PersonalAssistantConfig(
        cwd=Path(raw_cwd).expanduser(),
        side_sync_enabled=_parse_enabled(raw_enabled),
    )


def build_sidecar_sync_prompt(
    user_text: str,
    source_cwd: Path,
    skill_path: Path,
    state_dir: Path,
) -> str:
    return (
        f"使用[$personal-assistant-chat]({skill_path}) 进行轻量任务提取。\n"
        f"消息来源工作区: {source_cwd}\n"
        f"仅更新 {state_dir} 目录下的状态文件。\n"
        "不要向用户发送第二条飞书消息。\n"
        f"原始飞书消息:\n{user_text}"
    )

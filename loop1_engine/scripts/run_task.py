# -*- coding: utf-8 -*-
"""Task Runner: Dispatches task specifications to Claude Code CLI and monitors execution.

Usage:
    python scripts/run_task.py specs/TASK-001_example.md
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def execute_task(spec_path: str) -> int:
    """Execute a task specification using Claude Code CLI."""
    spec_file = Path(spec_path)
    if not spec_file.is_absolute():
        spec_file = HERE / spec_path

    if not spec_file.exists():
        print(f"Error: Specification file not found: {spec_file}")
        return 1

    print(f"===================================================")
    print(f"[Task Runner] Dispatching Task: {spec_file.name}")
    print(f"===================================================")

    claude_bin = shutil.which("claude") or shutil.which("claude.cmd")
    if not claude_bin:
        print("Warning: Claude Code CLI ('claude') not found in PATH.")
        print("Install it via: npm install -g @anthropic-ai/claude-code")
        print(f"Manual instruction:")
        print(f'  claude -p "{spec_file.relative_to(HERE)} を読み込み、要件に従って実装・テスト通過まで完了してください"')
        return 1

    instruction = f"{spec_file.relative_to(HERE)} を読み込み、要件に従って実装・テスト通過まで完了してください。"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"Executing: claude -p \"{instruction}\"")
    proc = subprocess.Popen(
        [claude_bin, "-p", instruction],
        cwd=str(HERE),
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return_code = proc.wait()

    if return_code == 0:
        print(f"\n[Task Runner] Task execution completed successfully.")
    else:
        print(f"\n[Task Runner] Task execution exited with code: {return_code}")

    print(f"===================================================\n")
    return return_code


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_task.py <path_to_spec_file>")
        sys.exit(1)
    sys.exit(execute_task(sys.argv[1]))

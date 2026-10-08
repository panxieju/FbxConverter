"""Headless Unreal Editor bridge.

Verified against UE 5.4 / 5.8 on this machine:

* ``UnrealEditor-Cmd.exe <project> -ExecutePythonScript=<file> -unattended ...``
  runs the script and then quits the editor by itself.
* Inside that script ``sys.argv`` holds **only** the script path, so all job
  configuration is exchanged through environment variables and JSON files
  (``FBXCONV_JOB`` -> ``FBXCONV_RESULT``).
* Lines printed as ``FBXCONV:{...json...}`` are forwarded to the UI as
  progress events while the editor is still running.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import resources
from ..errors import CancelledError, UnrealRunError, UnrealScriptError
from ..logutil import get_logger
from .locator import UnrealInstall

_log = get_logger("unreal.runner")

EVENT_PREFIX = "FBXCONV:"
POLL_INTERVAL = 0.25
KILL_GRACE_SECONDS = 15.0
DEFAULT_TIMEOUT = 7200.0
TAIL_LINES = 60

OutputCallback = Callable[[str], None]
EventCallback = Callable[[dict[str, Any]], None]


def ue_scripts_dir() -> Path:
    """Directory holding the Python scripts that run *inside* Unreal."""
    return resources.ue_scripts_dir()


@dataclass
class UnrealScriptResult:
    """Everything a finished Unreal run produced."""

    payload: dict[str, Any]
    log_path: Path
    result_path: Path
    returncode: int
    duration_s: float
    output_tail: str
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.payload.get("ok"))

    @property
    def data(self) -> dict[str, Any]:
        value = self.payload.get("data")
        return value if isinstance(value, dict) else {}

    @property
    def warnings(self) -> list[str]:
        value = self.payload.get("warnings")
        return [str(w) for w in value] if isinstance(value, list) else []

    @property
    def error_message(self) -> str:
        if self.ok:
            return ""
        return str(self.payload.get("error") or "Unreal 脚本未返回错误详情")

    @property
    def traceback_text(self) -> str | None:
        value = self.payload.get("traceback")
        return str(value) if value else None

    def require_ok(self) -> dict[str, Any]:
        """Return ``data``, raising a descriptive error if the run failed."""
        if self.ok:
            return self.data
        raise UnrealScriptError(self.error_message, traceback_text=self.traceback_text)


class UnrealRunner:
    """Runs Python scripts inside a headless Unreal Editor process."""

    def __init__(
        self,
        install: UnrealInstall,
        *,
        project_file: Path | None,
        work_root: Path,
        null_rhi: bool = True,
        extra_args: Sequence[str] = (),
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.install = install
        self.project_file = Path(project_file) if project_file else None
        self.work_root = Path(work_root)
        self.null_rhi = null_rhi
        self.extra_args = list(extra_args)
        self.timeout = timeout

    # ------------------------------------------------------------------ public

    def run_script(
        self,
        script_name: str,
        job: dict[str, Any],
        *,
        label: str = "run",
        on_output: OutputCallback | None = None,
        on_event: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
    ) -> UnrealScriptResult:
        """Execute ``ue_scripts/<script_name>`` with ``job`` as its input."""
        run_dir = self._make_run_dir(label)
        scripts = self._stage_scripts(run_dir)
        script_path = scripts / script_name
        if not script_path.is_file():
            raise UnrealRunError(f"未找到 Unreal 脚本：{script_path}")

        job_path = run_dir / "job.json"
        result_path = run_dir / "result.json"
        log_path = run_dir / "unreal.log"
        job_path.write_text(
            json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        env = os.environ.copy()
        env["FBXCONV_JOB"] = str(job_path)
        env["FBXCONV_RESULT"] = str(result_path)
        env["FBXCONV_RUN_DIR"] = str(run_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        command = self._build_command(script_path, log_path)
        _log.info("启动 Unreal：%s", script_name)
        _log.debug("命令：%s", subprocess.list2cmdline(command))

        started = time.monotonic()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        events: list[dict[str, Any]] = []

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        stdout_path = run_dir / "stdout.log"
        try:
            stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover
            raise UnrealRunError(f"无法写入运行日志：{stdout_path}（{exc}）") from exc

        process: subprocess.Popen[str] | None = None
        cancelled = False
        try:
            with stdout_handle:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    cwd=str(run_dir),
                    bufsize=1,
                    creationflags=creationflags,
                )
                pump = threading.Thread(
                    target=self._pump_output,
                    args=(process, stdout_handle, tail, events, on_output, on_event),
                    name=f"unreal-{label}-stdout",
                    daemon=True,
                )
                pump.start()

                limit = timeout if timeout is not None else self.timeout
                deadline = started + limit
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        _log.warning("收到取消请求，正在终止 Unreal…")
                        _terminate_tree(process)
                        break
                    if time.monotonic() > deadline:
                        _log.error("Unreal 运行超时（%.0f 秒），正在终止", limit)
                        _terminate_tree(process)
                        raise UnrealRunError(
                            f"Unreal 运行超时（{limit:.0f} 秒）", log_path=log_path
                        )
                    time.sleep(POLL_INTERVAL)

                pump.join(timeout=10.0)
                returncode = process.returncode if process.returncode is not None else -1
        finally:
            if process is not None and process.poll() is None:  # pragma: no cover
                _terminate_tree(process)

        duration = time.monotonic() - started
        output_tail = "\n".join(tail)

        if cancelled:
            raise CancelledError("用户取消了转换。")

        payload = self._read_result(result_path)

        if payload is None:
            message = (
                f"Unreal 未生成结果文件（退出码 {returncode}）。"
                "请查看日志确认资源是否可加载、依赖是否缺失。"
            )
            raise UnrealRunError(message, returncode=returncode, log_path=log_path,
                                 output_tail=output_tail)

        result = UnrealScriptResult(
            payload=payload,
            log_path=log_path,
            result_path=result_path,
            returncode=returncode,
            duration_s=duration,
            output_tail=output_tail,
            events=events,
        )
        _log.info(
            "Unreal 运行结束：%s（%.1f 秒，%s）",
            label,
            duration,
            "成功" if result.ok else "失败",
        )
        return result

    # ----------------------------------------------------------------- private

    def _make_run_dir(self, label: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_dir = self.work_root / "unreal-runs" / f"{safe}-{stamp}"
        counter = 1
        while run_dir.exists():
            run_dir = run_dir.with_name(f"{safe}-{stamp}-{counter}")
            counter += 1
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _stage_scripts(self, run_dir: Path) -> Path:
        """Copy the Unreal-side scripts somewhere ``-ExecutePythonScript`` likes.

        Unreal's command-line parser is happiest with a path that has no
        spaces, so when the working directory contains any we stage into the
        OS temp directory instead.
        """
        source = ue_scripts_dir()
        target = run_dir / "scripts"
        if " " in str(target):
            temp_root = Path(tempfile.gettempdir()) / "fbxconv"
            if " " not in str(temp_root):
                target = temp_root / run_dir.name / "scripts"
        try:
            shutil.copytree(source, target, dirs_exist_ok=True)
        except OSError as exc:
            raise UnrealRunError(f"无法暂存 Unreal 脚本：{exc}") from exc
        return target

    def _build_command(self, script_path: Path, log_path: Path) -> list[str]:
        command: list[str] = [str(self.install.editor_cmd)]
        if self.project_file is not None:
            command.append(str(self.project_file))
        command.append(f"-ExecutePythonScript={script_path}")
        command.extend(
            [
                "-unattended",
                "-nosplash",
                "-nopause",
                "-stdout",
                "-FullStdOutLogOutput",
                "-NoSourceControl",
            ]
        )
        command.append(f"-abslog={log_path}")
        if self.null_rhi:
            command.append("-nullrhi")
        command.extend(self.extra_args)
        return command

    @staticmethod
    def _pump_output(
        process: subprocess.Popen[str],
        sink,
        tail: deque[str],
        events: list[dict[str, Any]],
        on_output: OutputCallback | None,
        on_event: EventCallback | None,
    ) -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover
            return
        try:
            for raw in iter(stream.readline, ""):
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                tail.append(line)
                with contextlib.suppress(OSError, ValueError):
                    sink.write(line + "\n")
                index = line.find(EVENT_PREFIX)
                if index >= 0:
                    payload_text = line[index + len(EVENT_PREFIX) :].strip()
                    try:
                        event = json.loads(payload_text)
                    except json.JSONDecodeError:
                        event = None
                    if isinstance(event, dict):
                        events.append(event)
                        if on_event is not None:
                            try:
                                on_event(event)
                            except Exception:  # pragma: no cover - UI safety
                                _log.debug("事件回调异常", exc_info=True)
                if on_output is not None:
                    try:
                        on_output(line)
                    except Exception:  # pragma: no cover - UI safety
                        _log.debug("输出回调异常", exc_info=True)
        except (OSError, ValueError):  # pragma: no cover - stream closed
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    @staticmethod
    def _read_result(result_path: Path) -> dict[str, Any] | None:
        if not result_path.is_file():
            return None
        try:
            text = result_path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            return None
        if not text.strip():
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # The script may have died half-way through writing.
            _log.warning("结果文件不是合法 JSON：%s", result_path)
            return None
        return payload if isinstance(payload, dict) else None


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """Kill the editor and any children it spawned."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
    else:  # pragma: no cover - non-Windows
        process.terminate()
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _log.error("无法终止 Unreal 进程 %s", process.pid)


__all__ = [
    "DEFAULT_TIMEOUT",
    "EVENT_PREFIX",
    "UnrealRunner",
    "UnrealScriptResult",
    "ue_scripts_dir",
]

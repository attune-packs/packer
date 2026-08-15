"""Confined, non-interactive execution of the Packer CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PackerError(ValueError):
    """An action contract or execution setup error safe to show to a caller."""


class PackerCancelled(BaseException):
    """Raised when the runner asks this action to stop."""


@dataclass
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_OUTPUT_CHARS = 1_000_000
_BLOCKED_ENV = {
    "APPDATA",
    "BASH_ENV",
    "CDPATH",
    "CHECKPOINT_DISABLE",
    "ENV",
    "GCONV_PATH",
    "HOME",
    "IFS",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LOCALAPPDATA",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "SHELLOPTS",
    "TEMP",
    "TMP",
    "TMPDIR",
    "PACKER_CACHE_DIR",
    "PACKER_CONFIG",
    "PACKER_CONFIG_DIR",
    "PACKER_EXECUTABLE",
    "PACKER_LOG",
    "PACKER_LOG_PATH",
    "PACKER_PLUGIN_PATH",
    "PACKER_RUN_UUID",
    "PACKER_WRAP_COOKIE",
    "XDG_CONFIG_HOME",
}


def _string(params: dict[str, Any], name: str, default: str | None = None) -> str:
    value = params.get(name, default)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PackerError(f"'{name}' must be a non-empty string without null bytes")
    return value


def _boolean(params: dict[str, Any], name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise PackerError(f"'{name}' must be a boolean")
    return value


def _integer(
    params: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PackerError(f"'{name}' must be an integer from {minimum} to {maximum}")
    return value


def _artifact_root() -> Path:
    raw = os.environ.get("ATTUNE_ARTIFACTS_DIR")
    if not raw:
        raise PackerError("ATTUNE_ARTIFACTS_DIR is required")
    path = Path(raw)
    if not path.is_absolute():
        raise PackerError("ATTUNE_ARTIFACTS_DIR must be absolute")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise PackerError("ATTUNE_ARTIFACTS_DIR is unavailable") from exc
    if not root.is_dir():
        raise PackerError("ATTUNE_ARTIFACTS_DIR must be a directory")
    return root


def _confined_existing(root: Path, base: Path, raw: str, name: str) -> Path:
    candidate = Path(raw)
    candidate = candidate if candidate.is_absolute() else base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackerError(f"'{name}' does not exist") from exc
    if not resolved.is_relative_to(root):
        raise PackerError(f"'{name}' must stay within ATTUNE_ARTIFACTS_DIR")
    return resolved


def _working_directory(root: Path, params: dict[str, Any]) -> Path:
    path = _confined_existing(
        root, root, _string(params, "working_directory", "."), "working_directory"
    )
    if not path.is_dir():
        raise PackerError("'working_directory' must be a directory")
    return path


def _target(root: Path, cwd: Path, params: dict[str, Any]) -> Path:
    path = _confined_existing(root, cwd, _string(params, "template"), "template")
    if not path.is_file() and not path.is_dir():
        raise PackerError("'template' must be a file or directory")
    return path


def _optional_file(root: Path, cwd: Path, params: dict[str, Any], name: str) -> Path | None:
    raw = params.get(name)
    if raw is None:
        return None
    path = _confined_existing(root, cwd, _string(params, name), name)
    if not path.is_file():
        raise PackerError(f"'{name}' must be a file")
    return path


def _output_file(root: Path, cwd: Path, params: dict[str, Any]) -> Path:
    raw = Path(_string(params, "output_file"))
    candidate = raw if raw.is_absolute() else cwd / raw
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise PackerError("'output_file' parent directory does not exist") from exc
    if not parent.is_relative_to(root):
        raise PackerError("'output_file' must stay within ATTUNE_ARTIFACTS_DIR")
    if candidate.exists() and candidate.is_dir():
        raise PackerError("'output_file' must not be a directory")
    resolved = parent / candidate.name
    if resolved.name in {"", ".", ".."}:
        raise PackerError("'output_file' is invalid")
    return resolved


def _names(params: dict[str, Any], name: str) -> list[str]:
    values = params.get(name, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or "," in value or "\x00" in value
        for value in values
    ):
        raise PackerError(f"'{name}' must be an array of non-empty names without commas")
    return values


def _variable_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float, list, dict)):
        try:
            return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PackerError("variable values must be JSON-compatible") from exc
    raise PackerError("variable values must be strings or JSON-compatible values")


def _secret_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str) and len(value) >= 4:
        values.append(value)
    elif isinstance(value, list):
        for item in value:
            values.extend(_secret_values(item))
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(_secret_values(item))
    return values


def _environment(root: Path, params: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    state = root / ".packer"
    state.mkdir(mode=0o700, exist_ok=True)
    state = state.resolve(strict=True)
    if not state.is_dir() or not state.is_relative_to(root):
        raise PackerError("Packer state directories must stay within ATTUNE_ARTIFACTS_DIR")
    directories = []
    for name in ("home", "cache", "plugins", "tmp"):
        directory = state / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = directory.resolve(strict=True)
        if not directory.is_dir() or not directory.is_relative_to(root):
            raise PackerError("Packer state directories must stay within ATTUNE_ARTIFACTS_DIR")
        os.chmod(directory, 0o700)
        directories.append(directory)
    os.chmod(state, 0o700)
    home, cache, plugins, temporary = directories

    env = {
        "HOME": str(home),
        "PACKER_CACHE_DIR": str(cache),
        "PACKER_PLUGIN_PATH": str(plugins),
        "TMPDIR": str(temporary),
        "CHECKPOINT_DISABLE": "1",
    }
    for name in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if name in os.environ:
            env[name] = os.environ[name]

    supplied = params.get("environment", {})
    if not isinstance(supplied, dict):
        raise PackerError("'environment' must be an object of string values")
    secrets: list[str] = []
    for name, value in supplied.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise PackerError("environment variable names must be portable identifiers")
        if (
            name in _BLOCKED_ENV
            or name.startswith(("ATTUNE_", "DYLD_", "LD_", "PKR_VAR_"))
        ):
            raise PackerError(f"environment variable '{name}' is reserved")
        if not isinstance(value, str) or "\x00" in value:
            raise PackerError("environment values must be strings without null bytes")
        env[name] = value
        secrets.extend(_secret_values(value))

    variables = params.get("variables", {})
    if not isinstance(variables, dict):
        raise PackerError("'variables' must be an object")
    for name, value in variables.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise PackerError("variable names must be portable identifiers")
        text = _variable_text(value)
        env[f"PKR_VAR_{name}"] = text
        secrets.extend(_secret_values(value))
        if len(text) >= 4:
            secrets.append(text)
    return env, sorted(set(secrets), key=len, reverse=True)


def _executable() -> str:
    executable = os.environ.get("PACKER_EXECUTABLE", "packer")
    if not executable or "\x00" in executable:
        raise PackerError("PACKER_EXECUTABLE is invalid")
    if os.path.isabs(executable):
        path = Path(executable)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PackerError("Packer executable is unavailable")
        return str(path)
    if os.path.basename(executable) != executable:
        raise PackerError("Packer executable is unavailable")
    resolved = shutil.which(executable)
    if resolved is None:
        raise PackerError("Packer executable is unavailable")
    return str(Path(resolved).resolve())


def _stop_process(process: subprocess.Popen[str], force: bool = False) -> None:
    if process.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> ProcessResult:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise PackerError("unable to start Packer") from exc

    previous_handler: Any = None
    can_handle_term = threading.current_thread() is threading.main_thread()

    def cancel(_signum: int, _frame: Any) -> None:
        raise PackerCancelled()

    if can_handle_term:
        previous_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return ProcessResult(
                process.returncode,
                stdout,
                stderr,
                round(time.monotonic() - started, 3),
            )
        except subprocess.TimeoutExpired:
            _stop_process(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process(process, force=True)
                stdout, stderr = process.communicate()
            return ProcessResult(
                process.returncode,
                stdout,
                stderr,
                round(time.monotonic() - started, 3),
                timed_out=True,
            )
        except BaseException:
            _stop_process(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process(process, force=True)
                process.communicate()
            raise
    finally:
        if can_handle_term:
            signal.signal(signal.SIGTERM, previous_handler)


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text, False
    return text[:_MAX_OUTPUT_CHARS] + "\n[output truncated]", True


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        row = line.split(",")
        if len(row) < 3 or len(row[0]) > 20 or not row[0].isdigit():
            continue
        events.append(
            {
                "timestamp": int(row[0]),
                "target": row[1],
                "type": row[2],
                "data": [item.replace("%!(PACKER_COMMA)", ",") for item in row[3:]],
            }
        )
    return events


def _base_arguments(operation: str, params: dict[str, Any], target: Path) -> list[str]:
    arguments = [_executable(), operation]
    if operation != "fix":
        arguments.append("-machine-readable")

    if operation in {"build", "validate"}:
        only = _names(params, "only")
        exclude = _names(params, "exclude")
        if only and exclude:
            raise PackerError("'only' and 'exclude' are mutually exclusive")
        if only:
            arguments.append(f"-only={','.join(only)}")
        if exclude:
            arguments.append(f"-except={','.join(exclude)}")

    if operation == "build":
        arguments.extend(["-color=false", "-on-error=cleanup"])
        if _boolean(params, "force"):
            arguments.append("-force")
        parallel = _integer(params, "parallel_builds", 0, 0, 100)
        arguments.append(f"-parallel-builds={parallel}")
    elif operation == "validate":
        if _boolean(params, "syntax_only"):
            arguments.append("-syntax-only")
        if _boolean(params, "evaluate_datasources"):
            arguments.append("-evaluate-datasources")
    elif operation == "init":
        if _boolean(params, "upgrade"):
            arguments.append("-upgrade")
        if _boolean(params, "force"):
            arguments.append("-force")

    return arguments + [str(target)]


def execute(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    if operation not in {"build", "validate", "inspect", "init", "fix"}:
        raise PackerError("unsupported Packer action")
    root = _artifact_root()
    cwd = _working_directory(root, params)
    target = _target(root, cwd, params)
    if operation == "fix" and not target.is_file():
        raise PackerError("'template' must be a file for fix")
    destination = _output_file(root, cwd, params) if operation == "fix" else None
    timeout = _integer(params, "timeout_seconds", 900 if operation == "build" else 120, 1, 7200)
    env, secrets = _environment(root, params)
    arguments = _base_arguments(operation, params, target)

    variables_file = _optional_file(root, cwd, params, "variables_file")
    if variables_file is not None:
        if operation not in {"build", "validate"}:
            raise PackerError("'variables_file' is supported only by build and validate")
        arguments.insert(-1, f"-var-file={variables_file}")

    result = _run(arguments, cwd, env, timeout)
    stdout = _redact(result.stdout, secrets)
    stderr = _redact(result.stderr, secrets)
    output_file: str | None = None
    if operation == "fix" and result.exit_code == 0 and not result.timed_out:
        assert destination is not None
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(result.stdout)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        output_file = str(destination)
        stdout = ""

    stdout, stdout_truncated = _bounded(stdout)
    stderr, stderr_truncated = _bounded(stderr)
    response: dict[str, Any] = {
        "operation": operation,
        "success": result.exit_code == 0 and not result.timed_out,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "command": [Path(arguments[0]).name, *arguments[1:]],
        "working_directory": str(cwd),
        "stdout": stdout,
        "stderr": stderr,
        "output_truncated": stdout_truncated or stderr_truncated,
        "events": _events(stdout) if operation != "fix" else [],
    }
    if output_file is not None:
        response["output_file"] = output_file
    return response

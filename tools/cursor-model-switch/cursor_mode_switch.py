#!/usr/bin/env python3
"""Switch Cursor's saved OpenAI API key toggle on this computer.

Verified against the Cursor 3.22.7 workbench (linux x64 build
37076c6c3f9e253c0fa2305197e45befd13a2268):

- "Use OpenAI API Key" is the boolean ``useOpenAIKey`` on the
  ``applicationUser`` reactive-storage JSON in ``state.vscdb``.
- "Override OpenAI Base URL" is not a boolean. The checkbox is on when
  ``openAIBaseUrl`` is a non-empty string. Turning it off sets that field
  to null. ``grok`` copies a non-empty value into a user-only sidecar next
  to ``state.vscdb`` and then sets null. ``local`` restores it from that
  sidecar when the database value is empty.
- The key string lives in other rows (``cursorAuth/openAIKey`` or
  ``secret://cursorAuth/openAIKey``). This tool does not read or write them.

Model selection is not written.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APPLICATION_USER_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
    ".persistentStorage.applicationUser"
)
FLAG = "useOpenAIKey"
MODES = {"local": True, "grok": False}
STASH_NAME = "cursor-mode-switch-baseurl.json"

_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:api[_-]?key|access[_-]?token|token|secret|password)\s*[:=]\s*\S+"
    r"|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|\b[0-9a-fA-F]{32,}\b",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(r"https?://[^/\s:@]+:[^/\s@]+@")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|password|secret)=)[^&#\s]+",
    re.IGNORECASE,
)
_BOOL_NAME_RE = re.compile(r"openai|base.?url|api.?key|override|usekey", re.IGNORECASE)
_CURSOR_COMM_EXACT = {"cursor", "cursor.exe"}


def redact(text: str) -> str:
    """Hide keys, tokens, and credential-bearing URLs."""
    cleaned = _URL_USERINFO_RE.sub("https://[redacted]@", text)
    cleaned = _QUERY_SECRET_RE.sub(r"\1[redacted]", cleaned)
    cleaned = _SECRET_RE.sub("[redacted]", cleaned)
    return cleaned


def emit(text: str, stream=None) -> None:
    print(redact(text), file=stream or sys.stdout)


def default_user_dir() -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA is not set")
        return Path(appdata) / "Cursor" / "User"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Cursor" / "User"
    return home / ".config" / "Cursor" / "User"


def default_db_path() -> Path:
    return default_user_dir() / "globalStorage" / "state.vscdb"


def default_backup_root() -> Path:
    return Path(__file__).resolve().parent / "backups"


def _is_cursor_process_name(name: str) -> bool:
    base = Path(name.strip()).name
    lowered = base.lower()
    if lowered in _CURSOR_COMM_EXACT:
        return True
    # macOS keeps "Cursor Helper" processes while the app is open.
    return lowered.startswith("cursor helper")


def list_cursor_process_names() -> list[str]:
    """Return image names of running Cursor processes. Do not kill them."""
    if os.name == "nt":
        return _windows_cursor_processes()
    if Path("/proc").is_dir():
        return _proc_cursor_processes()
    return _ps_cursor_processes()


def _windows_cursor_processes() -> list[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        raise RuntimeError("tasklist failed")
    found = []
    for line in result.stdout.splitlines():
        if not line.startswith('"'):
            continue
        image = line.split('"', 2)[1]
        if _is_cursor_process_name(image):
            found.append(image)
    return found


def _proc_cursor_processes() -> list[str]:
    found = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if _is_cursor_process_name(comm):
            found.append(comm)
    return found


def _ps_cursor_processes() -> list[str]:
    result = subprocess.run(
        ["ps", "-ax", "-o", "comm="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ps failed")
    found = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name and _is_cursor_process_name(name):
            found.append(Path(name).name)
    return found


def assert_cursor_quit() -> bool:
    try:
        names = list_cursor_process_names()
    except Exception as exc:
        emit(
            "无法确认 Cursor 是否在运行（%s），已拒绝写入。请完全退出 Cursor 后重试。"
            % type(exc).__name__,
            sys.stderr,
        )
        return False
    if names:
        shown = ", ".join(sorted(set(names)))
        emit(
            "检测到 Cursor 正在运行（%s）。请先完全退出 Cursor，包括系统托盘里的图标，然后再运行。"
            "本工具不会结束 Cursor 进程。" % shown,
            sys.stderr,
        )
        return False
    return True


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    # A normal SQLite open (including mode=ro) reads -wal. mode=ro does not checkpoint.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    return conn


def decode_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8"), True
    if isinstance(value, str):
        return value, False
    raise TypeError("unexpected sqlite value type")


def load_application_user(db_path: Path):
    """Return (text, parsed, was_bytes) or raise FileNotFoundError / LookupError."""
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    conn = connect_readonly(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (APPLICATION_USER_KEY,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise LookupError("ItemTable") from exc
    finally:
        conn.close()
    if row is None:
        raise LookupError("applicationUser")
    text, was_bytes = decode_value(row[0])
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LookupError("json") from exc
    return text, parsed, was_bytes


def iter_bool_paths(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            if isinstance(value, bool):
                yield path
            elif isinstance(value, (dict, list)):
                yield from iter_bool_paths(value, path)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            path = "%s[%s]" % (prefix, index)
            if isinstance(value, bool):
                yield path
            elif isinstance(value, (dict, list)):
                yield from iter_bool_paths(value, path)


def matching_bool_names(obj) -> list[str]:
    names = []
    for path in iter_bool_paths(obj):
        if _BOOL_NAME_RE.search(path):
            names.append(path)
    return names


def flag_value(parsed):
    """Return the boolean flag, or None when the verified field is absent."""
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(FLAG)
    if isinstance(value, bool):
        return value
    return None


def url_is_set(value) -> bool:
    """True when Cursor's override checkbox would be on (non-empty string)."""
    return isinstance(value, str) and len(value) > 0


def stash_path_for(db_path: Path) -> Path:
    return db_path.parent / STASH_NAME


def read_stashed_url(path: Path):
    """Return the stashed URL, or None. Never raises for a missing or bad file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("openAIBaseUrl")
    if url_is_set(value):
        return value
    return None


def _restrict_user_only(path: Path) -> None:
    if os.name == "nt":
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    else:
        os.chmod(path, 0o600)


def save_baseurl_stash(path: Path, url: str) -> None:
    """Write the URL to a user-only JSON file. Raise OSError on failure."""
    if not url_is_set(url):
        raise ValueError("refusing to stash an empty base URL")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"openAIBaseUrl": url}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        _restrict_user_only(temporary)
        os.replace(temporary, path)
        _restrict_user_only(path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def format_url_lines(parsed, db_path: Path) -> str:
    value = parsed.get("openAIBaseUrl") if isinstance(parsed, dict) else None
    stashed = read_stashed_url(stash_path_for(db_path))
    return "Base URL 已设置: %s\n已暂存 Base URL: %s" % (
        "yes" if url_is_set(value) else "no",
        "yes" if stashed else "no",
    )


def format_known_status(parsed, db_path: Path) -> str:
    lines = [
        "useOpenAIKey: %s" % ("true" if flag_value(parsed) else "false"),
        format_url_lines(parsed, db_path),
        "API Key 字符串: 未读取、未修改",
        "模型: 未修改（不写模型选择）",
    ]
    return "\n".join(lines)


def format_unknown_status(parsed, db_path: Path) -> str:
    names = matching_bool_names(parsed) if isinstance(parsed, (dict, list)) else []
    lines = [
        "未找到布尔字段 useOpenAIKey。未写入。",
        "匹配的布尔字段名:",
    ]
    if names:
        lines.extend(names)
    else:
        lines.append("（无）")
    lines.append(format_url_lines(parsed, db_path))
    return "\n".join(lines)


def sidecar_paths(db_path: Path) -> list[Path]:
    paths = [db_path]
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.is_file():
            paths.append(sidecar)
    return paths


def create_backup_dir(backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = None
    while True:
        name = stamp if suffix is None else "%s-%s" % (stamp, suffix)
        dest = backup_root / name
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            suffix = 2 if suffix is None else suffix + 1


def backup_database(db_path: Path, backup_root: Path) -> Path:
    dest = create_backup_dir(backup_root)
    for source in sidecar_paths(db_path):
        target = dest / source.name
        if target.exists():
            raise FileExistsError(str(target))
        shutil.copy2(source, target)
    return dest


def apply_switch(db_path: Path, desired_flag: bool, clear_url: bool, restore_url) -> str:
    """Update the flag and, when requested, the base URL.

    ``clear_url`` sets ``openAIBaseUrl`` to null only when the current value is
    a non-empty string that is already in the sidecar. ``restore_url`` is
    written only when the database value is empty. A URL that is not already
    in the sidecar is never cleared.

    Returns ``ok``, ``missing``, or ``refused``.
    """
    conn = sqlite3.connect(db_path, timeout=5)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (APPLICATION_USER_KEY,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return "missing"
        text, was_bytes = decode_value(row[0])
        parsed = json.loads(text)
        if flag_value(parsed) is None:
            conn.execute("ROLLBACK")
            return "missing"
        current = parsed.get("openAIBaseUrl")
        if clear_url and url_is_set(current):
            stashed = read_stashed_url(stash_path_for(db_path))
            if stashed != current:
                conn.execute("ROLLBACK")
                return "refused"
            parsed["openAIBaseUrl"] = None
        elif url_is_set(restore_url) and not url_is_set(current):
            parsed["openAIBaseUrl"] = restore_url
        parsed[FLAG] = bool(desired_flag)
        new_text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        payload = new_text.encode("utf-8") if was_bytes else new_text
        conn.execute(
            "UPDATE ItemTable SET value = ? WHERE key = ?",
            (payload, APPLICATION_USER_KEY),
        )
        conn.execute("COMMIT")
        return "ok"
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def resolve_db(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return default_db_path()


def run_status(db_path: Path) -> int:
    emit("数据库: %s" % db_path)
    try:
        _text, parsed, _was_bytes = load_application_user(db_path)
    except FileNotFoundError:
        emit("找不到 state.vscdb。请确认这台电脑安装过 Cursor，并且至少打开过一次。", sys.stderr)
        return 1
    except LookupError as exc:
        emit("无法读取 applicationUser（%s）。未写入。" % exc, sys.stderr)
        return 1
    except sqlite3.Error:
        emit("无法打开数据库。若 Cursor 正在运行，请先完全退出后再试。", sys.stderr)
        return 1
    if flag_value(parsed) is None:
        emit(format_unknown_status(parsed, db_path))
        return 0
    emit(format_known_status(parsed, db_path))
    return 0


def _emit_result(db_path: Path, before_flag: bool, backup) -> None:
    _text, parsed, _was_bytes = load_application_user(db_path)
    after_flag = flag_value(parsed)
    emit(
        "useOpenAIKey: %s -> %s"
        % ("true" if before_flag else "false", "true" if after_flag else "false")
    )
    emit(format_url_lines(parsed, db_path))
    emit("API Key 字符串: 未修改")
    emit("模型: 未修改")
    if backup is not None:
        emit("备份: %s" % backup)


def _try_stash(db_path: Path, url: str) -> bool:
    """Copy ``url`` to the sidecar. Return False when the saved text cannot be read back."""
    path = stash_path_for(db_path)
    try:
        save_baseurl_stash(path, url)
    except (OSError, ValueError):
        return False
    return read_stashed_url(path) == url


def run_switch(mode: str, db_path: Path, backup_root: Path) -> int:
    desired = MODES[mode]
    emit("数据库: %s" % db_path)
    try:
        _text, parsed, _was_bytes = load_application_user(db_path)
    except FileNotFoundError:
        emit("找不到 state.vscdb。请确认这台电脑安装过 Cursor，并且至少打开过一次。", sys.stderr)
        return 1
    except LookupError as exc:
        emit("无法读取 applicationUser（%s）。未写入。" % exc, sys.stderr)
        return 1
    except sqlite3.Error:
        emit("无法打开数据库。若 Cursor 正在运行，请先完全退出后再试。", sys.stderr)
        return 1

    if flag_value(parsed) is None:
        emit(format_unknown_status(parsed, db_path), sys.stderr)
        return 1

    current_flag = flag_value(parsed)
    current_url = parsed.get("openAIBaseUrl")
    url_set = url_is_set(current_url)
    stashed = read_stashed_url(stash_path_for(db_path))
    restore_url = None
    clear_url = False

    if mode == "grok":
        clear_url = url_set
        needs_db = (current_flag is not False) or clear_url
        if not needs_db:
            emit("useOpenAIKey 已经是 false，Base URL 未设置，未写入。")
            emit(format_url_lines(parsed, db_path))
            emit("模型: 未修改")
            return 0
        if not assert_cursor_quit():
            return 1
        if clear_url and not _try_stash(db_path, current_url):
            emit("暂存 Base URL 失败，未清空该字段，未写入数据库。", sys.stderr)
            return 1
    else:
        if url_set:
            if not _try_stash(db_path, current_url):
                emit("暂存 Base URL 失败，数据库中的地址未删除。", sys.stderr)
                if current_flag is True:
                    return 1
        elif url_is_set(stashed):
            restore_url = stashed
        needs_db = (current_flag is not True) or (restore_url is not None)
        if not needs_db:
            emit("useOpenAIKey 已经是 true，未写入数据库。")
            emit(format_url_lines(parsed, db_path))
            emit("模型: 未修改")
            return 0
        if not assert_cursor_quit():
            return 1

    try:
        backup = backup_database(db_path, backup_root)
    except OSError:
        emit("备份失败，未写入。", sys.stderr)
        return 1

    try:
        result = apply_switch(db_path, desired, clear_url, restore_url)
    except (sqlite3.Error, json.JSONDecodeError, TypeError, UnicodeError):
        emit("写入失败。数据库应未被提交修改。备份: %s" % backup, sys.stderr)
        return 1
    if result == "refused":
        emit("Base URL 尚未确认写入暂存文件，未清空该字段。备份: %s" % backup, sys.stderr)
        return 1
    if result != "ok":
        emit("写入前 useOpenAIKey 已不存在，已回滚。备份: %s" % backup, sys.stderr)
        return 1

    _emit_result(db_path, current_flag, backup)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursor_mode_switch.py",
        description="在本机切换 Cursor 的 OpenAI API Key 开关。请先完全退出 Cursor。",
    )
    parser.add_argument(
        "command",
        choices=["local", "grok", "status"],
        help="local 打开 useOpenAIKey；grok 关闭它；status 只查看。",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        help="state.vscdb 路径。默认按 Windows / macOS / Linux 自动查找。",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="备份根目录。默认是脚本旁边的 backups/。",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        db_path = resolve_db(args.state_db)
    except RuntimeError as exc:
        emit(str(exc), sys.stderr)
        return 1
    if args.command == "status":
        return run_status(db_path)
    backup_root = args.backup_dir.expanduser().resolve() if args.backup_dir else default_backup_root()
    return run_switch(args.command, db_path, backup_root)


if __name__ == "__main__":
    sys.exit(main())

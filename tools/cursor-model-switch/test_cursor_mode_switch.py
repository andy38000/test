#!/usr/bin/env python3
"""Synthetic state.vscdb tests for cursor_mode_switch.py."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cursor_mode_switch as tool

KEY = tool.APPLICATION_USER_KEY
API_KEY = "sk-test-secret-key-1234567890"
SECRET_URL = "https://user:sk-supersecret@proxy.example/v1?api_key=sk-querysecret"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backup_root = self.root / "backups"
        self._proc = mock.patch.object(tool, "list_cursor_process_names", return_value=[])
        self._proc.start()

    def tearDown(self):
        self._proc.stop()
        self.tmp.cleanup()

    def run_cli(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = tool.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def make_wal_db(self, blob, extra=None):
        """Create a DB whose rows exist only in the WAL until a writer checkpoints."""
        db_dir = self.root / "user" / "globalStorage"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "state.vscdb"
        holder = sqlite3.connect(db_path)
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
        )
        holder.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            (KEY, dumps(blob).encode("utf-8")),
        )
        for row_key, row_value in extra or []:
            holder.execute(
                "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                (row_key, row_value),
            )
        holder.commit()
        self.addCleanup(holder.close)
        return db_path, holder

    def make_plain_db(self, blob):
        db_path = self.root / "plain" / "state.vscdb"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
        )
        conn.execute(
            "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
            (KEY, dumps(blob)),
        )
        conn.commit()
        conn.close()
        return db_path

    def read_blob(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (KEY,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)

    def read_row(self, db_path, row_key):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (row_key,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        return row[0]

    def test_redact_secrets(self):
        raw = "key=%s url=%s jwt=aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccc" % (
            API_KEY,
            SECRET_URL,
        )
        cleaned = tool.redact(raw)
        self.assertNotIn("sk-test-secret-key", cleaned)
        self.assertNotIn("sk-supersecret", cleaned)
        self.assertNotIn("sk-querysecret", cleaned)
        self.assertNotIn("user:", cleaned)
        self.assertIn("切换", tool.redact("切换 useOpenAIKey"))

    def test_local_and_grok_keep_url_and_key(self):
        blob = {
            "useOpenAIKey": False,
            "openAIBaseUrl": SECRET_URL,
            "备注": "不要动",
            "aiSettings": {
                "modelConfig": {
                    "composer": {
                        "modelName": "grok-4.6",
                        "selectedModels": [{"modelId": "grok-4.6", "parameters": []}],
                    }
                },
                "modelDefaultSwitchOnNewChat": False,
            },
        }
        extra = [
            ("cursorAuth/openAIKey", API_KEY.encode("utf-8")),
            ("secret://cursorAuth/openAIKey", b'{"type":"Buffer","data":[1,2,3]}'),
        ]
        db_path, _holder = self.make_wal_db(blob, extra)
        code, out, err = self.run_cli(
            ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("sk-", out + err)
        self.assertNotIn("supersecret", out + err)
        updated = self.read_blob(db_path)
        self.assertIs(updated["useOpenAIKey"], True)
        self.assertEqual(updated["openAIBaseUrl"], SECRET_URL)
        self.assertEqual(updated["备注"], "不要动")
        self.assertEqual(updated["aiSettings"]["modelConfig"]["composer"]["modelName"], "grok-4.6")
        self.assertEqual(self.read_row(db_path, "cursorAuth/openAIKey"), API_KEY.encode("utf-8"))
        self.assertEqual(
            self.read_row(db_path, "secret://cursorAuth/openAIKey"),
            b'{"type":"Buffer","data":[1,2,3]}',
        )
        self.assertIn("备份:", out)
        self.assertIn("未修改", out)

        code, out, err = self.run_cli(
            ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("sk-", out + err)
        self.assertNotIn("proxy.example", out + err)
        updated = self.read_blob(db_path)
        self.assertIs(updated["useOpenAIKey"], False)
        self.assertIsNone(updated["openAIBaseUrl"])
        self.assertEqual(updated["aiSettings"]["modelConfig"]["composer"]["modelName"], "grok-4.6")
        stash = db_path.parent / tool.STASH_NAME
        self.assertEqual(tool.read_stashed_url(stash), SECRET_URL)
        self.assertEqual(self.read_row(db_path, "cursorAuth/openAIKey"), API_KEY.encode("utf-8"))

    def test_backup_copies_wal_and_can_restore_previous_flag(self):
        blob = {"useOpenAIKey": False, "openAIBaseUrl": "https://api.example/v1"}
        db_path, _holder = self.make_wal_db(blob)
        self.assertTrue(Path(str(db_path) + "-wal").is_file())
        code, out, err = self.run_cli(
            ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        backup = Path(out.split("备份:", 1)[1].strip().splitlines()[0].strip())
        self.assertTrue((backup / "state.vscdb").is_file())
        self.assertTrue((backup / "state.vscdb-wal").is_file())
        self.assertTrue((backup / "state.vscdb-shm").is_file())
        restored = self.read_blob(backup / "state.vscdb")
        self.assertIs(restored["useOpenAIKey"], False)
        self.assertEqual(restored["openAIBaseUrl"], "https://api.example/v1")
        self.assertIs(self.read_blob(db_path)["useOpenAIKey"], True)

    def test_backup_never_overwrites(self):
        blob = {"useOpenAIKey": False, "openAIBaseUrl": "https://api.example/v1"}
        db_path = self.make_plain_db(blob)
        frozen = datetime(2026, 9, 26, 14, 15, 30)

        class FrozenDateTime:
            @staticmethod
            def now():
                return frozen

        with mock.patch.object(tool, "datetime", FrozenDateTime):
            code1, out1, err1 = self.run_cli(
                ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
            )
            code2, out2, err2 = self.run_cli(
                ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
            )
        self.assertEqual(code1, 0, err1)
        self.assertEqual(code2, 0, err2)
        first = Path(out1.split("备份:", 1)[1].strip().splitlines()[0].strip())
        second = Path(out2.split("备份:", 1)[1].strip().splitlines()[0].strip())
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertIs(self.read_blob(first / "state.vscdb")["useOpenAIKey"], False)
        self.assertIs(self.read_blob(second / "state.vscdb")["useOpenAIKey"], True)

    def test_missing_schema_status_lists_names_only(self):
        blob = {
            "openAIBaseUrl": SECRET_URL,
            "useClaudeKey": True,
            "aiSettings": {"someOverrideEnabled": False, "modelDefaultSwitchOnNewChat": True},
        }
        db_path = self.make_plain_db(blob)
        before = db_path.read_bytes()
        code, out, err = self.run_cli(["status", "--state-db", str(db_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("aiSettings.someOverrideEnabled", out)
        self.assertNotIn("useClaudeKey", out)
        self.assertNotIn("modelDefaultSwitchOnNewChat", out)
        self.assertNotIn("sk-", out + err)
        self.assertNotIn("supersecret", out + err)
        self.assertNotIn("true", out.lower())
        self.assertNotIn("false", out.lower())
        self.assertEqual(db_path.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

    def test_missing_schema_local_does_not_write(self):
        blob = {"openAIBaseUrl": SECRET_URL, "aiSettings": {"someOverrideEnabled": True}}
        db_path = self.make_plain_db(blob)
        before = db_path.read_bytes()
        code, out, err = self.run_cli(
            ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 1)
        self.assertIn("someOverrideEnabled", err + out)
        self.assertNotIn("sk-", out + err)
        self.assertEqual(db_path.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

        code, _out, _err = self.run_cli(
            ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(db_path.read_bytes(), before)

    def test_status_known_schema_hides_secrets(self):
        blob = {"useOpenAIKey": True, "openAIBaseUrl": SECRET_URL}
        db_path, _holder = self.make_wal_db(
            blob, [("cursorAuth/openAIKey", API_KEY.encode("utf-8"))]
        )
        code, out, err = self.run_cli(["status", "--state-db", str(db_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("useOpenAIKey: true", out)
        self.assertIn("Base URL 已设置: yes", out)
        self.assertIn("已暂存 Base URL: no", out)
        self.assertNotIn("sk-", out + err)
        self.assertNotIn("proxy.example", out + err)
        self.assertNotIn("api_key", out + err)

    def test_refuse_when_cursor_running(self):
        blob = {"useOpenAIKey": True, "openAIBaseUrl": "https://api.example/v1"}
        db_path = self.make_plain_db(blob)
        before = db_path.read_bytes()
        with mock.patch.object(tool, "list_cursor_process_names", return_value=["Cursor.exe"]):
            code, out, err = self.run_cli(
                ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
            )
        self.assertEqual(code, 1)
        self.assertIn("Cursor.exe", err)
        self.assertIn("不会结束", err)
        self.assertEqual(db_path.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

    def test_refuse_when_process_check_fails(self):
        blob = {"useOpenAIKey": False, "openAIBaseUrl": None}
        db_path = self.make_plain_db(blob)
        before = db_path.read_bytes()

        def boom():
            raise RuntimeError("tasklist failed")

        with mock.patch.object(tool, "list_cursor_process_names", boom):
            code, _out, err = self.run_cli(
                ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
            )
        self.assertEqual(code, 1)
        self.assertIn("拒绝写入", err)
        self.assertNotIn("tasklist failed", err)
        self.assertEqual(db_path.read_bytes(), before)

    def test_wal_only_row_is_visible(self):
        blob = {"useOpenAIKey": False, "openAIBaseUrl": "https://api.example/v1"}
        db_path, _holder = self.make_wal_db(blob)
        alone = self.root / "alone.vscdb"
        shutil.copy2(db_path, alone)
        conn = sqlite3.connect(alone)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("SELECT value FROM ItemTable").fetchone()
        conn.close()
        code, out, err = self.run_cli(["status", "--state-db", str(db_path)])
        self.assertEqual(code, 0, err)
        self.assertIn("useOpenAIKey: false", out)
        self.assertIn("Base URL 已设置: yes", out)
        self.assertIn("已暂存 Base URL: no", out)

    def test_grok_stashes_then_clears_and_local_restores(self):
        url = "https://api.example/v1"
        db_path = self.make_plain_db({"useOpenAIKey": True, "openAIBaseUrl": url, "备注": "留着"})
        code, out, err = self.run_cli(
            ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn(url, out + err)
        cleared = self.read_blob(db_path)
        self.assertIs(cleared["useOpenAIKey"], False)
        self.assertIsNone(cleared["openAIBaseUrl"])
        self.assertEqual(cleared["备注"], "留着")
        stash = db_path.parent / tool.STASH_NAME
        self.assertEqual(stash.parent, db_path.parent)
        self.assertNotEqual(stash.resolve().anchor, "")
        self.assertEqual(tool.read_stashed_url(stash), url)
        if os.name != "nt":
            self.assertEqual(stash.stat().st_mode & 0o777, 0o600)
        code, status_out, status_err = self.run_cli(["status", "--state-db", str(db_path)])
        self.assertEqual(code, 0, status_err)
        self.assertIn("Base URL 已设置: no", status_out)
        self.assertIn("已暂存 Base URL: yes", status_out)
        self.assertNotIn(url, status_out + status_err)

        code, out, err = self.run_cli(
            ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn(url, out + err)
        restored = self.read_blob(db_path)
        self.assertIs(restored["useOpenAIKey"], True)
        self.assertEqual(restored["openAIBaseUrl"], url)
        self.assertEqual(tool.read_stashed_url(stash), url)
        self.assertIn("Base URL 已设置: yes", out)
        self.assertIn("已暂存 Base URL: yes", out)

    def test_local_keeps_database_url_and_refreshes_stash(self):
        live = "https://live.example/v1"
        other = "https://old-stash.example/v1"
        db_path = self.make_plain_db({"useOpenAIKey": False, "openAIBaseUrl": live})
        stash = db_path.parent / tool.STASH_NAME
        tool.save_baseurl_stash(stash, other)
        code, out, err = self.run_cli(
            ["local", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn(live, out + err)
        self.assertNotIn(other, out + err)
        updated = self.read_blob(db_path)
        self.assertEqual(updated["openAIBaseUrl"], live)
        self.assertIs(updated["useOpenAIKey"], True)
        self.assertEqual(tool.read_stashed_url(stash), live)

    def test_grok_refuses_to_clear_when_stash_write_fails(self):
        url = "https://keep.example/v1"
        db_path = self.make_plain_db({"useOpenAIKey": True, "openAIBaseUrl": url})
        before = db_path.read_bytes()

        def fail_write(path, saved):
            raise OSError("disk full")

        with mock.patch.object(tool, "save_baseurl_stash", side_effect=fail_write):
            code, out, err = self.run_cli(
                ["grok", "--state-db", str(db_path), "--backup-dir", str(self.backup_root)]
            )
        self.assertEqual(code, 1)
        self.assertIn("未清空", err)
        self.assertNotIn("disk full", out + err)
        self.assertNotIn(url, out + err)
        self.assertEqual(db_path.read_bytes(), before)
        self.assertIsNone(tool.read_stashed_url(db_path.parent / tool.STASH_NAME))
        self.assertFalse(self.backup_root.exists())

    def test_apply_switch_does_not_clear_url_missing_from_stash(self):
        url = "https://only-in-db.example/v1"
        db_path = self.make_plain_db({"useOpenAIKey": True, "openAIBaseUrl": url})
        tool.save_baseurl_stash(db_path.parent / tool.STASH_NAME, "https://different.example/v1")
        result = tool.apply_switch(db_path, False, True, None)
        self.assertEqual(result, "refused")
        blob = self.read_blob(db_path)
        self.assertEqual(blob["openAIBaseUrl"], url)
        self.assertIs(blob["useOpenAIKey"], True)


if __name__ == "__main__":
    unittest.main()

"""Actual SDK handshake tests against an isolated read-only test server."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import json
import unittest

from tools_box.git_flow.publish import connect, discover
from tools_box.git_flow.config import Config


@unittest.skipUnless(importlib.util.find_spec("mcp"), "可选 MCP 依赖未安装")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_readonly_handshake(self):
        fixture = Path(__file__).parent / "fixtures/mcp_server.py"
        entry = {"transport": "stdio", "command": sys.executable, "args": [str(fixture)], "expected_workspace": "fixture"}
        async with connect(entry) as session:
            jobs, services = await discover(session, entry)
        self.assertEqual("dev", jobs["environment"])
        self.assertEqual(["fixture-service"], services["services"])

    async def test_http_readonly_handshake(self):
        fixture = Path(__file__).parent / "fixtures/mcp_server.py"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(fixture), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ready = False
            for _ in range(100):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    ready = True
                    break
                except OSError:
                    if process.poll() is not None:
                        self.fail("本地 HTTP 协议测试服务退出")
                    await asyncio.sleep(0.05)
            self.assertTrue(ready, "本地 HTTP 测试服务启动超时")
            os.environ["TOOLS_BOX_TEST_TOKEN"] = "fixture-only"
            entry = {"url": f"http://127.0.0.1:{port}/mcp",
                     "http_headers": {"Authorization": "Bearer fixture-only"}, "expected_workspace": "fixture"}
            async with connect(entry) as session:
                jobs, services = await discover(session, entry)
            self.assertEqual("dev", jobs["environment"])
            self.assertIn("fixture-service", services["services"])
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "config.toml"
                project = {"name": "project", "directory": directory, "mcp_url": entry["url"], "mcp_key": "fixture-only"}
                config_path.write_text("[[projects]]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in project.items()))
                config = Config(config_path)
                _, binding = config.workspace(Path(directory) / "child")
                async with connect(config.publisher(binding["publisher"])) as session:
                    _, configured_services = await discover(session, {})
                self.assertIn("fixture-service", configured_services["services"])
            legacy = {"url": entry["url"], "token_env": "TOOLS_BOX_TEST_TOKEN"}
            async with connect(legacy) as session:
                await discover(session, legacy)
            entry["http_headers"]["Authorization"] = "Bearer wrong-fixture-key"
            with self.assertRaises(Exception):
                async with connect(entry):
                    pass
        finally:
            process.terminate()
            process.wait(timeout=10)
            os.environ.pop("TOOLS_BOX_TEST_TOKEN", None)

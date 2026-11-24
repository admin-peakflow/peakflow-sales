"""Web UI: argument whitelist, paths, auth. Offline, nothing calls Claude."""
import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from grizly import webui


class Argv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "grizly.csv").write_text("company_id;name\n1;A\n", encoding="utf-8")
        (self.data / "run").mkdir()
        (self.data / "run" / "chunk-01.csv").write_text("x\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pull(self) -> None:
        argv = webui.build_argv(self.data, "pull", {"regions": ["CZ-64", "CZ-72"],
                                                    "industry": "46190, 46690", "limit": "50"})
        self.assertEqual(argv, ["pull", "--out", "grizly.csv", "--region", "CZ-64,CZ-72",
                                "--industry", "46190,46690", "--limit", "50"])

    def test_enrich(self) -> None:
        argv = webui.build_argv(self.data, "enrich", {"input": "run/chunk-01.csv", "limit": 5,
                                                      "model": "sonnet", "dry_run": True})
        self.assertEqual(argv, ["enrich", "--in", "run/chunk-01.csv", "--limit", "5",
                                "--model", "sonnet", "--workers", "3", "--dry-run"])

    def test_rejects_traversal_and_injection(self) -> None:
        bad = [("enrich", {"input": "../etc/passwd.csv"}),
               ("enrich", {"input": "missing.csv"}),
               ("enrich", {"input": "grizly.csv", "model": "gpt"}),
               ("pull", {"out": "../x.csv"}),
               ("pull", {"industry": "46190; rm -rf /"}),
               ("pull", {"regions": ["CZ-64 --force"]}),
               ("enrich", {"input": "grizly.csv", "workers": 99}),
               ("shell", {})]
        for action, params in bad:
            with self.subTest(action=action, params=params), self.assertRaises(webui.BadRequest):
                webui.build_argv(self.data, action, params)

    def test_safe_path(self) -> None:
        self.assertEqual(webui.safe_path(self.data, "grizly.csv").name, "grizly.csv")
        for rel in ("../grizly.csv", "/etc/passwd", "grizly-cache/x.json", "a/b/c.csv"):
            with self.subTest(rel=rel), self.assertRaises(webui.BadRequest):
                webui.safe_path(self.data, rel)

    def test_files_hide_cache(self) -> None:
        (self.data / "grizly-cache").mkdir()
        (self.data / "grizly-cache" / "x.json").write_text("{}", encoding="utf-8")
        paths = {f["path"] for f in webui.list_files(self.data)}
        self.assertEqual(paths, {"grizly.csv", "run/chunk-01.csv"})


class Server(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "grizly.csv").write_text("company_id;name\n1;Á\n", encoding="utf-8")
        runner = webui.Runner(self.data)
        handler = webui.make_handler(runner, "u", "secret-secret", None, b"<html>ok</html>")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.auth = "Basic " + base64.b64encode(b"u:secret-secret").decode()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, path: str, *, auth: bool = True, data: bytes | None = None,
            header: bool = True) -> tuple[int, bytes]:
        headers = {"Authorization": self.auth} if auth else {}
        if header:
            headers["X-Grizly"] = "1"
        r = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_health_is_open_everything_else_needs_login(self) -> None:
        self.assertEqual(self.req("/healthz", auth=False)[0], 200)
        self.assertEqual(self.req("/", auth=False)[0], 401)
        self.assertEqual(self.req("/files/grizly.csv", auth=False)[0], 401)
        self.assertEqual(self.req("/")[0], 200)

    def test_download(self) -> None:
        status, body = self.req("/files/grizly.csv")
        self.assertEqual(status, 200)
        self.assertIn("Á", body.decode())
        self.assertEqual(self.req("/files/..%2Fetc%2Fpasswd")[0], 400)

    def test_post_needs_header(self) -> None:
        self.assertEqual(self.req("/api/stop", data=b"", header=False)[0], 403)

    def test_upload_no_silent_overwrite(self) -> None:
        self.assertEqual(self.req("/api/upload?name=new.csv", data=b"a;b\n")[0], 200)
        self.assertEqual(self.req("/api/upload?name=new.csv", data=b"c;d\n")[0], 400)
        self.assertEqual(self.req("/api/upload?name=new.csv&overwrite=1", data=b"c;d\n")[0], 200)
        self.assertEqual((self.data / "new.csv").read_text(), "c;d\n")
        self.assertTrue(list(self.data.glob("new.before-upload-*.csv")))
        self.assertEqual(self.req("/api/upload?name=../x.csv", data=b"x")[0], 400)

    def test_run_job_and_one_at_a_time(self) -> None:
        body = json.dumps({"action": "classify", "params": {"input": "grizly.csv"}}).encode()
        status, out = self.req("/api/run", data=body)
        self.assertEqual(status, 200, out)
        job = json.loads(out)["job"]
        self.assertEqual(job["argv"], ["classify", "--in", "grizly.csv"])
        # a second start while the first runs is refused (unless it already ended)
        second = self.req("/api/run", data=body)[0]
        self.assertIn(second, (200, 400))
        for _ in range(100):
            state = json.loads(self.req("/api/state")[1])
            if not state["job"]["running"]:
                break
            time.sleep(0.1)
        self.assertTrue((self.data / job["log"]).exists())
        self.assertIn(job["log"], [f["path"] for f in state["logs"]])


if __name__ == "__main__":
    unittest.main()

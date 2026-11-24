"""Web UI: run grizly from a browser instead of a terminal. Standard library only.

    python3 grizly.py ui --port 8000          # GRIZLY_UI_PASSWORD must be set

One job at a time, each one a plain `python3 grizly.py …` subprocess in the data
directory, so the UI does exactly what the CLI does. Its output goes to
`logs/<time>-<command>.log` next to the CSVs, which the page shows live and
lets you download. Files can be downloaded and CSVs uploaded (an input list, or
the suppression lists neoslovovat.csv / klienti.csv). Nothing can be deleted
from the UI.

Security: HTTP Basic auth on every request except /healthz (password from
GRIZLY_UI_PASSWORD, the UI refuses to start without one), POSTs need the
`X-Grizly` header so another site cannot submit a form, arguments are built from
whitelisted fields (no shell), and every path stays inside the data directory.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from grizly import llm, sources
from grizly import profile as profiles

ENTRY = Path(__file__).resolve().parent.parent / "grizly.py"
LOG_DIR = "logs"
HIDDEN = {"grizly-cache", LOG_DIR}          # not listed as data files
MAX_UPLOAD = 50 * 1024 * 1024
# a file name, optionally one folder deep: run-1300/chunk-06.csv
REL_PATH = re.compile(r"^(?:[\w.-]+/)?[\w.-]+$")
CSV_NAME = re.compile(r"^[\w.-]+\.csv$")
MODELS = ("sonnet", "opus", "haiku")


class BadRequest(ValueError):
    pass


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    action: str
    argv: list[str]
    log: str
    started: float
    finished: float | None = None
    returncode: int | None = None
    pid: int | None = None

    @property
    def running(self) -> bool:
        return self.finished is None


class Runner:
    """Starts grizly subprocesses, one at a time, and remembers the last ones."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.lock = threading.Lock()
        self.current: Job | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.history: list[Job] = []

    def start(self, action: str, argv: list[str]) -> Job:
        with self.lock:
            if self.current and self.current.running:
                raise BadRequest(f"už běží úloha {self.current.action} — počkej, nebo ji zastav")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_rel = f"{LOG_DIR}/{stamp}-{action}.log"
            (self.data / LOG_DIR).mkdir(exist_ok=True)
            log_path = self.data / log_rel
            cmd = [sys.executable, "-u", str(ENTRY), *argv]
            with log_path.open("wb") as fh:
                fh.write(f"$ grizly {' '.join(argv)}\n\n".encode())
                fh.flush()
                proc = subprocess.Popen(  # list argv, no shell
                    cmd, cwd=self.data, stdout=fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
            job = Job(id=stamp, action=action, argv=argv, log=log_rel, started=time.time(),
                      pid=proc.pid)
            self.current, self.proc = job, proc
            self.history.insert(0, job)
            del self.history[20:]
            threading.Thread(target=self._wait, args=(job, proc), daemon=True).start()
            return job

    def _wait(self, job: Job, proc: subprocess.Popen[bytes]) -> None:
        rc = proc.wait()
        with self.lock:
            job.returncode, job.finished = rc, time.time()
            with (self.data / job.log).open("a", encoding="utf-8") as fh:
                fh.write(f"\n[konec, návratový kód {rc}]\n")

    def stop(self) -> bool:
        """SIGINT, like Ctrl+C: grizly writes what it has and ends."""
        with self.lock:
            if not (self.current and self.current.running and self.proc):
                return False
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                return False
            return True


# --------------------------------------------------------------------------- commands

def _int(v: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(str(v or 0).strip() or 0)
    except ValueError:
        raise BadRequest(f"{name}: není číslo") from None
    if not lo <= n <= hi:
        raise BadRequest(f"{name}: povoleno {lo}–{hi}")
    return n


def _text(v: Any, name: str, pattern: str = r"^[\w ,.\-/&'\"()+]*$", max_len: int = 200) -> str:
    s = str(v or "").strip()
    if len(s) > max_len or not re.match(pattern, s):
        raise BadRequest(f"{name}: nepovolené znaky")
    return s


def _csv_out(v: Any, default: str) -> str:
    name = str(v or "").strip() or default
    if not name.endswith(".csv"):
        name += ".csv"
    if not CSV_NAME.match(name):
        raise BadRequest("výstupní soubor: jen písmena, čísla, - _ . a přípona .csv")
    return name


def _csv_in(data: Path, v: Any) -> str:
    rel = str(v or "").strip()
    if not rel.endswith(".csv") or not REL_PATH.match(rel) or not (data / rel).is_file():
        raise BadRequest(f"vstupní soubor {rel or '(žádný)'} neexistuje")
    return rel


def build_argv(data: Path, action: str, p: dict[str, Any]) -> list[str]:
    """Form fields -> grizly arguments. Only known fields, every value checked."""
    if action == "selftest":
        return ["selftest"]
    if action == "pull":
        argv = ["pull", "--out", _csv_out(p.get("out"), "grizly.csv")]
        regions = [_text(r, "kraj", r"^[\w-]+$", 20) for r in p.get("regions") or []]
        if regions:
            argv += ["--region", ",".join(regions)]
        industry = _text(p.get("industry"), "obory", r"^[\d., ]*$", 2000)
        if industry:
            argv += ["--industry", industry.replace(" ", "")]
        limit = _int(p.get("limit"), 0, 100_000, "počet firem")
        if limit:
            argv += ["--limit", str(limit)]
        if p.get("no_detail"):
            argv.append("--no-detail")
        return argv
    if action == "enrich":
        src = _csv_in(data, p.get("input"))
        argv = ["enrich", "--in", src]
        if str(p.get("output") or "").strip():
            argv += ["--out", _csv_out(p.get("output"), "")]
        limit = _int(p.get("limit"), 0, 100_000, "počet firem")
        if limit:
            argv += ["--limit", str(limit)]
        model = str(p.get("model") or "sonnet")
        if model not in MODELS:
            raise BadRequest("neznámý model")
        argv += ["--model", model,
                 "--workers", str(_int(p.get("workers") or 3, 1, 8, "souběžně"))]
        if p.get("dry_run"):
            argv.append("--dry-run")
        return argv
    if action == "classify":
        return ["classify", "--in", _csv_in(data, p.get("input"))]
    if action == "import":
        argv = ["import", "--in", _csv_in(data, p.get("input")),
                "--out", _csv_out(p.get("output"), "grizly.csv")]
        country = _text(p.get("country"), "země", r"^[A-Za-z]{0,2}$", 2).upper()
        if country:
            argv += ["--country", country]
        for line in str(p.get("maps") or "").splitlines():
            if line.strip():
                argv += ["--map", _text(line, "mapování", r"^[\w]+=[^=\n]+$", 120)]
        return argv
    if action == "probe":
        argv = ["probe", "--id", _text(p.get("id"), "IČO", r"^[\w-]{1,20}$", 20),
                "--name", _text(p.get("name"), "název", max_len=200)]
        if not argv[-1]:
            raise BadRequest("název firmy je povinný")
        city = _text(p.get("city"), "město")
        web = _text(p.get("web"), "web", r"^[\w.:/\-]*$")
        if city:
            argv += ["--city", city]
        if web:
            argv += ["--web", web]
        if p.get("dry_run"):
            argv.append("--dry-run")
        return argv
    raise BadRequest(f"neznámá akce {action}")


# --------------------------------------------------------------------------- data

def list_files(data: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(data.rglob("*")):
        rel = path.relative_to(data)
        if (not path.is_file() or rel.parts[0] in HIDDEN or len(rel.parts) > 2
                or rel.name.startswith(".")):
            continue
        st = path.stat()
        out.append({"path": rel.as_posix(), "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def list_logs(data: Path, limit: int = 30) -> list[dict[str, Any]]:
    d = data / LOG_DIR
    if not d.is_dir():
        return []
    logs = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"path": f"{LOG_DIR}/{p.name}", "size": p.stat().st_size,
             "mtime": p.stat().st_mtime} for p in logs]


def safe_path(data: Path, rel: str) -> Path:
    if not REL_PATH.match(rel) or rel.split("/")[0] == "grizly-cache":
        raise BadRequest("neplatná cesta")
    path = (data / rel).resolve()
    if data.resolve() not in path.parents or not path.is_file():
        raise BadRequest("soubor neexistuje")
    return path


def meta(profile_name: str | None) -> dict[str, Any]:
    p = profiles.load(profile_name)
    regions: dict[str, str] = {}
    market = p.market(None) if p.markets else None
    if market:
        try:
            regions = sources.get(market.source, "grizly-ui").regions()
        except Exception:
            regions = {}
    try:
        token = llm.oauth_token()
        auth = {"ok": True, "text": f"OAuth token {token[:10]}…{token[-4:]}"}
    except RuntimeError as e:
        auth = {"ok": False, "text": str(e)}
    return {"profile": p.name, "regions": regions, "models": MODELS, "auth": auth,
            "default_codes": ",".join(market.industry_codes) if market else ""}


# --------------------------------------------------------------------------- http

def make_handler(runner: Runner, user: str, password: str, profile_name: str | None,
                 page: bytes) -> type[BaseHTTPRequestHandler]:
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "grizly-ui"

        def log_message(self, format: str, *args: Any) -> None:
            if not self.path.startswith(("/api/state", "/api/log", "/healthz")):
                sys.stderr.write(f"{self.address_string()} {format % args}\n")

        # ---------------------------------------------------------------- helpers
        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), expected.encode()):
                return True
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="grizly", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, status: int, body: bytes, ctype: str,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _error(self, msg: str, status: int = 400) -> None:
            self._json({"error": msg}, status)

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise BadRequest(f"soubor je větší než {limit // 1024 // 1024} MB")
            return self.rfile.read(n)

        def _state(self) -> dict[str, Any]:
            with runner.lock:
                job = asdict(runner.current) if runner.current else None
                if job is not None and runner.current is not None:
                    job["running"] = runner.current.running
            return {"job": job, "files": list_files(runner.data),
                    "logs": list_logs(runner.data), "now": time.time()}

        # ---------------------------------------------------------------- GET
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/healthz":
                self._send(200, b"ok", "text/plain")
                return
            if not self._authorized():
                return
            q = parse_qs(url.query)
            try:
                if url.path == "/":
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path == "/api/meta":
                    self._json(meta(profile_name))
                elif url.path == "/api/state":
                    self._json(self._state())
                elif url.path == "/api/log":
                    path = safe_path(runner.data, q.get("path", [""])[0])
                    offset = max(0, int(q.get("offset", ["0"])[0] or 0))
                    with path.open("rb") as fh:
                        size = path.stat().st_size
                        if offset > size:
                            offset = 0
                        # first view: only the tail of a long log
                        if offset == 0 and size > 200_000:
                            offset = size - 200_000
                        fh.seek(offset)
                        chunk = fh.read(1_000_000)
                    self._json({"text": chunk.decode("utf-8", "replace"),
                                "offset": offset + len(chunk)})
                elif url.path.startswith("/files/"):
                    rel = url.path[len("/files/"):]
                    path = safe_path(runner.data, rel)
                    ctype = ("text/csv; charset=utf-8" if path.suffix == ".csv"
                             else "text/plain; charset=utf-8")
                    disp = f"attachment; filename*=UTF-8''{quote(path.name)}"
                    self._send(200, path.read_bytes(), ctype, {"Content-Disposition": disp})
                else:
                    self._error("nenalezeno", 404)
            except (BadRequest, ValueError) as e:
                self._error(str(e))

        # ---------------------------------------------------------------- POST
        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.headers.get("X-Grizly") != "1":
                self._error("chybí hlavička X-Grizly", 403)
                return
            url = urlparse(self.path)
            try:
                if url.path == "/api/run":
                    body = json.loads(self._body(100_000) or b"{}")
                    action = str(body.get("action") or "")
                    argv = build_argv(runner.data, action, body.get("params") or {})
                    job = runner.start(action, argv)
                    self._json({"job": asdict(job)})
                elif url.path == "/api/stop":
                    self._json({"stopped": runner.stop()})
                elif url.path == "/api/upload":
                    name = parse_qs(url.query).get("name", [""])[0]
                    if not CSV_NAME.match(name):
                        raise BadRequest("nahrát jde jen .csv s jednoduchým názvem")
                    data = self._body(MAX_UPLOAD)
                    target = runner.data / name
                    overwrite = parse_qs(url.query).get("overwrite", [""])[0] == "1"
                    if target.exists() and not overwrite:
                        raise BadRequest(f"{name} už existuje — zaškrtni přepsat")
                    if target.exists():
                        backup = target.with_name(
                            f"{target.stem}.before-upload-{datetime.now():%Y%m%d-%H%M%S}.csv")
                        target.replace(backup)
                    tmp = target.with_suffix(".upload.tmp")
                    tmp.write_bytes(data)
                    tmp.replace(target)
                    self._json({"saved": name, "size": len(data)})
                else:
                    self._error("nenalezeno", 404)
            except (BadRequest, json.JSONDecodeError) as e:
                self._error(str(e))

    return Handler


def serve(host: str, port: int, profile_name: str | None, data: Path | None = None) -> int:
    password = os.environ.get("GRIZLY_UI_PASSWORD", "").strip().strip("\"'")
    if len(password) < 12:
        raise RuntimeError("GRIZLY_UI_PASSWORD must be set (at least 12 characters): "
                           "the UI can start paid runs and serves client data")
    user = os.environ.get("GRIZLY_UI_USER", "").strip() or "peakflow"
    data = (data or Path.cwd()).resolve()
    page = (Path(__file__).with_name("webui.html")).read_bytes()
    runner = Runner(data)
    handler = make_handler(runner, user, password, profile_name, page)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"grizly UI on http://{host}:{port}  (user {user}, data {data})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        httpd.server_close()
    return 0

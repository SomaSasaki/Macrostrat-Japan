from __future__ import annotations

# -*- coding: utf-8 -*-
"""ダッシュボードを配信するローカルサーバ（標準ライブラリのみ）。

    python run.py ui                 索引を作り直してブラウザを開く
    python run.py ui --port 8899     ポートを指定
    python run.py ui --no-browser    ブラウザを開かない
    python run.py ui --no-build      索引を作り直さず既存の JSON を使う
    python run.py ui --build-async   先にサーバを起動し、索引はあとから裏で作り直す
    python run.py ui --strict-port   ポートが埋まっていても別ポートへ逃げない

外部に開かない。既定で 127.0.0.1 のみを listen する。
``/files/...`` はリポジトリ配下の成果物（PNG・XLSX・PDF・JSONL）だけを返し、
それ以外のパスや親ディレクトリへの脱出は 403 で拒否する。

Windows でのバックグラウンド起動（pythonw.exe / VBS 経由 / コンソールなし）でも
落ちないことを設計要件とする。そのために:
  * 標準出力・標準エラーが None でも壊れない `_say()` を通してのみ出力する。
  * 何が起きたかは必ず `loop2_governance/logs/dashboard_server.log` に残す。
  * Windows では SO_REUSEADDR を使わない（他プロセスの listen を奪い、
    どちらに接続が届くか不定になるため）。代わりに SO_EXCLUSIVEADDRUSE を使う。
  * 8787 で既に同じダッシュボードが動いていれば二重起動せずブラウザだけ開く。
"""


import argparse
import http.server
import json
import mimetypes
import os
import platform
import socket
import socketserver
import subprocess
import sys
import threading
import traceback
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = (ROOT / "loop1_engine" / "dashboard") if (ROOT / "loop1_engine" / "dashboard").is_dir() else (ROOT / "dashboard")

IS_WINDOWS = os.name == "nt"
LOG_DIR = (ROOT / "loop2_governance" / "logs") if (ROOT / "loop2_governance").is_dir() else (ROOT / "logs")
LOG_FILE = LOG_DIR / "dashboard_server.log"
STATE_FILE = LOG_DIR / "dashboard_server.state.json"
MAX_LOG_BYTES = 1_000_000

# /files/ で配信を許す拡張子。実行可能ファイルや設定ファイルは渡さない。
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                    ".xlsx", ".xlsm", ".csv", ".pdf", ".kml", ".json", ".jsonl", ".txt", ".md"}
# /files/ で配信を許すディレクトリ（リポジトリ相対）。
ALLOWED_ROOTS = ("data/50k", "data/200k", "outputs", "docs", "config", "loop1_engine", "loop2_governance/data", "loop2_governance/config", "loop3_community/docs")


# --------------------------------------------------------------------------
# 出力（コンソールが無くても絶対に例外を投げない）
# --------------------------------------------------------------------------
def _log_write(line: str) -> None:
    """ログファイルへ 1 行追記する。失敗しても黙って諦める。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
                LOG_FILE.replace(LOG_FILE.with_name(LOG_FILE.name + ".1"))
        except OSError:
            pass
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"[{stamp}] pid={os.getpid()} {line}\n")
    except Exception:
        pass


def _say(msg: str = "", err: bool = False) -> None:
    """画面とログの両方に出す。stdout が None（pythonw 等）でも落ちない。"""
    _log_write(msg if msg else "")
    stream = sys.stderr if err else sys.stdout
    try:
        if stream is None:
            return
        stream.write(msg + "\n")
        stream.flush()
    except Exception:
        pass


def host_info() -> dict:
    """どの機械の、どの OS で動いているか。ログ・state・/api/health に必ず入れる。"""
    try:
        host = platform.node() or ""
    except Exception:
        host = ""
    return {"platform": platform.system() or os.name, "release": platform.release(),
            "hostname": host, "python": sys.executable, "cwd": os.getcwd()}


def sandbox_warning() -> str:
    """Windows 用の作業ツリーを Windows 以外から起動していないか判定する。

    このリポジトリは Windows 上で使われており、`.venv/Scripts/python.exe` は
    Windows の venv でしか作られない。それがあるのに実行側が Windows でない場合、
    ここは AI エージェントの隔離環境（Linux VM / クラウドコンテナ）である。
    そこで listen したサーバは利用者の Windows のブラウザからは到達できず、
    ERR_CONNECTION_REFUSED になる。過去に「サーバは 200 OK なのにブラウザから
    開けない」という誤診断を招いたため、必ず警告する。
    """
    if IS_WINDOWS:
        return ""
    if not (ROOT / ".venv" / "Scripts" / "python.exe").exists():
        return ""
    info = host_info()
    return (
        "[警告] ここは Windows ではありません "
        f"(platform={info['platform']} host={info['hostname']})。\n"
        "       この作業ツリーは Windows 用の .venv を持つため、実際の利用環境は Windows です。\n"
        "       ここで起動したサーバはこの隔離環境の中だけで listen します。\n"
        "       Windows のブラウザからは到達できません（ERR_CONNECTION_REFUSED）。\n"
        "       Windows 側で start_dashboard.bat を実行するか、\n"
        "       python run.py ui-static で dashboard_static.html を作ってダブルクリックしてください。"
    )


def _reconfigure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# --------------------------------------------------------------------------
# 索引ビルド
# --------------------------------------------------------------------------
def build_payload(quiet: bool = False) -> str:
    """索引 JSON を作り直す。失敗しても既存 JSON があればサーバは起動させる。"""
    scripts = (ROOT / "loop1_engine" / "scripts") if (ROOT / "loop1_engine" / "scripts").is_dir() else (ROOT / "scripts")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import dashboard_data
    except ImportError as exc:
        return f"索引ビルダを読み込めません: {exc}"
    try:
        payload = dashboard_data.build_index()
        dashboard_data.write_outputs(payload)
        if not quiet:
            try:
                dashboard_data.print_summary(payload)
            except Exception:
                pass                                 # 画面が無いだけで止めない
        return ""
    except BaseException as exc:                     # SystemExit / MemoryError も拾う
        _log_write("build_payload 失敗:\n" + traceback.format_exc())
        return f"索引の生成に失敗: {exc.__class__.__name__}: {exc}"


def _build_in_background() -> None:
    def worker() -> None:
        try:
            error = build_payload(quiet=True)
        except BaseException as exc:                 # ここで死んでもサーバは止めない
            _log_write("背景ビルドが異常終了:\n" + traceback.format_exc())
            error = f"{exc.__class__.__name__}: {exc}"
        _say("[WARN] 索引の更新に失敗（配信中の索引はそのまま使えます）: " + error
             if error else "[OK] 索引を更新しました（バックグラウンド）。")
    threading.Thread(target=worker, name="index-build", daemon=True).start()


# --------------------------------------------------------------------------
# ファイル配信
# --------------------------------------------------------------------------
def _safe_file(rel: str) -> Path | None:
    """リポジトリ配下の許可された成果物だけを解決する。"""
    rel = urllib.parse.unquote(rel).lstrip("/")
    if not rel or "\x00" in rel:
        return None
    candidate = (ROOT / rel).resolve() if (ROOT / rel).exists() else (ROOT / "loop2_governance" / rel).resolve() if (ROOT / "loop2_governance" / rel).exists() else (ROOT / rel).resolve()
    try:
        relative = candidate.relative_to(ROOT.resolve())
    except ValueError:
        return None                                  # リポジトリ外への脱出
    posix = relative.as_posix()
    if not any(posix == root or posix.startswith(root + "/") for root in ALLOWED_ROOTS):
        return None
    if candidate.suffix.casefold() not in ALLOWED_SUFFIXES or not candidate.is_file():
        return None
    return candidate


class Handler(http.server.SimpleHTTPRequestHandler):
    """dashboard/ を配信しつつ、/files/ と /api/ を足したハンドラ。"""

    server_version = "MacrostratDashboard/2.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    # アクセスログは 1 行に抑える（起動直後の大量ログで肝心の URL が流れないように）
    def log_message(self, fmt: str, *args) -> None:
        try:
            if self.path.startswith(("/api/", "/files/")) or self.command != "GET":
                line = f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}"
                if sys.stderr is not None:
                    sys.stderr.write(line + "\n")
                    sys.stderr.flush()
        except Exception:
            pass

    def log_error(self, fmt: str, *args) -> None:     # 既定実装は stderr 直書きで落ちうる
        try:
            _log_write("http error: " + (fmt % args))
        except Exception:
            pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self) -> None:                                    # noqa: N802
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                     # ブラウザが切っただけ
        except Exception:
            _log_write("do_GET 例外:\n" + traceback.format_exc())
            try:
                self.send_error(500, "internal error (詳細は dashboard_server.log)")
            except Exception:
                pass

    def _route(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/refresh":
            error = build_payload(quiet=True)
            self._json({"ok": not error, "error": error})
            return
        if path == "/api/health":
            self._json({"ok": True, "root": str(ROOT), "pid": os.getpid(),
                        "dashboard": str(DASHBOARD), "host": host_info(),
                        "sandbox": bool(sandbox_warning())})
            return
        if path.startswith("/files/"):
            target = _safe_file(path[len("/files/"):])
            if target is None:
                self.send_error(403, "not an allowed artifact path")
                return
            self._send_file(target)
            return
        super().do_GET()

    def _json(self, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, target: Path) -> None:
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            size = target.stat().st_size
            with target.open("rb") as handle:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                if target.suffix.casefold() in {".xlsx", ".xlsm", ".csv", ".jsonl"}:
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{target.name}"')
                self.end_headers()
                while chunk := handle.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                    # ブラウザが読み込みを中断しただけ
        except OSError as exc:
            self.send_error(500, f"read error: {exc}")


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # Windows の SO_REUSEADDR は「既に listen 中の他プロセスからポートを奪える」
    # 挙動になり、どちらに接続が届くか不定になる（= ERR_CONNECTION_REFUSED の断続再現）。
    # そのため Windows では有効にせず、代わりに排他バインドを要求する。
    allow_reuse_address = not IS_WINDOWS

    def server_bind(self) -> None:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        _log_write("接続処理で例外:\n" + traceback.format_exc())


# --------------------------------------------------------------------------
# ポート診断
# --------------------------------------------------------------------------
def _port_in_use(host: str, port: int, timeout: float = 0.4) -> bool:
    """実際に connect して「誰かが listen しているか」を確かめる。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, port)) == 0
    except OSError:
        return False


def probe_existing(host: str, port: int) -> dict | None:
    """そのポートで動いているのが「このリポジトリのダッシュボード」かを確かめる。"""
    if not _port_in_use(host, port):
        return None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as res:
            body = json.loads(res.read().decode("utf-8"))
    except Exception:
        return {"ok": False, "root": None}           # 何か別のものが占有している
    if body.get("ok") and str(body.get("root")) == str(ROOT):
        return body
    return {"ok": False, "root": body.get("root")}


def _pick_port(host: str, preferred: int, attempts: int = 20) -> int:
    """指定ポートが本当に使われていたら順に空きを探す。全滅なら OS 任せ。"""
    for offset in range(attempts):
        port = preferred + offset
        if not _port_in_use(host, port):
            return port
    return 0


# --------------------------------------------------------------------------
# ブラウザ・状態ファイル
# --------------------------------------------------------------------------
def open_url(url: str) -> None:
    """既定ブラウザで開く。webbrowser が使えない環境ではプラットフォーム手段へ落とす。"""
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    try:
        if IS_WINDOWS:
            os.startfile(url)                        # type: ignore[attr-defined]
            return
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _log_write(f"ブラウザを自動で開けませんでした: {url}")


def _write_state(url: str, port: int) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "pid": os.getpid(), "port": port, "url": url,
            "root": str(ROOT), "python": sys.executable, "host": host_info(),
            "reachable_from_user_browser": not bool(sandbox_warning()),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _clear_state() -> None:
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------
def serve(port: int = 8787, host: str = "127.0.0.1", open_browser: bool = True,
          build: bool = True, build_async: bool = False, reuse: bool = True,
          strict_port: bool = False) -> int:
    _reconfigure_streams()
    _log_write("=" * 60)
    _log_write(f"serve() 開始 python={sys.executable} root={ROOT} "
               f"port={port} build={build} build_async={build_async}")
    _log_write("host=" + json.dumps(host_info(), ensure_ascii=False))
    warning = sandbox_warning()
    if warning:
        _say(warning, err=True)
        _say("")

    if not (DASHBOARD / "index.html").is_file():
        _say(f"[ERROR] {DASHBOARD / 'index.html'} がありません。", err=True)
        return 1

    # ① 既に同じダッシュボードが動いていれば二重に立てない（ポート飛びの元凶）
    existing = probe_existing(host, port) if reuse else None
    if existing and existing.get("ok"):
        url = f"http://{host}:{port}/"
        _say(f"[OK] すでに起動しています: {url}  (pid={existing.get('pid')})")
        if open_browser:
            open_url(url)
        return 0
    if existing and not existing.get("ok"):
        _say(f"[WARN] ポート {port} は別のプログラムが使用中です。", err=True)

    # ② 索引ビルド（--build-async ならサーバ起動後に回す）
    if build and not build_async:
        error = build_payload()
        if error:
            _say(f"[WARN] {error}")
            if not (DASHBOARD / "data" / "index.json").is_file():
                _say("[ERROR] 既存の索引もないため起動できません。", err=True)
                return 1
            _say("[WARN] 既存の索引でサーバを起動します。")

    # ③ ポート確保
    if strict_port:
        chosen = port
    else:
        chosen = _pick_port(host, port)
    server = None
    for attempt in range(3):
        try:
            server = Server((host, chosen), Handler)
            break
        except OSError as exc:
            _log_write(f"bind 失敗 port={chosen}: {exc}")
            if strict_port:
                _say(f"[ERROR] ポート {chosen} を確保できません: {exc}", err=True)
                _say("      既に起動中のサーバを閉じるか stop_dashboard.bat を実行してください。", err=True)
                return 1
            chosen = _pick_port(host, chosen + 1)
    if server is None:
        _say("[ERROR] 空きポートを確保できませんでした。", err=True)
        return 1

    actual = server.server_address[1]
    url = f"http://{host}:{actual}/"
    _write_state(url, actual)

    _say("")
    _say(f"ダッシュボード: {url}")
    if actual != port:
        _say(f"[注意] 既定ポート {port} が使用中のため {actual} で起動しました。")
        _say(f"       ブックマークではなく上の URL を開いてください。")
    _say("  停止は Ctrl+C。索引の再生成は /api/refresh。")
    _say(f"  ログ: {LOG_FILE}")

    if build and build_async:
        _build_in_background()
    if open_browser:
        threading.Timer(0.8, lambda: open_url(url)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        _say("\n停止しました。")
    except BaseException:
        _log_write("serve_forever が異常終了:\n" + traceback.format_exc())
        _say("[ERROR] サーバが異常終了しました。詳細はログを参照してください。", err=True)
        return 1
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        _clear_state()
        _log_write("serve() 終了")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="進捗ダッシュボードをローカルで開く")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="索引を作り直さない")
    parser.add_argument("--build-async", action="store_true",
                        help="先にサーバを起動し、索引はあとから裏で作り直す")
    parser.add_argument("--no-reuse", action="store_true",
                        help="既に起動していても新しく立ち上げる")
    parser.add_argument("--strict-port", action="store_true",
                        help="ポートが埋まっていても別ポートへ逃げない")
    parser.add_argument("--status", action="store_true",
                        help="起動状態だけ調べて終了する")
    args = parser.parse_args(argv)

    if args.status:
        warning = sandbox_warning()
        if warning:
            _say(warning, err=True)
            _say("")
        info = probe_existing(args.host, args.port)
        if info and info.get("ok"):
            where = info.get("host") or {}
            _say(f"[OK] http://{args.host}:{args.port}/ で稼働中 (pid={info.get('pid')})")
            if where:
                _say(f"     稼働ホスト: platform={where.get('platform')} host={where.get('hostname')}")
            if info.get("sandbox"):
                _say("     [注意] このサーバは隔離環境で動いており、利用者のブラウザからは到達できません。", err=True)
                return 2
            return 0
        if info:
            _say(f"[NG] ポート {args.port} は別のプログラムが使用中です。")
            return 2
        _say(f"[NG] http://{args.host}:{args.port}/ は応答しません（未起動）。")
        return 1

    try:
        return serve(args.port, args.host, not args.no_browser, not args.no_build,
                     args.build_async, not args.no_reuse, args.strict_port)
    except BaseException:
        _log_write("main が異常終了:\n" + traceback.format_exc())
        _say("[ERROR] 起動に失敗しました。詳細は下記ログを参照してください。", err=True)
        _say(f"        {LOG_FILE}", err=True)
        return 1


if __name__ == "__main__":
    _reconfigure_streams()
    raise SystemExit(main())

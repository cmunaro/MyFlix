"""Install-state preparation and supervision for the bundled VibraVid engine."""
import fcntl
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .engine_jobs import install_stages, install_tools, read_limits

ROOT = Path(__file__).resolve().parent.parent


def prepare(database):
    import tomlkit

    pin = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["myflix"]["vibravid"]
    source = ROOT / ".vibravid"
    if not (source / "GUI" / "webgui" / "wsgi.py").is_file():
        raise RuntimeError("Bundled engine is missing. Run mise run setup first.")
    state = Path(database).resolve().parent / "vibravid"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = state / pin["revision"]
    if not runtime.exists():
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=state) as temporary:
            staged = Path(temporary) / "runtime"
            shutil.copytree(source / "GUI", staged / "GUI", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.sqlite3"))
            staged.rename(runtime)
    conf = state / "Conf"
    conf.mkdir(mode=0o700, exist_ok=True)
    for name in ("config.toml", "login.toml", "domains.json"):
        if not (conf / name).exists() and (source / "Conf" / name).is_file():
            shutil.copyfile(source / "Conf" / name, conf / name)
            (conf / name).chmod(0o600)
    config_path = conf / "config.toml"
    config = tomlkit.parse(config_path.read_text())
    # These settings belong to MyFlix's lifecycle, not a second manual setup.
    config["DEFAULT"]["auto_check_update"] = False
    config["DEFAULT"]["auto_update_binaries"] = False
    config["DOWNLOAD"]["auto_select"] = True
    config["DOWNLOAD"]["thread_count"] = 2
    config["DOWNLOAD"]["write_sidecars"] = True
    config["DOWNLOAD"]["select_video"] = "1080"
    download_limit, processing_limit = read_limits(database)
    config["ARR"]["max_concurrent_downloads"] = download_limit
    config["PROCESS"]["use_live_mux"] = False
    config["DOWNLOAD"]["use_live_decrypt"] = False
    downloads = state / "downloads"
    downloads.mkdir(exist_ok=True)
    config["OUTPUT"]["root_path"] = str(downloads)
    config["OUTPUT"]["episode_format"] = "%(series_name)/Season %(season:02d)/%(series_name) S%(season:02d)E%(episode:02d) %(episode_name)"
    hook = {
        "name": "myflix-publish", "type": "python",
        "path": str(ROOT / "deploy" / "vibravid_publish.py"),
        "args": ["--downloads", str(downloads), "--database", str(Path(database).resolve())],
        "enabled": True, "continue_on_error": False, "timeout": 3600,
    }
    hooks = [dict(item) for item in config["HOOKS"].get("post_download", []) if item.get("name") != hook["name"]]
    config["HOOKS"]["post_download"] = hooks + [hook]
    # Atomic writes retain the prior working config if serialization is interrupted.
    temporary = conf / ".config.tmp"
    with temporary.open("w", encoding="utf-8") as output:
        output.write(tomlkit.dumps(config))
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(config_path)
    return state, runtime


class ManagedEngine:
    def __init__(self, app):
        self.app = app
        self.process = None
        self.lock = None

    def start(self):
        data = Path(self.app.config["DATABASE"]).resolve().parent
        self.lock = (data / "engine.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            self.lock = None
            raise RuntimeError("Another MyFlix instance is already using this data directory") from error
        try:
            state, runtime = prepare(self.app.config["DATABASE"])
            env = dict(os.environ)
            env.update(VIBRAVID_BASE_PATH=str(state), DJANGO_DB_DIR=str(state),
                       VIBRAVID_OUTPUT_ROOT=str(state / "downloads"),
                       VIBRAVID_BINARY_DIR=str(state / "binary"),
                       VIBRAVID_BOT_SECRET=self.app.config["VIBRAVID_SECRET"],
                       MYFLIX_DATABASE=self.app.config["DATABASE"],
                       VIBRAVID_SKIP_PRE_RUN_HOOKS="1", DJANGO_SETTINGS_MODULE="webgui.settings",
                       ALLOWED_HOSTS="127.0.0.1", PYTHONDONTWRITEBYTECODE="1")
            # A dedicated readiness pipe avoids fixed ports, stale ready files,
            # log parsing, and exposing credentials in process arguments.
            reader, writer = os.pipe()
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "myflix.engine", str(runtime), str(writer)],
                    cwd=ROOT, env=env, pass_fds=(writer,), start_new_session=True)
                os.close(writer)
                writer = None
                if not select.select([reader], [], [], 120)[0]:
                    raise RuntimeError("Bundled download engine did not become ready; check the service logs")
                address = os.read(reader, 4096).decode().strip()
                if not address or self.process.poll() is not None:
                    raise RuntimeError("Bundled download engine failed during startup; check the service logs")
                self.app.extensions["vibravid"].address = address + "/"
            finally:
                os.close(reader)
                if writer is not None:
                    os.close(writer)
            logging.info("Bundled VibraVid engine ready")
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            # Include ffmpeg/download subprocesses, even if the engine exited.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            self.process = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def serve_engine(runtime, ready_fd):
    runtime = Path(runtime)
    os.chdir(runtime)
    sys.path.insert(0, str(runtime / "GUI"))
    # Migration mode suppresses upstream background workers until schema setup
    # finishes. Run it in a separate process so serving gets normal AppConfig.ready.
    subprocess.run([sys.executable, str(runtime / "GUI" / "manage.py"), "migrate", "--noinput"],
                   check=True, pass_fds=())
    from django.core.wsgi import get_wsgi_application
    from waitress.server import create_server

    download_limit, processing_limit = read_limits(os.environ["MYFLIX_DATABASE"])
    install_tools(runtime.parent, processing_limit)
    application = get_wsgi_application()
    install_stages(download_limit, processing_limit)
    from .engine_api import install
    install()
    server = create_server(application, host="127.0.0.1", port=0, threads=2)
    os.write(ready_fd, f"http://127.0.0.1:{server.effective_port}\n".encode())
    os.close(ready_fd)
    server.run()


if __name__ == "__main__":
    serve_engine(sys.argv[1], int(sys.argv[2]))

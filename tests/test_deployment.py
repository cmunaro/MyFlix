"""Exercise deployment orchestration without root, apt, or a systemd host."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent

# All privileged/network operations are replaced; bash still executes the actual
# orchestration, archive extraction, release creation, and symlink switching.
COMMAND = r'''#!/usr/bin/env python3
import io
import os
from pathlib import Path
import sys
import tarfile

name = Path(sys.argv[0]).name
args = sys.argv[1:]
base = Path(os.environ["DEPLOY_TEST"])
mode = os.environ.get("DEPLOY_MODE", "")
with (base / "commands").open("a") as log:
    log.write(name + " " + " ".join(args) + "\n")
if name == "id":
    print("0" if args == ["-u"] else "myflix")
elif name == "uname":
    print("Linux")
elif name == "getconf":
    print("64")
elif name == "flock" and mode == "locked":
    sys.exit(1)
elif name == "git":
    if "status" in args and mode == "dirty":
        print(" M README.md")
    elif args[:1] == ["pull"] and mode == "pull-failure":
        sys.exit(1)
    elif "archive" in args:
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            data = b"[Service]\nWorkingDirectory=/opt/myflix/current\n"
            entry = tarfile.TarInfo("deploy/myflix.service")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
elif name == "mise":
    if args == ["run", "setup"]:
        if mode == "setup-failure":
            sys.exit(1)
        executable = Path.cwd() / ".venv/bin/python"
        executable.parent.mkdir(parents=True)
        executable.symlink_to(sys.executable)
elif name == "systemctl":
    if args[:1] == ["is-active"]:
        sys.exit(0 if (base / "opt/myflix/current").exists() else 3)
elif name == "systemd-run":
    sys.stdin.read()
    if mode == "health-failure":
        sys.exit(1)
elif name == "mv":
    os.replace(args[-2], args[-1])
'''


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        command = self.bin / "command"
        command.write_text(COMMAND)
        command.chmod(0o755)
        for name in ("id", "uname", "getconf", "apt-get", "getent", "useradd",
                     "usermod", "flock", "git", "systemctl", "systemd-run",
                     "mv", "runuser"):
            (self.bin / name).symlink_to(command)
        self.prefix = self.base / "opt/myflix"
        (self.prefix / ".mise/bin").mkdir(parents=True)
        (self.prefix / ".mise/bin/mise").symlink_to(command)
        for path in ("run/systemd/system", "run/lock", "etc/systemd/system",
                     "srv/media/library"):
            (self.base / path).mkdir(parents=True)
        self.env_file = self.base / "etc/myflix.env"
        self.env_file.write_text("MYFLIX_PORT=9000\n")
        self.source = self.base / "checkout"
        self.source.mkdir()
        script = (ROOT / "scripts/deploy.sh").read_text()
        for path in ("/opt/myflix", "/etc/systemd/system/myflix.service",
                     "/run/systemd/system", "/run/lock/myflix-deploy.lock",
                     "/srv/media/library", "/etc/myflix.env"):
            script = script.replace(path, str(self.base) + path)
        self.script = self.base / "deploy.sh"
        self.script.write_text(script)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        DEPLOY_TEST=str(self.base))

    def previous(self):
        release = self.prefix / "releases/previous"
        release.mkdir(parents=True)
        (self.prefix / "current").symlink_to(release)
        return release

    def deploy(self, mode=""):
        result = subprocess.run(["bash", str(self.script), str(self.source)],
                                env=dict(self.env, DEPLOY_MODE=mode),
                                capture_output=True, text=True)
        self.commands = (self.base / "commands").read_text()
        return result

    def test_install_creates_release_and_preserves_configuration_and_media(self):
        media = self.base / "srv/media/library/video.mp4"
        media.write_bytes(b"video")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / "current/.venv/bin/python").exists())
        self.assertEqual(self.env_file.read_text(), "MYFLIX_PORT=9000\n")
        self.assertEqual(media.read_bytes(), b"video")
        self.assertIn("systemctl enable myflix", self.commands)
        self.assertIn("systemd-run", self.commands)

    def test_update_prepares_before_stopping(self):
        previous = self.previous()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual((self.prefix / "current").resolve(), previous)
        self.assertTrue(previous.exists())
        self.assertLess(self.commands.index("mise run setup"),
                        self.commands.index("systemctl stop myflix"))
        self.assertLess(self.commands.index("systemctl stop myflix"),
                        self.commands.index("systemctl start myflix"))

    def test_setup_failure_keeps_previous_running(self):
        previous = self.previous()
        self.assertNotEqual(self.deploy("setup-failure").returncode, 0)
        self.assertEqual((self.prefix / "current").resolve(), previous)
        self.assertNotIn("systemctl stop", self.commands)
        self.assertNotIn("systemctl start", self.commands)

    def test_failed_health_restores_previous_release(self):
        previous = self.previous()
        self.assertNotEqual(self.deploy("health-failure").returncode, 0)
        self.assertEqual((self.prefix / "current").resolve(), previous)
        self.assertEqual(self.commands.count("systemctl start myflix"), 2)

    def test_failed_first_install_removes_active_symlink(self):
        self.assertNotEqual(self.deploy("health-failure").returncode, 0)
        self.assertFalse((self.prefix / "current").is_symlink())
        self.assertEqual(self.commands.count("systemctl start myflix"), 1)

    def test_dirty_checkout_and_concurrent_deployment_stop_before_mutation(self):
        for mode in ("dirty", "locked"):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.deploy(mode).returncode, 0)
                self.assertNotIn("apt-get", self.commands)

    def test_update_refuses_dirty_checkout_or_failed_pull(self):
        (self.source / "update.sh").write_text((ROOT / "update.sh").read_text())
        (self.source / "install.sh").write_text("echo DEPLOYED\n")
        for mode in ("dirty", "pull-failure"):
            with self.subTest(mode=mode):
                result = subprocess.run(["bash", str(self.source / "update.sh")],
                                        env=dict(self.env, DEPLOY_MODE=mode, SUDO_USER=""),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("DEPLOYED", result.stdout)

    def test_update_pulls_then_installs(self):
        (self.source / "update.sh").write_text((ROOT / "update.sh").read_text())
        (self.source / "install.sh").write_text("echo DEPLOYED\n")
        result = subprocess.run(["bash", str(self.source / "update.sh")],
                                env=dict(self.env, SUDO_USER=""), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEPLOYED", result.stdout)
        self.assertIn("git pull --ff-only", (self.base / "commands").read_text())

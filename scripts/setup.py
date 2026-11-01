"""Install the pinned engine source and the locked application dependencies."""
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def install_source():
    pin = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["myflix"]["vibravid"]
    target = ROOT / ".vibravid"
    if not target.exists():
        # Build beside the destination so a failed fetch never leaves a partial
        # checkout that a later setup mistakes for an installed engine.
        with tempfile.TemporaryDirectory(prefix=".engine-setup-", dir=ROOT) as staging:
            source = Path(staging) / "source"
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "remote", "add", "origin", pin["repository"]], check=True)
            subprocess.run(["git", "-C", str(source), "fetch", "--depth=1", "origin", pin["revision"]], check=True)
            subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
            source.rename(target)
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
    if actual != pin["revision"]:
        raise RuntimeError("Engine source differs from the project pin; preserve any local edits and replace .vibravid before setup")
    subprocess.run(["git", "-C", str(target), "diff", "--quiet", "HEAD"], check=True)
    return target


def main():
    os.chdir(ROOT)
    install_source()
    subprocess.run(["uv", "sync", "--frozen"], check=True)
    print("MyFlix and its pinned VibraVid engine are installed. Start with mise run dev.")


if __name__ == "__main__":
    main()

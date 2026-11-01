import logging
import os
import signal
import threading

from waitress import serve

from .app import create_app
from .engine import ManagedEngine


def main():
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    stop = threading.Event()
    engine = ManagedEngine(app)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)

    def index():
        while not stop.is_set():
            try:
                app.extensions["scan"]()
            except Exception:
                logging.exception("Library scan failed; will retry in 60 seconds")
            stop.wait(60)

    try:
        engine.start()
        threading.Thread(target=index, name="library-index", daemon=True).start()
        threading.Thread(target=app.extensions["artwork"].run, args=(stop,), name="artwork-cache", daemon=True).start()

        def monitor():
            while not stop.wait(1):
                if engine.process.poll() is not None:
                    logging.error("Bundled engine exited; stopping MyFlix so systemd can restart it")
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

        threading.Thread(target=monitor, name="engine-monitor", daemon=True).start()
        serve(app, host=os.environ.get("MYFLIX_HOST", "0.0.0.0"),
              port=int(os.environ.get("MYFLIX_PORT", "8090")), threads=4)
        if engine.process.poll() is not None:
            raise RuntimeError("Bundled download engine exited")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        engine.close()


if __name__ == "__main__":
    main()

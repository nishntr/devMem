"""Entry point for running the daemon as a module: python -m devmem.daemon_main"""

import logging
import os
from devmem.config import load_config
from devmem.daemon import Daemon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

if __name__ == "__main__":
    config = load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    # Write PID file so `devmem daemon status/stop` can find this process.
    # start(foreground=True) skips _write_pid(), so we do it here.
    try:
        config.pid_path.write_text(str(os.getpid()))
    except OSError as exc:
        logging.warning("Could not write PID file: %s", exc)
    daemon = Daemon(config)
    daemon.start(foreground=True)

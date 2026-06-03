"""One-shot apply of the running configuration at boot.

Mirrors the startup sequence in NOSShell.__init__ without launching the
interactive shell.  Intended to be run as a systemd oneshot service before
nos-cli.service.
"""
from __future__ import annotations

import logging
import sys


def main() -> None:
    """Entry point for the ``nos-apply`` command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    from nos.config.applier import ConfigApplier
    from nos.config.store import ConfigStore
    from nos.drivers.frr import FRRClient
    from nos.drivers.kernel import KernelDriver
    from nos.pfe.manager import PFEManager

    store = ConfigStore(base_dir="/opt/nos")
    pfe = PFEManager()
    pfe.start()
    kernel_driver = KernelDriver()
    frr_client = FRRClient()
    applier = ConfigApplier(kernel_driver, frr_client, pfe)

    log.info("Applying running configuration...")
    try:
        applier.apply({}, store.get_running())
        log.info("Running configuration applied successfully.")
    except Exception as exc:
        log.error("Failed to apply running configuration: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

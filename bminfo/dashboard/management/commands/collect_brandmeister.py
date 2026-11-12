import logging
import signal
import threading

from django.core.management.base import BaseCommand

from dashboard.collector import bootstrap_talkgroups, heartbeat_loop, run_collector


class Command(BaseCommand):
    """Run the LastHeard collector.

    This process does one job: hold the socket.io connection and record that it is
    alive. Everything that used to run here — nightly raw-event cleanup, the
    talkgroup refresh loop and raw-event archiving — is now a scheduled command,
    so restarting this pod cannot interrupt them and a collector crash cannot
    take them down.

    Runs as its own Deployment, separate from the web pods, so restarting the web
    application no longer drops the live feed.
    """

    help = "Run the BrandMeister Lastheard collector (socket.io) and its heartbeat."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        stop_event = threading.Event()
        connected_event = threading.Event()

        def stop(_signum, _frame):
            self.stdout.write("collector shutdown requested")
            stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        # The heartbeat lets /status/ tell "connected and receiving" apart from
        # "process alive but feed dead", so it belongs with the connection.
        heartbeat = threading.Thread(target=heartbeat_loop, args=(stop_event, connected_event), daemon=True)
        heartbeat.start()

        try:
            bootstrap_talkgroups()
            run_collector(stop_event, connected_event)
        finally:
            stop_event.set()
            heartbeat.join(timeout=3)

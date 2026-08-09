import logging
import signal
import threading

from django.core.management.base import BaseCommand

from dashboard.collector import heartbeat_loop, run_collector, talkgroup_loop
from dashboard.maintenance import nightly_cleanup


class Command(BaseCommand):
    help = "Run the BrandMeister Lastheard collector as a Django background job."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        stop_event = threading.Event()

        def stop(_signum, _frame):
            self.stdout.write("collector shutdown requested")
            stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        heartbeat = threading.Thread(target=heartbeat_loop, args=(stop_event,), daemon=True)
        talkgroups = threading.Thread(target=talkgroup_loop, args=(stop_event,), daemon=True)
        cleanup = threading.Thread(target=nightly_cleanup, args=(stop_event,), daemon=True)
        heartbeat.start()
        talkgroups.start()
        cleanup.start()
        run_collector(stop_event)
        stop_event.set()
        heartbeat.join(timeout=3)
        talkgroups.join(timeout=3)
        cleanup.join(timeout=3)

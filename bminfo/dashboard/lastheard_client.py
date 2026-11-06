"""Socket.IO client with a send-queue deadline matching the heartbeat window."""

from queue import Queue

import engineio
import socketio


class _HeartbeatQueue(Queue):
    def __init__(self, client, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = client

    def get(self, block=True, timeout=None):
        if block and timeout is not None:
            # Engine.IO's writer uses max(interval, timeout) + 5, which can
            # expire before the reader's interval + timeout deadline. A late
            # server ping must still be able to enqueue its pong. Keep the
            # writer alive until after the reader detects a dead connection
            # and wakes it with the normal shutdown sentinel.
            timeout = max(timeout, self.client.ping_interval + self.client.ping_timeout + 5)
        return super().get(block=block, timeout=timeout)


class _HeartbeatClient(engineio.Client):
    def create_queue(self, *args, **kwargs):
        return _HeartbeatQueue(self, *args, **kwargs)


class LastheardClient(socketio.Client):
    def _engineio_client_class(self):
        return _HeartbeatClient

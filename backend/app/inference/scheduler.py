"""Serialize GPU calls and give already waiting live frames first access."""
from contextlib import contextmanager
import threading


class InferenceScheduler:
    def __init__(self):
        self.condition = threading.Condition()
        self.busy = False
        self.live_waiters = 0

    @contextmanager
    def slot(self, *, realtime=False, cancelled=None):
        with self.condition:
            if realtime:
                self.live_waiters += 1
            try:
                while self.busy or not realtime and self.live_waiters:
                    if cancelled and cancelled():
                        raise InterruptedError()
                    self.condition.wait(.1)
                if cancelled and cancelled():
                    raise InterruptedError()
                self.busy = True
            finally:
                if realtime:
                    self.live_waiters -= 1
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()


inference_scheduler = InferenceScheduler()

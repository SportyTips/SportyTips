"""A chat turn that survives the app being minimised.

Before: the ticket was sent straight down one open connection. When the phone put the app to sleep the
connection died, the ticket was lost, and the page said "I could not reach the server".

Now every turn is a job kept on the server for 30 minutes. The page can reconnect to the same job and
continue from the last line it received (GET /api/chat/resume?job=ID&from=N). The bot keeps working while
the app is closed, and push_notify.py can tell the user when the ticket is ready.
"""

import json
import threading
import time
import uuid

JOB_SECONDS = 30 * 60
HEARTBEAT = 15

_JOBS = {}
_LOCK = threading.Lock()


class Job:

    def __init__(self, owner, text=""):
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.text = text
        self.items = []
        self.done = False
        self.hidden = False  # the page told us the app is minimised
        self.listeners = 0  # open connections reading this job
        self.created = time.time()
        self.cond = threading.Condition()
        self.on_done = None

    # the bot writes to the job exactly like it wrote to the old queue
    def put(self, item):

        with self.cond:
            if item is None:
                self.done = True
            else:
                self.items.append(item)
            self.cond.notify_all()

        if item is None and self.on_done:
            try:
                self.on_done(self)
            except Exception as exc:
                print(f"Job finished hook failed: {type(exc).__name__}: {exc}")

    def watching(self):
        """True while someone is looking at the screen and reading this job."""
        return self.listeners > 0 and not self.hidden

    def stream(self, start=0):
        """Lines for the browser, from item number `start`. A blank line every few seconds keeps it alive."""

        index = max(0, int(start or 0))

        with self.cond:
            self.listeners += 1

        try:
            yield "\n"  # sends the headers (and the job id) straight away

            while True:

                with self.cond:
                    while index >= len(self.items) and not self.done:
                        if not self.cond.wait(timeout=HEARTBEAT):
                            break

                    if index < len(self.items):
                        item = self.items[index]
                        index += 1
                    elif self.done:
                        item = {"type": "end"}
                        finished = True
                    else:
                        item = None  # heartbeat

                if item is None:
                    yield "\n"
                    continue

                yield json.dumps(item) + "\n"

                if item.get("type") == "end":
                    return

        finally:
            with self.cond:
                self.listeners -= 1


def new_job(owner, text=""):
    job = Job(owner, text)
    now = time.time()

    with _LOCK:
        for old in [k for k, j in _JOBS.items() if now - j.created > JOB_SECONDS]:
            _JOBS.pop(old, None)
        _JOBS[job.id] = job

    return job


def get_job(job_id, owner):
    with _LOCK:
        job = _JOBS.get(str(job_id or ""))

    if job is None or str(job.owner) != str(owner):
        return None

    return job

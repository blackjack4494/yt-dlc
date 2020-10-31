# coding: utf-8
from __future__ import unicode_literals

import time
import threading
import traceback

from .utils import (
    sanitized_Request
)


class Heartbeat(object):
    def __init__(self, ydl, params):
        self.ydl = ydl

        data = params.get('data')
        if type(data) is str:
            data = data.encode()
        self.request = sanitized_Request(
            params.get('url'),
            data=data,
            headers=params.get('headers', {}),
            method=params.get('method')
        )

        self.interval = params.get('interval', 30)
        self.stopped = False
        self.thread = threading.Thread(target=self.__heartbeat, daemon=True)

    def start(self):
        if self.ydl.params.get('verbose'):
            self.ydl.to_screen('[heartbeat] Heartbeat every %s seconds' % self.interval)
        self.thread.start()

    def stop(self):
        self.stopped = True

    def check_download_status(self, progress):
        status = progress.get('status')
        if status == 'finished' or status == 'error':
            self.stop()

    def __heartbeat(self):
        while not self.stopped:
            try:
                if self.ydl.params.get('verbose'):
                    self.ydl.to_screen('[heartbeat]')
                self.ydl.urlopen(self.request)
            except Exception:
                if self.ydl.params.get('verbose'):
                    traceback.print_exc()
                self.ydl.to_screen("[heartbeat] Heartbeat failed")
            time.sleep(self.interval)

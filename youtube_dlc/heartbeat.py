# coding: utf-8
from __future__ import unicode_literals

import threading
import traceback

from .utils import (
    compat_str,
    encode_compat_str,
    sanitized_Request
)


class Heartbeat(object):
    def __init__(self, ydl, params):
        self.ydl = ydl

        data = params.get('data')
        if isinstance(data, compat_str):
            data = data.encode()
        # Python 2 does not allow us to set HTTP method
        # it is POST if Request has data, otherwise GET
        self.request = sanitized_Request(
            params.get('url'),
            data=data,
            headers=params.get('headers', {})
        )

        self.interval = params.get('interval', 30)
        self.cancelled = False
        self.parent_thread = threading.current_thread()
        self.thread = threading.Thread(target=self.__heartbeat)

    def start(self):
        self.ydl.to_screen('[heartbeat] Heartbeat every %s seconds' % self.interval)
        self.thread.start()

    def cancel(self):
        self.cancelled = True

    def check_download_status(self, progress):
        status = progress.get('status')
        if status == 'finished' or status == 'error':
            self.cancel()

    def __heartbeat(self):
        while not self.cancelled:
            try:
                if self.ydl.params.get('verbose'):
                    self.ydl.to_screen('[heartbeat]')
                self.ydl.urlopen(self.request)
            except Exception:
                self.ydl.report_warning("Heartbeat failed")
                if self.ydl.params.get('verbose'):
                    self.ydl.to_stderr(encode_compat_str(traceback.format_exc()))
            self.parent_thread.join(self.interval)
            if not self.parent_thread.is_alive():
                break

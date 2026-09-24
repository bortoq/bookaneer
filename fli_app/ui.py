"""Terminal status display."""

import os
import sys
import threading

class Spinner:
    FRAMES = (" ", "░", "▒", "▓", "█", "▓", "▒", "░")

    def __init__(self, action):
        self.action = action
        self.stream = sys.stderr
        # MC's subshell shares a terminal with its panel renderer. Redrawing
        # the same line there can disturb its Ctrl-O screen and prompt state.
        self.animated = (self.stream.isatty()
                         and not (os.environ.get("MC_SID") or os.environ.get("MC_TMPDIR")))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def __enter__(self):
        if self.animated:
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()
        else:
            print(self.action, file=self.stream, flush=True)
        return self

    def animate(self):
        index = 0
        while not self.stop.is_set():
            with self.lock:
                self.stream.write(f"\r\033[2K{self.FRAMES[index % len(self.FRAMES)]} {self.action}")
                self.stream.flush()
            index += 1
            self.stop.wait(0.12)

    def report(self, message, stream=None):
        with self.lock:
            if self.animated:
                self.stream.write("\r\033[2K")
                self.stream.flush()
            print(message, file=stream or sys.stdout, flush=True)

    def __exit__(self, *_):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if self.animated:
            with self.lock:
                self.stream.write("\r\033[2K\n")
                self.stream.flush()



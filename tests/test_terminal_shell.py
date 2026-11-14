"""The bash integration of the terminal panel, run in a real pty."""

import os
import pty
import select
import subprocess
import tempfile
import time
import unittest

from metafiles.terminal import SEQ_RUN, SEQ_SYNC_CD, SHELL_RC


class BashPty:
    def __init__(self, root, cwd, sync_file, confine=True):
        self.home = tempfile.mkdtemp()
        env = {"HOME": self.home, "PATH": os.environ["PATH"], "TERM": "xterm", "PS1": "PROMPT[\\w]$ ",
               "METAFILES_ROOT": root, "METAFILES_CONFINE": "1" if confine else "0", "METAFILES_CONFINE_FILE": "",
               "METAFILES_SYNC_FILE": sync_file}
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(["bash", "--rcfile", SHELL_RC, "-i"], stdin=slave, stdout=slave, stderr=slave,
                                     cwd=cwd, env=env, start_new_session=True)
        os.close(slave)
        self.output = b""
        self.read_until(b"PROMPT[")

    def read_until(self, marker, timeout=5):
        end = time.time() + timeout
        while marker not in self.output and time.time() < end:
            ready, _w, _x = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    self.output += os.read(self.master, 4096)
                except OSError:
                    break
        return marker in self.output

    def send(self, data):
        os.write(self.master, data)

    def close(self):
        self.proc.kill()
        self.proc.wait()
        os.close(self.master)


class ShellSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(os.path.join(self.tmp.name, "proj"))
        os.makedirs(os.path.join(self.root, "src", "lib"))
        self.sync = os.path.join(self.tmp.name, "sync")
        self.bash = BashPty(self.root, self.root, self.sync)

    def tearDown(self):
        self.bash.close()
        self.tmp.cleanup()

    def test_sync_keeps_partial_command(self):
        self.bash.send(b"echo half")
        time.sleep(0.2)
        with open(self.sync, "w") as f:
            f.write(os.path.join(self.root, "src", "lib"))
        self.bash.send(SEQ_SYNC_CD)
        time.sleep(0.3)
        self.bash.send(b"typed; pwd\n")
        self.assertTrue(self.bash.read_until(b"halftyped\r\n"), self.bash.output[-400:])
        self.assertTrue(self.bash.read_until(os.path.join(self.root, "src", "lib").encode() + b"\r\n"))

    def test_run_command(self):
        with open(self.sync, "w") as f:
            f.write("echo ran-$((1+2))")
        self.bash.send(SEQ_RUN)
        self.assertTrue(self.bash.read_until(b"ran-3"), self.bash.output[-400:])

    def test_confinement_still_applies(self):
        self.bash.send(b"cd /; pwd\n")
        self.assertTrue(self.bash.read_until(b"blocked"), self.bash.output[-400:])


if __name__ == "__main__":
    unittest.main()

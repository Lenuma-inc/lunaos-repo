"""Logging and bounded subprocess execution shared by buildbot operations."""

import codecs
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import time
import traceback


class CommandError(RuntimeError):
    pass


class Runtime:
    def __init__(self, logdir):
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.package = None
        self.sequence = 0
        secrets = {value for key, value in os.environ.items() if value and
                   any(word in key.upper() for word in
                       ("TOKEN", "PASSWORD", "PASSPHRASE", "PRIVATE_KEY", "SECRET"))}
        # Subprocess streams arrive one line at a time, including private keys.
        secrets.update(line for value in list(secrets) for line in value.splitlines() if len(line) >= 8)
        self.secrets = sorted(secrets, key=len, reverse=True)

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def redact_value(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value

    def log(self, event, **fields):
        record = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                  "event": event, "package": self.package, **fields}
        # Redact before JSON serialization, including multiline private keys.
        record = self.redact_value(record)
        line = f"{record['time']} [{self.package or 'buildbot'}] {event} " + " ".join(
            f"{k}={v}" for k, v in record.items() if k not in {"time", "package", "event"})
        print(line, flush=True)
        with (self.logdir / "buildbot.log").open("a", encoding="utf-8") as log:
            log.write(line + "\n")
        with (self.logdir / "events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.package:
            with (self.logdir / f"{self.package}.log").open("a", encoding="utf-8") as log:
                log.write(line + "\n")

    def exception(self, event, exc):
        self.log(event, error=str(exc), traceback=traceback.format_exc())

    @contextlib.contextmanager
    def context(self, package):
        previous, self.package = self.package, package
        try:
            yield
        finally:
            self.package = previous

    def run(self, args, *, cwd=None, check=True, env=None, timeout=600, capture=True):
        """Stream both pipes; return stdout only so stderr cannot corrupt machine output.

        Build output need not be held in RAM. Machine-readable stdout has a hard
        bound, and errors refer to the complete on-disk log. A new process group
        allows a timeout to terminate makepkg and its compiler children together.
        """
        args = [str(arg) for arg in args]
        self.sequence += 1
        command = self.sequence
        start = time.monotonic()
        self.log("command-start", command=command, argv=shlex.join(args),
                 cwd=str(cwd or Path.cwd()), timeout=timeout)
        environment = dict(os.environ if env is None else env, LC_ALL="C", GIT_TERMINAL_PROMPT="0")
        chunks, size = [], 0
        next_heartbeat = start + 60
        proc = subprocess.Popen(args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        streams = selectors.DefaultSelector()
        decoders, pending = {}, {}
        for pipe, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            streams.register(pipe, selectors.EVENT_READ, label)
            decoders[label] = codecs.getincrementaldecoder("utf-8")("replace")
            pending[label] = ""
        try:
            while streams.get_map() or proc.poll() is None:
                now = time.monotonic()
                if now - start >= timeout:
                    raise CommandError(f"command #{command} timed out after {timeout}s")
                if now >= next_heartbeat:
                    self.log("command-heartbeat", command=command, pid=proc.pid,
                             elapsed_seconds=round(now - start, 1))
                    next_heartbeat = now + 60
                for key, _ in streams.select(min(1, max(0.01, timeout - (now - start)))):
                    raw = os.read(key.fileobj.fileno(), 65536)
                    label = key.data
                    text = decoders[label].decode(raw, final=not raw)
                    if capture and label == "stdout":
                        size += len(raw)
                        if size > 32 * 1024 * 1024:
                            raise CommandError(f"command #{command} stdout exceeded the 32 MiB capture limit")
                        chunks.append(text)
                    pending[label] += text
                    while "\n" in pending[label]:
                        line, pending[label] = pending[label].split("\n", 1)
                        self.log("command-output", command=command, stream=label, text=line)
                    if not raw:
                        if pending[label]:
                            self.log("command-output", command=command, stream=label, text=pending[label])
                        pending[label] = ""
                        streams.unregister(key.fileobj)
                    elif len(pending[label]) > 65536:
                        # Keep newline-free tool output bounded too.
                        self.log("command-output", command=command, stream=label, text=pending[label])
                        pending[label] = ""
            code = proc.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # Children may outlive their parent or ignore SIGTERM.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            self.log("command-aborted", command=command, elapsed_seconds=round(time.monotonic() - start, 3))
            raise
        finally:
            streams.close()
            proc.stdout.close()
            proc.stderr.close()
        self.log("command-end", command=command, exit_code=code,
                 elapsed_seconds=round(time.monotonic() - start, 3))
        if check and code:
            raise CommandError(f"command #{command} exited {code}: {shlex.join(args)}; see {self.logdir}")
        return "".join(chunks).strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)

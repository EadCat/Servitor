"""Collects nvidia-smi output from remote servers, or from this machine.

Knows nothing about the UI; it only pushes parsed samples and status messages
onto a queue, and reports messages as i18n keys rather than sentences.

A transport decides only *how* a command runs: over one SSH connection, or as
a local subprocess. Both run the same nvidia-smi arguments and feed the same
parsers, so local monitoring is the same data by the same route, just without
the network. Nothing reads NVML, X or any other side channel.

Two commands run per host:

* GPU stats use `nvidia-smi -lms`, which keeps a single nvidia-smi alive and
  prints a sample on its own schedule. Measured on a driver 570 machine that
  costs ~2 ms per sample instead of the ~35 ms of re-running nvidia-smi.
* The process list uses `nvidia-smi pmon`, which also streams. It is used in
  place of `--query-compute-apps` because that query reports only compute
  processes and silently omits graphics ones, so GPU memory could appear in
  use with nothing listed. pmon reports both, with per-process memory in MB.
  Its `command` column is truncated, so full command lines, owners and
  container ids are read from /proc on demand and cached per pid.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time

import paramiko

GPU_FIELDS = (
    "index,uuid,name,utilization.gpu,utilization.memory,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit,clocks.current.graphics,"
    "clocks.max.graphics,clocks.current.memory,fan.speed,pstate"
)
# Hardware health, read by the GPU itself: why it is slowing down (a bitmask),
# the PCIe link width against what it should be, and memory errors. Asked for
# separately because an older driver that lacks one rejects the whole query.
HEALTH_FIELDS = (
    "clocks_throttle_reasons.active,pcie.link.width.current,pcie.link.width.max,"
    "ecc.mode.current,ecc.errors.uncorrected.volatile.total,retired_pages.pending,"
    "remapped_rows.pending"
)
# process_name is the only name there is on Windows, which has no /proc.
COMPUTE_FIELDS = "gpu_uuid,pid,used_memory,process_name"
MIN_PROC_INTERVAL = 2
_BACKOFF = [5, 10, 20, 40, 60]
_CONNECT_TIMEOUT = 10


class _FatalError(Exception):
    """An error that retrying cannot resolve. Its message is an i18n key."""


def _fatal(key, args=None):
    error = _FatalError(key)
    error.args_map = args or {}
    return error


def _guarded(command: str) -> str:
    return "command -v nvidia-smi >/dev/null 2>&1 || { echo ERR:NO_NVIDIA_SMI; exit 1; }; " + command


def _gpu_argv(interval: float, fields: str):
    return ["nvidia-smi", f"--query-gpu={fields}",
            "--format=csv,noheader,nounits", "-lms", str(max(100, int(interval * 1000)))]


def _pmon_argv(interval: float):
    # -d is whole seconds; process lists change slowly so the floor is fine.
    delay = max(MIN_PROC_INTERVAL, int(interval + 0.5))
    return ["nvidia-smi", "pmon", "-d", str(delay), "-s", "um", "-o", "T"]


def _compute_argv():
    return ["nvidia-smi", f"--query-compute-apps={COMPUTE_FIELDS}",
            "--format=csv,noheader,nounits"]


# Run only when nvidia-smi produced nothing, to tell a missing GPU apart from a
# broken NVML. /proc/driver/nvidia is the kernel's own enumeration and does not
# go through NVML, and lspci sees the hardware even when no driver is loaded.
_DIAGNOSE = (
    "printf 'KERNEL:%s\\n' \"$(ls /proc/driver/nvidia/gpus 2>/dev/null | wc -l)\"; "
    "printf 'PCI:%s\\n' \"$(lspci 2>/dev/null | grep -ci nvidia)\"; "
    "printf 'SMI:%s\\n' \"$(nvidia-smi -L 2>&1 | head -3 | tr '\\n' ' ')\""
)


_DETAILS_BODY = r'''for p in %s; do
c=$(tr '\0\t' '  ' < /proc/$p/cmdline 2>/dev/null)
[ -n "$c" ] || c=$(cat /proc/$p/comm 2>/dev/null)
o=$(stat -c %%U /proc/$p 2>/dev/null)
k=$(sed -nE 's#.*(docker|containerd|kubepods|lxc)[-/]?([0-9a-f]{12})[0-9a-f]*.*#\2#p' /proc/$p/cgroup 2>/dev/null | head -1)
e=$(ps -o etimes= -p $p 2>/dev/null | tr -d ' ')
printf '%%s\t%%s\t%%s\t%%s\t%%s\n' "$p" "${o:-}" "${k:-}" "${e:-}" "$c"
done'''


# The host's CPU and memory, and CPU time and resident memory of the given
# pids, read from /proc in one go. CPU figures are running totals; the rates
# are worked out here from two readings. The first reading also takes the core
# count and the clock tick the totals are counted in.
_HOST_BODY = r'''%s
head -1 /proc/stat
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
cat /proc/loadavg
for p in %s; do
s=$(sed 's/.*) //' /proc/$p/stat 2>/dev/null | cut -d' ' -f12,13)
r=$(sed -n 's/^VmRSS:[^0-9]*\([0-9]*\).*/\1/p' /proc/$p/status 2>/dev/null)
echo "P $p $s $r"
done'''


def _host_command(pids, first) -> str:
    # Only digits reach the shell, so the pid list cannot inject anything.
    head = 'echo "N $(nproc) $(getconf CLK_TCK)"' if first else ""
    return _HOST_BODY % (head, " ".join(pid for pid in pids if pid.isdigit()))


def _details_command(pids) -> str:
    """Read owner, container id and full command line for the given pids."""
    # Only digits reach the shell, so the pid list cannot inject anything.
    return _DETAILS_BODY % " ".join(pid for pid in pids if pid.isdigit())


def _num(text: str):
    try:
        return float(text)
    except ValueError:
        return None


def _flag(text: str):
    if text in ("Yes", "No"):
        return text == "Yes"
    return None


def _parse_gpus(lines):
    gpus = []
    for line in lines:
        p = [x.strip() for x in line.split(",")]
        if len(p) < 15:
            continue
        # Without the health fields every one of these stays None.
        h = p[15:22] if len(p) >= 22 else ["[N/A]"] * 7
        try:
            throttle = int(h[0], 16)
        except ValueError:
            throttle = None
        gpus.append(
            {
                "index": p[0],
                "uuid": p[1],
                "name": p[2],
                "util": _num(p[3]),
                "mem_util": _num(p[4]),
                "mem_used": _num(p[5]),
                "mem_total": _num(p[6]),
                "temp": _num(p[7]),
                "power": _num(p[8]),
                "power_limit": _num(p[9]),
                "clock_core": _num(p[10]),
                "clock_core_max": _num(p[11]),
                "clock_mem": _num(p[12]),
                "fan": _num(p[13]),
                "pstate": p[14] if p[14] not in ("", "[N/A]") else None,
                "throttle": throttle,
                "pcie_width": _num(h[1]),
                "pcie_width_max": _num(h[2]),
                # Counts only mean something while ECC is on; off, they are 0 or N/A.
                "ecc": h[3] == "Enabled",
                "ecc_errors": _num(h[4]),
                "mem_repair": _flag(h[5]) or _flag(h[6]),
            }
        )
    return gpus


def _parse_pmon(line: str, columns):
    """Parse one pmon row by the column names its header gave.

    Which columns appear depends on the driver (jpg, ofa, ccpm come and go), so
    positions are read from the header rather than assumed. The command is
    last and may hold spaces, so the row is split into exactly that many parts.
    """
    parts = line.split(None, len(columns) - 1)
    if len(parts) < len(columns) - 1:
        return None
    row = dict(zip(columns, parts))
    pid = row.get("pid", "")
    if not pid.isdigit():
        return row.get("time"), None
    sm = row.get("sm", "-")
    return row.get("time"), {
        "gpu": row.get("gpu", "?"),
        "pid": pid,
        "type": row.get("type", ""),
        "mem": _num(row.get("fb", "")),
        "sm": _num(sm) if sm != "-" else None,
        "name": row.get("command", "").strip(),
        "user": "",
        "container": "",
        "started": None,
        "cpu": None,
        "rss": None,
    }


def _parse_compute(text, uuid_to_index):
    """Fallback rows from --query-compute-apps, which sees compute apps only."""
    rows = []
    for line in text.splitlines():
        # The name comes last and is a path, which may itself hold commas.
        parts = [x.strip() for x in line.split(",", 3)]
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        rows.append({
            "gpu": uuid_to_index.get(parts[0], "?"),
            "pid": parts[1],
            "type": "C",
            "mem": _num(parts[2]),
            "sm": None,
            "name": parts[3] if len(parts) > 3 and parts[3] != "[N/A]" else "",
            "user": "",
            "container": "",
            "started": None,
            "cpu": None,
            "rss": None,
        })
    return rows


_NVML_SIGNS = (
    ("driver/library version mismatch", "err_nvml_mismatch"),
    ("driver not loaded", "err_nvml_driver"),
    ("couldn't communicate with the nvidia driver", "err_nvml_driver"),
    ("insufficient permissions", "err_nvml_permission"),
    ("unknown error", "err_nvml_unknown"),
)


def _classify_output(text: str):
    """Map an nvidia-smi failure message to an i18n key, or None if unknown."""
    low = text.lower()
    for needle, key in _NVML_SIGNS:
        if needle in low:
            return key, {}
    if "no devices were found" in low or "no running processes" in low:
        return "err_no_gpu", {}
    return None


def _read_diagnosis(text: str):
    """Turn the diagnosis output into a specific reason for having no GPU."""
    facts = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        facts[key.strip()] = value.strip()
    detected = _classify_output(facts.get("SMI", ""))
    if detected and detected[0] != "err_no_gpu":
        return detected
    kernel = facts.get("KERNEL", "0")
    pci = facts.get("PCI", "0")
    if kernel.isdigit() and int(kernel) > 0:
        # The kernel driver enumerated a GPU, so NVML is the part that failed.
        return "err_gpu_hidden", {}
    if pci.isdigit() and int(pci) > 0:
        return "err_gpu_no_driver", {}
    return "err_no_gpu", {}


def _describe(exc: Exception):
    """Return an i18n key and its format arguments, never a finished sentence."""
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return "err_unreachable", {}
    if isinstance(exc, socket.gaierror):
        return "err_dns", {}
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "err_timeout", {}
    return "err_raw", {"text": str(exc) or type(exc).__name__}


def _load_key(key_path, password):
    """The private key at key_path, or a _FatalError saying what is wrong with it.

    Loaded here rather than left to paramiko, which reports a missing file, a
    public key or a key still locked by its passphrase all as a failed login.
    """
    if not os.path.isfile(key_path):
        raise _fatal("err_key_missing", {"path": key_path})
    with open(key_path, "rb") as fp:
        head = fp.read(64)
    # The .pub half of the pair is one line starting with its algorithm.
    if key_path.endswith(".pub") or head.startswith((b"ssh-", b"ecdsa-", b"sk-")):
        raise _fatal("err_key_public")
    # Tried without a passphrase first: the password box may hold the
    # server's login password, which an unlocked key would refuse.
    try:
        return paramiko.PKey.from_path(key_path)
    except (paramiko.PasswordRequiredException, TypeError):
        pass                    # locked; cryptography says so with a TypeError
    except Exception as exc:
        raise _fatal("err_key_invalid", {"text": str(exc)})
    if not password:
        raise _fatal("err_need_passphrase")
    try:
        return paramiko.PKey.from_path(key_path, password=password.encode())
    except Exception:
        raise _fatal("err_bad_passphrase")


# Where the keys of the servers seen so far are kept; set by the app.
host_keys_path = None
# Servers connect on threads of their own, and saving rewrites the whole file.
_host_keys_lock = threading.Lock()


class _SaveHostKey(paramiko.AutoAddPolicy):
    def missing_host_key(self, client, hostname, key):
        with _host_keys_lock:
            super().missing_host_key(client, hostname, key)


def forget_host_key(server: dict):
    """Drop a server's saved key, so the one it shows next is taken as new."""
    port = int(server.get("port") or 22)
    with _host_keys_lock:
        keys = paramiko.HostKeys(host_keys_path)
        # Named as paramiko names them when it saves.
        keys.pop(server["host"] if port == 22 else "[%s]:%d" % (server["host"], port), None)
        keys.save(host_keys_path)


def _connect(server: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # A server's key is saved the first time it is seen and has to match from
    # then on, as ssh does. Accepted unseen every time, whoever answered at the
    # address would be handed the password.
    os.makedirs(os.path.dirname(host_keys_path), exist_ok=True)
    with _host_keys_lock:
        open(host_keys_path, "a").close()       # paramiko needs it to exist
        client.load_host_keys(host_keys_path)
    client.set_missing_host_key_policy(_SaveHostKey())
    # paramiko does not expand "~" in key paths, but users type it.
    key_path = os.path.expanduser((server.get("key_path") or "").strip())
    password = server.get("password") or None
    # The password box doubles as the key's passphrase, as in paramiko.
    pkey = _load_key(key_path, password) if key_path else None
    try:
        client.connect(
            hostname=server["host"],
            port=int(server.get("port") or 22),
            username=server["username"],
            password=password,
            pkey=pkey,
            timeout=_CONNECT_TIMEOUT,
            banner_timeout=15,
            auth_timeout=15,
            look_for_keys=not password,
            allow_agent=True,
        )
    except paramiko.BadHostKeyException as exc:
        raise _fatal("err_host_key", {"fingerprint": exc.key.fingerprint})
    return client


class _SshTransport:
    """Runs the commands on a remote host over one SSH connection."""

    def __init__(self, server):
        self.client = _connect(server)
        transport = self.client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)

    def _exec(self, script):
        # The account's login shell sees only "sh"; the script reaches sh on
        # its stdin. Sent as the command itself, it was parsed by that login
        # shell first, and tcsh, csh or fish do not read sh syntax.
        stdin, out, err = self.client.exec_command("sh")
        stdin.write(script + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        return out, err

    def stream(self, argv):
        return self._exec(_guarded("exec " + " ".join(argv)))

    def run(self, argv):
        out, _ = self._exec(" ".join(argv))
        return out.read().decode(errors="replace")

    def run_shell(self, script):
        out, _ = self._exec(script)
        return out.read().decode(errors="replace")

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


# A GUI process on Windows has no console for a child to inherit, so Windows
# opens a new console window for every console child. nvidia-smi is spawned
# over and over, which makes a black window blink for as long as the local
# machine is monitored. EVERY spawn below must pass **_NO_WINDOW; the flag does
# not exist on POSIX, where the empty dict makes the splat a no-op.
_NO_WINDOW = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    if os.name == "nt" else {}
)


class _LocalTransport:
    """Runs the same commands on this machine, without a shell where possible."""

    def __init__(self):
        self.processes = []

    def stream(self, argv):
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, **_NO_WINDOW,
            )
        except FileNotFoundError:
            raise _FatalError("err_no_smi")
        self.processes.append(process)
        return process.stdout, process.stderr

    def run(self, argv):
        try:
            done = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=15, **_NO_WINDOW)
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        return done.stdout

    def run_shell(self, script):
        # /proc reads and the diagnosis need a POSIX shell, which Windows lacks.
        try:
            done = subprocess.run(["sh", "-c", script], capture_output=True,
                                  text=True, timeout=15, **_NO_WINDOW)
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""
        return done.stdout

    def close(self):
        for process in self.processes:
            try:
                process.terminate()
            except Exception:
                pass
        self.processes = []


def _make_transport(server):
    return _LocalTransport() if server.get("local") else _SshTransport(server)


def test_connection(server: dict):
    """Backs the dialog's test button. Returns (ok, i18n key, format args)."""
    transport = None
    try:
        transport = _make_transport(server)
        listed = transport.run(["nvidia-smi", "-L"])
        count = sum(1 for line in listed.splitlines() if line.startswith("GPU "))
        if count == 0:
            return (False,) + _read_diagnosis(transport.run_shell(_DIAGNOSE))
        return True, "ok_connected", {"n": count}
    except paramiko.AuthenticationException:
        return False, "err_auth", {}
    except _FatalError as exc:
        return False, str(exc), getattr(exc, "args_map", {})
    except Exception as exc:  # any failure is reported to the user as text
        return (False,) + _describe(exc)
    finally:
        if transport is not None:
            transport.close()


class ServerCollector:
    """Keeps one host's GPU and process streams running, over SSH or locally."""

    def __init__(self, server: dict, out_queue, interval: float = 2.0, host_stats=True):
        self.server = server
        self.server_id = server["id"]
        self.queue = out_queue
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._transport = None
        self._lock = threading.Lock()
        self._details = {}      # pid -> owner, container id and full command line
        self._uuid_index = {}   # gpu uuid -> index, for the fallback process query
        self._saw_gpu = False
        # Read at every process sample, so switching it takes effect at once.
        self.host_stats = host_stats
        self._host = None       # cores, clock tick, and the last CPU totals

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            transport = self._transport
        if transport is not None:
            transport.close()

    def _emit(self, message: dict):
        message["server_id"] = self.server_id
        self.queue.put(message)

    def _status(self, status: str, key: str = "", args: dict = None, attempt: int = 0):
        self._emit({
            "type": "status",
            "status": status,
            "detail_key": key,
            "detail_args": args or {},
            "attempt": attempt,
        })

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            self._status("connecting", attempt=attempt)
            try:
                self._session()
                reason = ("err_disconnected", {})
            except _FatalError as exc:
                self._status("error", str(exc), getattr(exc, "args_map", {}))
                return
            except paramiko.AuthenticationException:
                self._status("auth_failed", "err_auth")
                return
            except Exception as exc:  # every other failure is retryable
                reason = _describe(exc)
            if self._stop.is_set():
                return
            # A session that delivered samples was a recovery, so the count of
            # failures in a row starts over rather than carrying the last outage.
            if self._saw_gpu:
                attempt = 0
            attempt += 1
            self._status("retrying", reason[0], reason[1], attempt)
            self._stop.wait(_BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)])

    def _session(self):
        transport = _make_transport(self.server)
        # Ends the process thread as soon as this session does, so a fatal GPU
        # error is reported at once instead of waiting out a poll interval.
        halt = threading.Event()
        proc_thread = None
        try:
            with self._lock:
                self._transport = transport
            proc_thread = threading.Thread(
                target=self._run_procs, args=(transport, halt), daemon=True
            )
            proc_thread.start()

            noise = ""
            for fields in (GPU_FIELDS + "," + HEALTH_FIELDS, GPU_FIELDS):
                gpu_out, gpu_err = transport.stream(_gpu_argv(self.interval, fields))
                self._saw_gpu = self._rejected = False
                self._read_gpus(gpu_out)
                if self._saw_gpu or self._stop.is_set() or not self._rejected:
                    break
            if not self._saw_gpu and not self._stop.is_set():
                # nvidia-smi exited without a sample; say why instead of retrying.
                try:
                    noise = gpu_err.read()
                    # Text from a local process, bytes over SSH.
                    if isinstance(noise, bytes):
                        noise = noise.decode(errors="replace")
                except Exception:
                    pass
                found = _classify_output(noise)
                if found and found[0] != "err_no_gpu":
                    raise _fatal(*found)
                raise _fatal(*_read_diagnosis(transport.run_shell(_DIAGNOSE)))
        finally:
            halt.set()
            with self._lock:
                self._transport = None
            transport.close()
            if proc_thread is not None:
                proc_thread.join(timeout=3)

    def _read_gpus(self, stdout):
        # -lms emits one line per GPU with no separator, so a sample ends when
        # the index wraps back to 0.
        batch = []
        for raw in stdout:
            if self._stop.is_set():
                return
            line = raw.strip()
            if not line:
                continue
            if line == "ERR:NO_NVIDIA_SMI":
                raise _FatalError("err_no_smi")
            # Printed on stdout, not stderr, by a driver that lacks a field.
            if line.endswith("is not a valid field to query."):
                self._rejected = True
                continue
            if line.split(",", 1)[0].strip() == "0" and batch:
                self._flush_gpus(batch)
                batch = []
            batch.append(line)
        if batch and not self._stop.is_set():
            self._flush_gpus(batch)

    def _flush_gpus(self, batch):
        gpus = _parse_gpus(batch)
        if not gpus:
            return
        self._saw_gpu = True
        self._uuid_index = {gpu["uuid"]: gpu["index"] for gpu in gpus}
        self._emit({"type": "gpus", "gpus": gpus})

    # --- processes ---

    def _run_procs(self, transport, halt):
        try:
            # Read once per connection: they only change with a driver update,
            # which takes a reboot and so a reconnect.
            found = re.search(r"Driver Version:\s*(\S+).*?CUDA Version:\s*(\S+)",
                              transport.run(["nvidia-smi"]), re.S)
            if found:
                self._emit({"type": "versions", "driver": found[1], "cuda": found[2]})
            if self._read_pmon(transport):
                return
            if self._stop.is_set() or halt.is_set():
                return
            # pmon needs bare-metal Linux and a supported card; fall back to the
            # compute-only query rather than showing an empty table.
            self._poll_compute(transport, halt)
        except Exception:
            pass  # the GPU stream drives reconnection

    def _read_pmon(self, transport):
        """Returns True if pmon produced at least one sample."""
        stdout, _ = transport.stream(_pmon_argv(self.interval))
        batch, stamp, seen, columns = [], None, False, None
        for raw in stdout:
            if self._stop.is_set():
                return True
            line = raw.rstrip()
            if line.startswith("#"):
                # Two header lines, names then units; the names carry "pid".
                names = line.lstrip("#").lower().split()
                if "pid" in names:
                    columns = names
                continue
            if not line or line == "ERR:NO_NVIDIA_SMI" or columns is None:
                continue
            parsed = _parse_pmon(line, columns)
            if parsed is None:
                continue
            row_stamp, row = parsed
            if stamp is not None and row_stamp != stamp:
                seen = True
                self._flush_procs(transport, batch)
                batch = []
            stamp = row_stamp
            if row is not None:
                batch.append(row)
        return seen

    def _poll_compute(self, transport, halt):
        delay = max(MIN_PROC_INTERVAL, self.interval)
        while not (self._stop.is_set() or halt.is_set()):
            rows = _parse_compute(transport.run(_compute_argv()), self._uuid_index)
            self._flush_procs(transport, rows, notice="proc_fallback")
            halt.wait(delay)

    def _flush_procs(self, transport, rows, notice=""):
        """Attach cached /proc details, fetching them for pids not seen yet."""
        unknown = [row["pid"] for row in rows if row["pid"] not in self._details]
        if unknown:
            self._details.update(self._fetch_details(transport, unknown))
        for row in rows:
            detail = self._details.get(row["pid"], {})
            row["user"] = detail.get("user", "")
            row["container"] = detail.get("container", "")
            row["started"] = detail.get("started")
            # pmon truncates its command column; /proc has the whole line.
            row["name"] = detail.get("cmd") or row["name"]
        if self.host_stats:
            self._read_host(transport, rows)
        live = {row["pid"] for row in rows}
        self._details = {pid: d for pid, d in self._details.items() if pid in live}
        self._emit({"type": "procs", "procs": rows, "notice": notice})

    def _read_host(self, transport, rows):
        """Emit the host's CPU and memory, and add cpu and rss to the rows.

        Nothing is emitted where there is no /proc to read, which on Windows
        means the local machine: its rows simply keep cpu and rss as None.
        """
        try:
            text = transport.run_shell(_host_command([r["pid"] for r in rows],
                                                     self._host is None))
        except Exception:
            return
        now = time.monotonic()
        mem, procs, total, load = {}, {}, None, None
        host = self._host or {}
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "N" and len(parts) == 3:
                host["cores"], host["tick"] = int(parts[1]), int(parts[2])
            elif parts[0] == "cpu":
                ticks = [int(x) for x in parts[1:9]]
                total, idle = sum(ticks), ticks[3] + ticks[4]
            elif parts[0].endswith(":") and len(parts) >= 2:
                mem[parts[0][:-1]] = int(parts[1]) / 2**20          # kB to GB
            elif parts[0] == "P" and len(parts) == 5:
                procs[parts[1]] = (int(parts[2]) + int(parts[3]), int(parts[4]) / 2**20)
            elif len(parts) == 5 and "/" in parts[3]:          # /proc/loadavg
                load = float(parts[0])
        if total is None or "cores" not in host:
            return
        before = host.get("cpu")
        cpu = (100 * (1 - (idle - before[1]) / (total - before[0]))
               if before and total > before[0] else None)
        last = host.get("procs", {})
        span = now - host.get("at", now)
        for row in rows:
            ticks, rss = procs.get(row["pid"], (None, None))
            row["rss"] = rss
            # 100% is one core, as top and nvitop count it.
            row["cpu"] = (100 * (ticks - last[row["pid"]]) / host["tick"] / span
                          if ticks is not None and row["pid"] in last and span > 0 else None)
        host.update(cpu=(total, idle), procs={p: v[0] for p, v in procs.items()}, at=now)
        self._host = host
        self._emit({
            "type": "host", "cpu": cpu, "cores": host["cores"], "load": load,
            "mem_total": mem.get("MemTotal"),
            "mem_used": mem["MemTotal"] - mem["MemAvailable"] if "MemAvailable" in mem else None,
            "swap_total": mem.get("SwapTotal"),
            "swap_used": mem["SwapTotal"] - mem["SwapFree"] if "SwapFree" in mem else None,
        })

    def _fetch_details(self, transport, pids):
        found = {}
        try:
            for raw in transport.run_shell(_details_command(pids)).splitlines():
                parts = raw.split("\t", 4)
                if len(parts) < 5:
                    continue
                found[parts[0]] = {
                    "user": parts[1],
                    "container": parts[2],
                    # Kept as a clock time, since the age keeps growing after this.
                    "started": time.time() - int(parts[3]) if parts[3].isdigit() else None,
                    "cmd": parts[4].strip(),
                }
        except Exception:
            pass  # names simply stay as pmon reported them
        # Remember pids we could not read so they are not queried every sample.
        for pid in pids:
            found.setdefault(pid, {})
        return found

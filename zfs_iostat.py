#!/usr/bin/env python3
"""zfs-iostat: report ZFS I/O activity at dataset granularity.

The tool reads ZFS dataset kstats from /proc/spl/kstat/zfs on Linux and from
the kstat.zfs sysctl tree on FreeBSD, computes rates the same way zpool iostat
does (lifetime average for the first report, per interval deltas after that),
and renders the result. No third party dependencies are used; curses comes
from the standard library.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field


# Platform detection. The collector, the mount table reader, and the open
# files scanner are the only platform specific parts of the tool; every layer
# above the collector consumes the same platform neutral data structures.
IS_FREEBSD = sys.platform.startswith("freebsd")


# Default location of the ZFS dataset kstats on Linux. Overridable with the
# hidden --kstat-base flag so recorded fixture directories can be replayed and
# the Python and C++ implementations can be cross tested.
KSTAT_BASE = "/proc/spl/kstat/zfs"

# Root of the ZFS dataset kstat sysctl tree on FreeBSD. The same counters that
# Linux exposes as files under KSTAT_BASE surface here as sysctl nodes named
# kstat.zfs.<pool>.dataset.objset-0x<objsetid>.<counter>.
FREEBSD_KSTAT_SYSCTL = "kstat.zfs"

# Command used to dump the FreeBSD dataset kstat sysctl tree. sysctl prints one
# "node: value" line per leaf, which the FreeBSD collector parses. Overridable
# with the hidden --sysctl-cmd flag so a recorded fixture can be replayed (for
# example "cat tests/fixtures/sysctl.txt") and cross tested against Linux.
FREEBSD_SYSCTL_CMD = "sysctl " + FREEBSD_KSTAT_SYSCTL

# Default kernel mount table used by the files view on Linux.
MOUNTS_PATH = "/proc/self/mounts"


VERSION = "0.1.0 (python mvp)"


# ---------------------------------------------------------------------------
# 1. Data structures
# ---------------------------------------------------------------------------

@dataclass
class DatasetSample:
    """One parsed objset kstat file at one point in time."""
    objset_id: str          # "0x36", stable key across samples
    pool_name: str
    dataset_name: str
    crtime_ns: int
    snaptime_ns: int
    counters: dict = field(default_factory=dict)  # name -> int


@dataclass
class DatasetRates:
    """Computed rates between two samples, ready for rendering."""
    dataset_name: str
    read_ops_per_sec: float
    write_ops_per_sec: float
    read_bytes_per_sec: float
    write_bytes_per_sec: float


@dataclass
class OpenFileEntry:
    """One open file on a ZFS dataset (files view)."""
    dataset_name: str
    file_path: str
    pid: int
    process_name: str
    open_mode: str          # "r", "w", "rw", or "?"
    offset: int             # from fdinfo pos, for activity detection
    is_active: bool = False  # offset changed since previous scan


@dataclass
class Options:
    """Parsed command line options, passed to the run_* functions."""
    dataset: object
    interval: object
    count: object
    skip_first: bool
    parsable: bool
    scripted: bool
    json_output: bool
    sort_column: str
    top: bool
    files: bool
    kstat_base: str
    sysctl_cmd: str
    mounts_path: str



# ---------------------------------------------------------------------------
# 2. Collector (Linux procfs and FreeBSD sysctl)
# ---------------------------------------------------------------------------

# --- Linux collector (procfs kstats) ---------------------------------------

def list_objset_paths(kstat_base):
    """Return all objset kstat file paths across all pools."""
    return glob.glob(os.path.join(kstat_base, "*", "objset-0x*"))


def parse_objset_file(path):
    """Parse one objset kstat file into a DatasetSample.

    Returns None if the file vanished mid read (dataset unmounted during
    sampling) or if the content is too short or malformed to use.
    """
    try:
        with open(path, "r") as kstat_file:
            lines = kstat_file.read().splitlines()
    except (FileNotFoundError, PermissionError):
        return None
    if len(lines) < 3:
        return None
    header_fields = lines[0].split()
    if len(header_fields) < 7:
        return None
    try:
        crtime_ns = int(header_fields[5])
        snaptime_ns = int(header_fields[6])
    except ValueError:
        return None
    counters = {}
    dataset_name = None
    # Line 1 (index 0) is the kstat header, line 2 (index 1) is the column
    # header "name type data". Data lines begin at index 2.
    for line in lines[2:]:
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        name, kstat_type, value = parts
        if name == "dataset_name":
            dataset_name = value
        elif kstat_type == "4":
            try:
                counters[name] = int(value)
            except ValueError:
                continue
    if dataset_name is None:
        return None
    pool_name = os.path.basename(os.path.dirname(path))
    objset_id = os.path.basename(path).removeprefix("objset-")
    return DatasetSample(objset_id, pool_name, dataset_name,
                         crtime_ns, snaptime_ns, counters)


def collect_samples_linux(kstat_base):
    """Read every objset kstat once on Linux (procfs).

    Returns a dict keyed by (pool_name, objset_id) so ids from different pools
    never collide and renames do not corrupt the per dataset deltas.
    """
    samples = {}
    for path in list_objset_paths(kstat_base):
        sample = parse_objset_file(path)
        if sample is not None:
            samples[(sample.pool_name, sample.objset_id)] = sample
    return samples


# --- FreeBSD collector (sysctl kstats) -------------------------------------

# A sysctl node line looks like:
#   kstat.zfs.tank.dataset.objset-0x36.dataset_name: tank/data
#   kstat.zfs.tank.dataset.objset-0x36.writes: 1234
# The middle segment ".dataset." separates the pool name from the objset node
# and counter name. Pool names cannot contain that literal segment, so it is a
# safe split point. Counter names never contain a dot, so the objset node and
# the counter split on the first dot after the marker.
SYSCTL_DATASET_MARKER = ".dataset."


def read_sysctl_output(sysctl_cmd):
    """Run the sysctl command for the ZFS kstat tree and return its text.

    Returns an empty string if sysctl is missing or the tree does not exist
    (for example the zfs module is not loaded), which the caller treats the
    same as no datasets.
    """
    try:
        completed = subprocess.run(sysctl_cmd, shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   universal_newlines=True)
    except OSError:
        return ""
    return completed.stdout


def parse_sysctl_output(text):
    """Parse sysctl kstat.zfs output into DatasetSample objects.

    The counters, dataset_name, pool_name, and objset_id match the Linux
    collector one for one so every layer above the collector is unchanged.
    crtime and snaptime are not exposed through the dataset sysctls, so both
    are recorded as 0; the first report on FreeBSD therefore uses an interval
    sample rather than a lifetime average (see run_iostat).
    """
    # Group the flat sysctl lines by (pool_name, objset_id) first, then build
    # one DatasetSample per group.
    grouped = {}
    for line in text.splitlines():
        separator = line.find(": ")
        if separator < 0:
            continue
        full_name = line[:separator]
        value = line[separator + 2:]
        marker = full_name.find(SYSCTL_DATASET_MARKER)
        if marker < 0:
            continue
        prefix = full_name[:marker]                 # kstat.zfs.<pool>
        remainder = full_name[marker + len(SYSCTL_DATASET_MARKER):]
        if not prefix.startswith("kstat.zfs."):
            continue
        pool_name = prefix[len("kstat.zfs."):]
        if not remainder.startswith("objset-"):
            continue
        dot = remainder.find(".")
        if dot < 0:
            continue
        objset_id = remainder[len("objset-"):dot]   # e.g. "0x36"
        counter_name = remainder[dot + 1:]
        entry_key = (pool_name, objset_id)
        node = grouped.setdefault(entry_key, {"dataset_name": None,
                                              "counters": {}})
        if counter_name == "dataset_name":
            node["dataset_name"] = value
        else:
            try:
                node["counters"][counter_name] = int(value)
            except ValueError:
                continue

    samples = {}
    for (pool_name, objset_id), node in grouped.items():
        dataset_name = node["dataset_name"]
        if dataset_name is None:
            continue
        samples[(pool_name, objset_id)] = DatasetSample(
            objset_id=objset_id,
            pool_name=pool_name,
            dataset_name=dataset_name,
            crtime_ns=0,
            snaptime_ns=0,
            counters=node["counters"])
    return samples


def collect_samples_freebsd(sysctl_cmd):
    """Read every objset kstat once on FreeBSD (sysctl).

    Returns a dict keyed by (pool_name, objset_id), matching the Linux
    collector so the aggregator and renderer are platform neutral.
    """
    return parse_sysctl_output(read_sysctl_output(sysctl_cmd))


# --- Collector dispatcher --------------------------------------------------

def collect_samples(kstat_source):
    """Read every objset kstat once on the current platform.

    kstat_source is the Linux kstat base directory or, on FreeBSD, the sysctl
    command string. The dispatch keeps the rest of the tool platform neutral.
    """
    if IS_FREEBSD:
        return collect_samples_freebsd(kstat_source)
    return collect_samples_linux(kstat_source)


# ---------------------------------------------------------------------------
# 3. Aggregator (pure rate math, no I/O)
# ---------------------------------------------------------------------------

def compute_lifetime_rates(sample):
    """Rates averaged since the kstat was created (used for first report)."""
    elapsed = (sample.snaptime_ns - sample.crtime_ns) / 1e9
    if elapsed <= 0:
        elapsed = 1.0
    counters = sample.counters
    return DatasetRates(
        dataset_name=sample.dataset_name,
        read_ops_per_sec=counters.get("reads", 0) / elapsed,
        write_ops_per_sec=counters.get("writes", 0) / elapsed,
        read_bytes_per_sec=counters.get("nread", 0) / elapsed,
        write_bytes_per_sec=counters.get("nwritten", 0) / elapsed,
    )


def compute_interval_rates(old_sample, new_sample, elapsed_seconds):
    """Rates over one interval. Caller guarantees matching keys."""
    if elapsed_seconds <= 0:
        elapsed_seconds = 1.0
    old_counters = old_sample.counters
    new_counters = new_sample.counters

    def rate(name):
        delta = new_counters.get(name, 0) - old_counters.get(name, 0)
        if delta < 0:
            delta = 0   # counter reset (remount reuses the objset id)
        return delta / elapsed_seconds

    return DatasetRates(
        dataset_name=new_sample.dataset_name,
        read_ops_per_sec=rate("reads"),
        write_ops_per_sec=rate("writes"),
        read_bytes_per_sec=rate("nread"),
        write_bytes_per_sec=rate("nwritten"),
    )


def dataset_matches(dataset_name, dataset_argument):
    """Return True if a dataset name matches the CLI dataset selection."""
    if dataset_argument is None:
        return True
    if dataset_argument.endswith("/*"):
        base = dataset_argument[:-2]
        return dataset_name == base or dataset_name.startswith(base + "/")
    return dataset_name == dataset_argument


def filter_datasets(rates_list, dataset_argument):
    """Apply the CLI dataset selection to a list of DatasetRates."""
    return [r for r in rates_list
            if dataset_matches(r.dataset_name, dataset_argument)]


# ---------------------------------------------------------------------------
# 4. Renderer (iostat table and top view)
# ---------------------------------------------------------------------------

UNIT_SUFFIXES = ["", "K", "M", "G", "T", "P"]  # P is insanely optimistic but why not add it anyway
NAME_COLUMN_MIN = 24
NUMBER_COLUMN_WIDTH = 9


def humanize(value):
    """Format a number the way zpool iostat does (base 1024)."""
    if value < 1024:
        return f"{value:.0f}"
    for suffix in UNIT_SUFFIXES[1:]:
        value /= 1024.0
        if value < 1024:
            if value < 10:
                return f"{value:.2f}{suffix}"
            if value < 100:
                return f"{value:.1f}{suffix}"
            return f"{value:.0f}{suffix}"
    return f"{value:.0f}E"


def humanize_short(value):
    """Format a number with a base 1024 suffix and at most one decimal place.

    This is the value format used in JSON output, for example "12.5M" or
    "1.1G". Values below 1024 are printed as a whole number with no suffix.
    """
    if value < 1024:
        return f"{value:.0f}"
    for suffix in UNIT_SUFFIXES[1:]:
        value /= 1024.0
        if value < 1024:
            return f"{value:.1f}{suffix}"
    return f"{value:.1f}E"


def json_escape_string(text):
    """Return text as a quoted, escaped JSON string.

    Written by hand rather than using the json module so the Python and C++
    implementations produce byte for byte identical output.
    """
    result = ['"']
    for character in text:
        if character == '"':
            result.append('\\"')
        elif character == '\\':
            result.append('\\\\')
        elif character == '\n':
            result.append('\\n')
        elif character == '\r':
            result.append('\\r')
        elif character == '\t':
            result.append('\\t')
        elif character == '\b':
            result.append('\\b')
        elif character == '\f':
            result.append('\\f')
        elif ord(character) < 0x20:
            result.append('\\u%04x' % ord(character))
        else:
            result.append(character)
    result.append('"')
    return "".join(result)


def render_json(rates_list):
    """Render the iostat report as a single line JSON array of objects.

    Each object has the dataset name and the four rates as humanized strings
    (at most one decimal place). Rows are sorted by dataset name to match the
    table renderer.
    """
    sorted_rates = sorted(rates_list, key=lambda r: r.dataset_name)
    objects = []
    for rates in sorted_rates:
        objects.append(
            '{"dataset":' + json_escape_string(rates.dataset_name)
            + ',"read_ops":'
            + json_escape_string(humanize_short(rates.read_ops_per_sec))
            + ',"write_ops":'
            + json_escape_string(humanize_short(rates.write_ops_per_sec))
            + ',"read_bandwidth":'
            + json_escape_string(humanize_short(rates.read_bytes_per_sec))
            + ',"write_bandwidth":'
            + json_escape_string(humanize_short(rates.write_bytes_per_sec))
            + '}')
    return "[" + ",".join(objects) + "]"



def format_row_fields(rates, parsable):
    """Turn one DatasetRates into a list of five text fields."""
    if parsable:
        return [rates.dataset_name,
                f"{rates.read_ops_per_sec:.0f}",
                f"{rates.write_ops_per_sec:.0f}",
                f"{rates.read_bytes_per_sec:.0f}",
                f"{rates.write_bytes_per_sec:.0f}"]
    return [rates.dataset_name,
            humanize(rates.read_ops_per_sec),
            humanize(rates.write_ops_per_sec),
            humanize(rates.read_bytes_per_sec),
            humanize(rates.write_bytes_per_sec)]


def join_row(fields, name_width):
    """Join one row: name left aligned, four numbers right aligned."""
    parts = [fields[0].ljust(name_width)]
    for value in fields[1:]:
        parts.append(value.rjust(NUMBER_COLUMN_WIDTH))
    return " ".join(parts)


def render_table(rates_list, parsable=False, scripted=False):
    """Render the iostat table as a single string (no trailing newline)."""
    sorted_rates = sorted(rates_list, key=lambda r: r.dataset_name)
    rows = [format_row_fields(r, parsable) for r in sorted_rates]

    if scripted:
        return "\n".join("\t".join(fields) for fields in rows)

    name_width = max(NAME_COLUMN_MIN,
                     max((len(fields[0]) for fields in rows), default=0))

    # A numeric group spans two number columns plus the space between them.
    group_width = NUMBER_COLUMN_WIDTH * 2 + 1
    group_line = (" " * name_width + " "
                  + "operations".center(group_width) + " "
                  + "bandwidth".center(group_width))
    column_line = join_row(["dataset", "read", "write", "read", "write"],
                           name_width)
    separator_line = " ".join(
        ["-" * name_width] + ["-" * NUMBER_COLUMN_WIDTH] * 4)

    lines = [group_line, column_line, separator_line]
    for fields in rows:
        lines.append(join_row(fields, name_width))
    return "\n".join(lines)


def sort_key_value(rates, sort_column):
    """Return the value used to rank a dataset in the top view."""
    if sort_column == "read":
        return rates.read_bytes_per_sec
    if sort_column == "write":
        return rates.write_bytes_per_sec
    if sort_column == "rops":
        return rates.read_ops_per_sec
    if sort_column == "wops":
        return rates.write_ops_per_sec
    return rates.read_bytes_per_sec + rates.write_bytes_per_sec  # total


def sort_rates(rates_list, sort_column):
    """Sort DatasetRates descending by the selected column."""
    return sorted(rates_list,
                  key=lambda r: sort_key_value(r, sort_column),
                  reverse=True)


# ---------------------------------------------------------------------------
# 5. Open files collector (files view)
# ---------------------------------------------------------------------------

def unescape_mount_path(path):
    """Undo the octal escaping used in /proc/self/mounts."""
    return (path.replace("\\040", " ").replace("\\011", "\t")
                .replace("\\012", "\n").replace("\\134", "\\"))


# --- Linux mount map (procfs) ----------------------------------------------

def parse_mount_lines_linux(mounts_text):
    """Parse /proc/self/mounts text into (mountpoint, dataset_name) pairs.

    Field 0 is the dataset name, field 1 the mountpoint, field 2 the fstype;
    only zfs mounts are kept. Mountpoints use octal escapes that are undone.
    """
    entries = []
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "zfs":
            mountpoint = unescape_mount_path(fields[1])
            dataset_name = unescape_mount_path(fields[0])
            entries.append((mountpoint, dataset_name))
    return entries


def read_zfs_mount_map_linux(mounts_path=MOUNTS_PATH):
    """Read the Linux kernel mount table and return ZFS mount pairs."""
    with open(mounts_path, "r") as mounts_file:
        return parse_mount_lines_linux(mounts_file.read())


# --- FreeBSD mount map (mount -p) ------------------------------------------

def parse_mount_lines_freebsd(mounts_text):
    """Parse `mount -p` output into (mountpoint, dataset_name) pairs.

    `mount -p` prints an fstab style table:

        tank/data  /tank/data  zfs  rw  0  0

    Field 0 is the dataset name, field 1 the mountpoint, field 2 the fstype.
    Only zfs mounts are kept. This is the same field layout the Linux parser
    uses, but FreeBSD does not octal escape the paths.
    """
    entries = []
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "zfs":
            mountpoint = fields[1]
            dataset_name = fields[0]
            entries.append((mountpoint, dataset_name))
    return entries


def read_zfs_mount_map_freebsd(mounts_path=MOUNTS_PATH):
    """Read the FreeBSD mount table and return ZFS mount pairs.

    When mounts_path still holds the Linux default it means no fixture was
    supplied, so `mount -p` is run. Otherwise the fixture file is read and
    parsed as `mount -p` output for cross testing.
    """
    if mounts_path == MOUNTS_PATH:
        completed = subprocess.run("mount -p", shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   universal_newlines=True)
        return parse_mount_lines_freebsd(completed.stdout)
    with open(mounts_path, "r") as mounts_file:
        return parse_mount_lines_freebsd(mounts_file.read())


# --- Mount map dispatcher --------------------------------------------------

def read_zfs_mount_map(mounts_path=MOUNTS_PATH):
    """Return a list of (mountpoint, dataset_name) for ZFS mounts.

    The list is sorted with the longest mountpoint first so a longest prefix
    match against a file path selects the most specific dataset. The reader
    itself is platform specific; the sorted output is platform neutral.
    """
    if IS_FREEBSD:
        entries = read_zfs_mount_map_freebsd(mounts_path)
    else:
        entries = read_zfs_mount_map_linux(mounts_path)
    entries.sort(key=lambda entry: len(entry[0]), reverse=True)
    return entries


def dataset_for_path(file_path, mount_map):
    """Longest prefix match of file_path against ZFS mountpoints."""
    for mountpoint, dataset_name in mount_map:
        if (file_path == mountpoint
                or file_path.startswith(mountpoint.rstrip("/") + "/")):
            return dataset_name
    return None


def parse_fdinfo_text(text):
    """Parse fdinfo content into (offset, mode)."""
    offset = 0
    mode = "?"
    mode_by_access = {0: "r", 1: "w", 2: "rw"}
    for line in text.splitlines():
        if line.startswith("pos:"):
            try:
                offset = int(line.split()[1])
            except (IndexError, ValueError):
                offset = 0
        elif line.startswith("flags:"):
            try:
                access = int(line.split()[1], 8) & 3
                mode = mode_by_access.get(access, "?")
            except (IndexError, ValueError):
                mode = "?"
    return offset, mode


def read_fdinfo(pid_name, fd_name):
    """Read /proc/<pid>/fdinfo/<fd> and return (offset, mode)."""
    try:
        with open(f"/proc/{pid_name}/fdinfo/{fd_name}", "r") as info:
            return parse_fdinfo_text(info.read())
    except OSError:
        return 0, "?"


def read_process_name(pid_name):
    """Read /proc/<pid>/comm for a friendly process name."""
    try:
        with open(f"/proc/{pid_name}/comm", "r") as comm_file:
            return comm_file.read().strip()
    except OSError:
        return "?"


def scan_open_files_linux(mount_map):
    """Walk /proc/<pid>/fd for all visible processes on ZFS datasets."""
    entries = []
    for pid_name in os.listdir("/proc"):
        if not pid_name.isdigit():
            continue
        fd_dir = f"/proc/{pid_name}/fd"
        try:
            fd_names = os.listdir(fd_dir)
        except (PermissionError, FileNotFoundError):
            continue    # process exited or not ours; both are normal
        for fd_name in fd_names:
            try:
                target = os.readlink(f"{fd_dir}/{fd_name}")
            except OSError:
                continue
            if not target.startswith("/"):
                continue    # sockets, pipes, anon inodes
            dataset_name = dataset_for_path(target, mount_map)
            if dataset_name is None:
                continue
            offset, mode = read_fdinfo(pid_name, fd_name)
            entries.append(OpenFileEntry(
                dataset_name=dataset_name,
                file_path=target,
                pid=int(pid_name),
                process_name=read_process_name(pid_name),
                open_mode=mode,
                offset=offset))
    return entries


# --- FreeBSD open files scanner (procstat) ---------------------------------

def read_procstat_output():
    """Run `procstat -af` and return its text.

    procstat is the FreeBSD equivalent of walking /proc/<pid>/fd. The -a flag
    selects all processes and -f lists their open files. Reading other users'
    processes requires root, exactly like the Linux case. Returns an empty
    string if procstat is missing or fails.
    """
    try:
        completed = subprocess.run("procstat -af", shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   universal_newlines=True)
    except OSError:
        return ""
    return completed.stdout


def parse_procstat_output(text, mount_map):
    """Parse `procstat -af` output into OpenFileEntry objects for ZFS files.

    Each data line has the columns:

        PID COMM FD T V FLAGS REF OFFSET PRO PATH

    T is the descriptor type (v for vnode), V is the vnode type (r for a
    regular file), FLAGS is a string such as "rw" giving the access mode, and
    OFFSET is the file offset used for activity detection. Only numeric file
    descriptors of type vnode with a path on a ZFS dataset are kept. The
    OpenFileEntry fields match the Linux scanner one for one so the renderer
    is platform neutral.
    """
    entries = []
    mode_from_flags = {"r": "r", "w": "w", "rw": "rw", "wr": "rw"}
    for line in text.splitlines():
        fields = line.split(None, 9)
        if len(fields) < 10:
            continue
        pid_text, process_name, fd_text, type_char = fields[0:4]
        flags_text, offset_text, file_path = fields[5], fields[7], fields[9]
        if not pid_text.isdigit():
            continue            # header line or a non numeric summary row
        if not fd_text.isdigit():
            continue            # cwd, root, jail, text, and similar entries
        if type_char != "v":
            continue            # only vnodes are files on disk
        dataset_name = dataset_for_path(file_path, mount_map)
        if dataset_name is None:
            continue
        access = flags_text.replace("-", "")
        mode = mode_from_flags.get(access, "?")
        try:
            offset = int(offset_text)
        except ValueError:
            offset = 0
        entries.append(OpenFileEntry(
            dataset_name=dataset_name,
            file_path=file_path,
            pid=int(pid_text),
            process_name=process_name,
            open_mode=mode,
            offset=offset))
    return entries


def scan_open_files_freebsd(mount_map):
    """List open files on ZFS datasets for all visible processes (FreeBSD)."""
    return parse_procstat_output(read_procstat_output(), mount_map)


# --- Open files scanner dispatcher -----------------------------------------

def scan_open_files(mount_map):
    """List open files on ZFS datasets for all visible processes.

    Platform specific under the hood; the returned OpenFileEntry list is
    platform neutral so scan_and_mark and the renderer are unchanged.
    """
    if IS_FREEBSD:
        return scan_open_files_freebsd(mount_map)
    return scan_open_files_linux(mount_map)


# ---------------------------------------------------------------------------
# 6. Command line and main
# ---------------------------------------------------------------------------

NUMBER_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def is_number(text):
    """A ZFS pool name cannot begin with a digit, so a purely numeric token
    is always an interval or count, never a dataset name."""
    return bool(NUMBER_PATTERN.match(text))


def is_root():
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def current_user_name():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", "unknown")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="zfs-iostat",
        description="Report ZFS I/O activity at dataset granularity.",
        epilog="Quote a dataset/* argument to stop the shell expanding it.")
    parser.add_argument("-t", "--top", action="store_true",
                        help="live ranked view of datasets by I/O rate")
    parser.add_argument("-f", "--files", action="store_true",
                        help="list open files per dataset (lsof style)")
    parser.add_argument("-y", dest="skip_first", action="store_true",
                        help="skip the first (since mount) report")
    parser.add_argument("-p", dest="parsable", action="store_true",
                        help="parsable output, exact counts, no unit suffixes")
    parser.add_argument("-H", dest="scripted", action="store_true",
                        help="scripted mode, tab separated, no headers")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="output JSON with humanized values (12.5M)")
    parser.add_argument("-s", dest="sort_column", default="total",
                        choices=["read", "write", "rops", "wops", "total"],
                        help="sort column for the top view (default total)")
    parser.add_argument("--no-zil", dest="no_zil", action="store_true",
                        help="reserved for future ZIL columns (no effect yet)")

    parser.add_argument("--version", action="version",
                        version=f"zfs-iostat {VERSION}")
    # Hidden flags used for fixture based testing and cross checking.
    parser.add_argument("--kstat-base", dest="kstat_base", default=KSTAT_BASE,
                        help=argparse.SUPPRESS)
    parser.add_argument("--sysctl-cmd", dest="sysctl_cmd",
                        default=FREEBSD_SYSCTL_CMD, help=argparse.SUPPRESS)
    parser.add_argument("--mounts-path", dest="mounts_path",
                        default=MOUNTS_PATH, help=argparse.SUPPRESS)
    parser.add_argument("positionals", nargs="*",
                        metavar="[dataset|dataset/*] [interval [count]]")
    return parser


def classify_positionals(positionals, parser):
    """Split trailing numeric tokens off as interval and count."""
    dataset_argument = None
    interval = None
    count = None
    remaining = list(positionals)
    numeric = []
    while remaining and is_number(remaining[-1]):
        numeric.insert(0, remaining.pop())
    if len(numeric) >= 1:
        interval = float(numeric[0])
    if len(numeric) >= 2:
        count = int(float(numeric[1]))
    if len(numeric) > 2 or len(remaining) > 1:
        parser.error("too many arguments")
    if remaining:
        dataset_argument = remaining[0]
    return dataset_argument, interval, count


def kstat_source_for(options):
    """Return the value passed to collect_samples for the current platform.

    On Linux this is the kstat base directory; on FreeBSD it is the sysctl
    command string. Keeping the selection in one place lets every caller stay
    platform neutral.
    """
    if IS_FREEBSD:
        return options.sysctl_cmd
    return options.kstat_base


def ensure_kstat_source(options):
    """Verify the platform's ZFS statistics source exists before sampling.

    On Linux the kstat base directory must exist. On FreeBSD the kstat.zfs
    sysctl tree must return at least one dataset node; if not, the zfs module
    is probably not loaded.
    """
    if IS_FREEBSD:
        if not collect_samples_freebsd(options.sysctl_cmd):
            sys.stderr.write(
                "ZFS kstats not found. Is the zfs module loaded?\n")
            sys.exit(1)
        return
    if not os.path.isdir(options.kstat_base):
        sys.stderr.write(
            "ZFS kstats not found. Is the zfs module loaded?\n")
        sys.exit(1)


def print_report(rates_list, options):
    """Filter and render one iostat report to stdout."""
    filtered = filter_datasets(rates_list, options.dataset)
    if options.json_output:
        print(render_json(filtered))
        return
    print(render_table(filtered,
                        parsable=options.parsable,
                        scripted=options.scripted))



def run_iostat(options):
    """Default view: the iostat table, one shot or on an interval."""
    ensure_kstat_source(options)
    kstat_source = kstat_source_for(options)
    previous = collect_samples(kstat_source)
    previous_time = time.monotonic()

    if options.dataset is not None and not any(
            dataset_matches(sample.dataset_name, options.dataset)
            for sample in previous.values()):
        sys.stderr.write(
            f"dataset not found or not mounted: {options.dataset}\n")
        return 1

    # The lifetime first report needs crtime and snaptime, which the FreeBSD
    # dataset sysctls do not expose. On FreeBSD the first report is therefore
    # skipped and the tool starts with an interval sample, the same behavior
    # as the -y flag. On Linux the lifetime average is printed as before.
    skip_first = options.skip_first or IS_FREEBSD
    if not skip_first:
        rates = [compute_lifetime_rates(sample)
                 for sample in previous.values()]
        print_report(rates, options)

    if options.interval is None:
        if IS_FREEBSD and not options.skip_first:
            sys.stderr.write(
                "first report is unavailable on FreeBSD; "
                "specify an interval to see per interval rates\n")
        return 0

    reports_printed = 0 if skip_first else 1
    while options.count is None or reports_printed < options.count:
        time.sleep(options.interval)
        current = collect_samples(kstat_source)
        now = time.monotonic()
        elapsed = now - previous_time
        rates = []
        for key, new_sample in current.items():
            old_sample = previous.get(key)
            if old_sample is not None:
                rates.append(compute_interval_rates(
                    old_sample, new_sample, elapsed))
        print_report(rates, options)
        previous = current
        previous_time = now
        reports_printed += 1
    return 0


def draw_top(screen, rates_list, interval, sort_column):
    """Render one frame of the top view into a curses screen."""
    screen.erase()
    max_y, max_x = screen.getmaxyx()
    ordered = sort_rates(rates_list, sort_column)

    header = (f"zfs-iostat top    interval: {interval:.1f}s"
              f"    datasets: {len(rates_list)}    sort: {sort_column}")
    columns = (f"{'DATASET':<24} {'READ/s':>10} {'WRITE/s':>10}"
               f" {'ROPS':>8} {'WOPS':>8}")
    screen.addnstr(0, 0, header, max_x - 1)
    if max_y > 1:
        screen.addnstr(1, 0, columns, max_x - 1)

    row = 2
    for rates in ordered:
        if row >= max_y - 1:
            break
        line = (f"{rates.dataset_name:<24}"
                f" {humanize(rates.read_bytes_per_sec):>10}"
                f" {humanize(rates.write_bytes_per_sec):>10}"
                f" {rates.read_ops_per_sec:>8.0f}"
                f" {rates.write_ops_per_sec:>8.0f}")
        screen.addnstr(row, 0, line, max_x - 1)
        row += 1

    footer = "q quit   r read   w write   o rops   p wops   t total"
    if max_y > 3:
        screen.addnstr(max_y - 1, 0, footer, max_x - 1)
    screen.refresh()


def top_loop(screen, options):
    import curses
    curses.curs_set(0)
    interval = options.interval if options.interval else 1.0
    screen.timeout(int(interval * 1000))
    sort_column = options.sort_column
    kstat_source = kstat_source_for(options)

    previous = collect_samples(kstat_source)
    previous_time = time.monotonic()

    while True:
        key = screen.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key == ord("r"):
            sort_column = "read"
        elif key == ord("w"):
            sort_column = "write"
        elif key == ord("o"):
            sort_column = "rops"
        elif key == ord("p"):
            sort_column = "wops"
        elif key == ord("t"):
            sort_column = "total"

        current = collect_samples(kstat_source)
        now = time.monotonic()
        elapsed = now - previous_time
        rates = []
        for sample_key, new_sample in current.items():
            old_sample = previous.get(sample_key)
            if old_sample is not None:
                rates.append(compute_interval_rates(
                    old_sample, new_sample, elapsed))
        filtered = filter_datasets(rates, options.dataset)
        draw_top(screen, filtered, interval, sort_column)
        previous = current
        previous_time = now


def run_top(options):
    """Live ranked view. Uses curses; restores the terminal on exit."""
    ensure_kstat_source(options)
    import curses
    try:
        curses.wrapper(top_loop, options)
    except KeyboardInterrupt:
        pass
    return 0


def render_files_json(entries):
    """Render the files view as a single line JSON array of objects.

    Entries keep the same sort order as the table renderer.
    """
    objects = []
    for entry in entries:
        objects.append(
            '{"dataset":' + json_escape_string(entry.dataset_name)
            + ',"active":' + ("true" if entry.is_active else "false")
            + ',"mode":' + json_escape_string(entry.open_mode)
            + ',"pid":' + str(entry.pid)
            + ',"process":' + json_escape_string(entry.process_name)
            + ',"file":' + json_escape_string(entry.file_path)
            + '}')
    return "[" + ",".join(objects) + "]"


def print_files_report(entries, options):
    """Render the files view to stdout."""
    ordered = sorted(entries,
                     key=lambda e: (e.dataset_name, not e.is_active,
                                    e.pid, e.file_path))
    if options.json_output:
        print(render_files_json(ordered))
        return
    if options.scripted:
        for entry in ordered:
            fields = [entry.dataset_name,
                      "yes" if entry.is_active else "no",

                      entry.open_mode,
                      str(entry.pid),
                      entry.process_name,
                      entry.file_path]
            print("\t".join(fields))
        return

    if not is_root():
        print(f"showing only processes owned by {current_user_name()};"
              f" run as root for all processes")
    print(f"{'DATASET':<20} {'ACTIVE':<6} {'MODE':<4} {'PID':<7}"
          f" {'PROCESS':<16} FILE")
    for entry in ordered:
        active = "yes" if entry.is_active else "no"
        print(f"{entry.dataset_name:<20} {active:<6} {entry.open_mode:<4}"
              f" {entry.pid:<7} {entry.process_name:<16} {entry.file_path}")


def scan_and_mark(mount_map, previous_offsets):
    """One open file scan. Marks entries whose offset moved as active."""
    entries = scan_open_files(mount_map)
    offsets = {}
    for entry in entries:
        entry_key = (entry.pid, entry.file_path)
        offsets[entry_key] = entry.offset
        if (entry_key in previous_offsets
                and previous_offsets[entry_key] != entry.offset):
            entry.is_active = True
    return entries, offsets


def run_files(options):
    """Open files view: which files on which datasets are being accessed."""
    mount_map = read_zfs_mount_map(options.mounts_path)

    if options.interval is None:
        # Two scans one second apart so the ACTIVE column is meaningful.
        _, first_offsets = scan_and_mark(mount_map, {})
        time.sleep(1.0)
        entries, _ = scan_and_mark(mount_map, first_offsets)
        print_files_report(entries, options)
        return 0

    previous_offsets = {}
    reports_printed = 0
    while options.count is None or reports_printed < options.count:
        entries, previous_offsets = scan_and_mark(mount_map, previous_offsets)
        print_files_report(entries, options)
        reports_printed += 1
        if options.count is not None and reports_printed >= options.count:
            break
        time.sleep(options.interval)
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.top and args.files:
        parser.error("-t and -f are mutually exclusive")
    if args.top and args.json_output:
        parser.error("--json is not supported with the top view")

    dataset, interval, count = classify_positionals(args.positionals, parser)


    if interval is not None and interval <= 0:
        parser.error("interval must be greater than 0")
    if count is not None and count < 1:
        parser.error("count must be at least 1")

    options = Options(
        dataset=dataset,
        interval=interval,
        count=count,
        skip_first=args.skip_first,
        parsable=args.parsable,
        scripted=args.scripted,
        json_output=args.json_output,
        sort_column=args.sort_column,

        top=args.top,
        files=args.files,
        kstat_base=args.kstat_base,
        sysctl_cmd=args.sysctl_cmd,
        mounts_path=args.mounts_path)

    try:
        if options.top:
            return run_top(options)
        if options.files:
            return run_files(options)
        return run_iostat(options)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

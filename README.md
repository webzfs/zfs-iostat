# zfs-iostat (Python)

zfs-iostat reports ZFS I/O activity at dataset granularity. Where `zpool iostat` only reports at the pool and vdev level, this tool breaks the numbers down per dataset, so you can see which filesystem or zvol is driving the load on a pool.

This repo contains the Python implementation, I am planning on a C++ version but Python was easier for me to prototype out. It is a single file, `zfs_iostat.py`, and has no third party dependencies (curses comes from the Python standard library).

## Requirements

- Linux with the ZFS kernel module loaded (the tool reads `/proc/spl/kstat/zfs`), or FreeBSD with ZFS loaded (the tool reads the `kstat.zfs` sysctl tree).
- Python 3.11 or newer.
- Root is not required on Linux for the iostat and top views. For the files view, root lets you see files opened by other users' processes.

## Platform support

The tool runs on both Linux and FreeBSD and detects the platform at runtime.

- Linux: dataset counters come from `/proc/spl/kstat/zfs`, the mount map from `/proc/self/mounts`, and the files view from `/proc/<pid>/fd` and `/proc/<pid>/fdinfo`.
- FreeBSD: dataset counters come from the `kstat.zfs` sysctl tree (read with `sysctl`), the mount map from `mount -p`, and the files view from `procstat -af`. (Root required on FreeBSD)

FreeBSD note: the dataset sysctls do not expose the counter creation and snapshot times, so the lifetime average first report (the since mount numbers) is not available on FreeBSD. The first report is skipped, the same as passing `-y`. Give an interval to see per interval rates. If you run the tool with no interval on FreeBSD, it prints a note explaining this.

## Running

The tool is a plain script. Run it with Python:

```
python3 zfs_iostat.py [options] [dataset | dataset/*] [interval [count]]
```

You can also mark it executable and run it directly:

```
chmod +x zfs_iostat.py
./zfs_iostat.py
```

## Views

The tool has three views. The default is the iostat table. The other two are selected with a flag.

### iostat view (default)

Prints a `zpool iostat` style table of per dataset read and write operations and bandwidth.

```
python3 zfs_iostat.py
```

Example output:

```
                              operations          bandwidth
dataset                       read     write      read     write
------------------------ --------- --------- --------- ---------
tank/data                      124        31     15.2M      3.1M
tank/home                        0         2         0        24K
```

The first report shows averages over the life of each dataset's counters, the same as `zpool iostat`. When you give an interval, each later report shows the rate over the time since the previous sample.

### top view (`-t`)

A live, full screen ranked display of datasets sorted by current I/O rate, in the spirit of `top` or `nethogs`.

```
python3 zfs_iostat.py -t
python3 zfs_iostat.py -t 2        # refresh every 2 seconds
```

Keys while it is running:

- `q` quit
- `r` sort by read bandwidth
- `w` sort by write bandwidth
- `o` sort by read operations
- `p` sort by write operations
- `t` sort by total bandwidth (default)

### files view (`-f`)

An `lsof` style listing of open files on ZFS datasets, with an ACTIVE column that flags files whose read/write offset moved between scans.

```
python3 zfs_iostat.py -f
sudo python3 zfs_iostat.py -f     # see files from all users' processes
```

Example output:

```
DATASET          ACTIVE MODE PID     PROCESS       FILE
tank/vm/images   yes    rw   2214    qemu-kvm      /tank/vm/images/win.qcow2
tank/data        yes    r    8841    rsync         /tank/data/projects/big.tar
tank/data        no     r    1023    smbd          /tank/data/media/song.mp3
```

Limitations of the files view:

- Activity detection watches the file offset, so it misses memory mapped I/O and positioned reads and writes (pread/pwrite), which do not move the   offset.
- In kernel consumers, such as the kernel NFS or SMB servers, do not appear.
- zvols have no file paths and are not listed here.
- Without root you only see files opened by your own processes; the tool prints a note when this is the case.

## Positional arguments

The positional arguments follow `zpool iostat` conventions:

```
[dataset | dataset/*] [interval [count]]
```

- No dataset: report all mounted datasets and active zvols.
- `dataset`: report exactly that dataset (for example `tank/data`).
- `dataset/*`: report that dataset and all of its descendants. Quote it so the shell does not expand the `*`, for example `"tank/data/*"`.
- `interval`: seconds between reports. With no interval, one report is printed and the tool exits. Fractional values are allowed, for example
  `0.5`.
- `count`: number of reports to print, then exit. Only valid after an interval.

Numbers are always treated as interval or count, never as a dataset name, because ZFS pool names cannot begin with a digit.

Examples:

```
python3 zfs_iostat.py                 # one report, all datasets
python3 zfs_iostat.py 2               # every 2 seconds, all datasets
python3 zfs_iostat.py 2 5             # 5 reports at 2 second intervals
python3 zfs_iostat.py tank/data       # one report for tank/data
python3 zfs_iostat.py tank/data 1     # tank/data every second
python3 zfs_iostat.py "tank/*" 2      # tank and all descendants, every 2s
```

Press Ctrl-C at any time to exit cleanly.

## Options

- `-t`, `--top`: live ranked top view. Uses the interval as the refresh rate (default 1 second).
- `-f`, `--files`: open files view. With an interval it refreshes and marks active files; without one it takes two scans a second apart so the ACTIVE column is meaningful.
- `-y`: skip the first report (the since mount averages), like `zpool iostat -y`.
- `-p`: parsable output. Exact operation and byte counts with no unit suffixes.
- `-H`: scripted mode. Tab separated columns and no header lines, for use in pipelines.
- `--json`: JSON output. Prints a single line JSON array of objects, one per dataset (or per open file in the files view). Numeric values are humanized strings with at most one decimal place, for example `12.5M`. Not supported with the top view.
- `-s COLUMN`: sort column for the top view. One of `read`, `write`, `rops`, `wops`, or `total` (default `total`).
- `--no-zil`: reserved for future ZIL columns. No effect yet.
- `--version`: print the version and exit.
- `-h`, `--help`: print usage and exit.

*`-t` and `-f` cannot be used together.*

Examples:

```
python3 zfs_iostat.py -H -p              # tab separated exact counts
python3 zfs_iostat.py -y 1               # skip the lifetime report
python3 zfs_iostat.py -t -s read         # top view sorted by read bandwidth
python3 zfs_iostat.py --json             # JSON array of per dataset rates
python3 zfs_iostat.py -f --json          # JSON array of open files
```

Example JSON output (formatted here for readability; the tool prints it on a single line):

```
[{"dataset":"tank/data","read_ops":"124","write_ops":"31",
  "read_bandwidth":"15.2M","write_bandwidth":"3.1M"}]
```


## Exit codes

- `0`: success.
- `1`: runtime error, such as no ZFS kstats found or a named dataset that is not mounted.
- `2`: usage error, such as bad arguments.

## Notes on the numbers

The counters come from ZFS dataset kstats, which measure logical I/O at the ZFS POSIX layer. They will not exactly match pool level physical I/O, because of the ARC cache, compression, and RAID write amplification. Treat the figures as per dataset logical activity, not raw disk traffic.

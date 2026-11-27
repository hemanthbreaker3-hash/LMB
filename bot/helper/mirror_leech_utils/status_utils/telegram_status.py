from ...ext_utils.status_utils import (
    MirrorStatus,
    EngineStatus,
    get_readable_file_size,
    get_readable_time,
)


class TelegramStatus:
    def __init__(self, listener, obj, gid, status, hyper):
        self.listener = listener
        self._obj = obj
        self._size = self.listener.size
        self._gid = gid
        self._status = status
        self.engine = EngineStatus().STATUS_TGRAM + {
            "cb": " (cbgram)",
            "hdl": " (HyperDL)",
            "hul": " (HyperUP)",
        }.get(hyper, "")

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def size(self):
        return get_readable_file_size(self._size)

    def status(self):
        if self._status == "up":
            if getattr(self._obj, "_upload_waiting", False):
                return MirrorStatus.STATUS_QUEUEUP
            return MirrorStatus.STATUS_UPLOAD
        return MirrorStatus.STATUS_DOWNLOAD

    def current_file(self):
        idx = getattr(self._obj, "_current_file_index", 0)
        total = getattr(self._obj, "_total_files", 0)
        name = getattr(self._obj, "_current_file_name", "")
        if name and idx:
            total_str = f"/{total}" if total else ""
            return f"[{idx}{total_str}] {name}"
        return ""

    def name(self):
        return self.listener.name

    def progress(self):
        try:
            progress_raw = self._obj.processed_bytes / self._size * 100
        except ZeroDivisionError:
            progress_raw = 0
        return f"{round(progress_raw, 2)}%"

    def speed(self):
        # Bytes/sec + Mbps so ≥200 Mbps class is obvious
        # (e.g. 25 MB/s ≈ 200 Mbps). 1 byte/s = 8 bit/s.
        bps = float(getattr(self._obj, "speed", 0) or 0)
        human = get_readable_file_size(bps)
        mbps = (bps * 8) / 1_000_000
        if mbps >= 10:
            return f"{human}/s (~{mbps:.0f} Mbps)"
        if mbps >= 1:
            return f"{human}/s (~{mbps:.1f} Mbps)"
        return f"{human}/s"

    def eta(self):
        try:
            spd = float(getattr(self._obj, "speed", 0) or 0)
            if spd <= 0:
                return "-"
            seconds = max(0, (self._size - self._obj.processed_bytes) / spd)
            return get_readable_time(seconds)
        except Exception:
            return "-"

    def gid(self):
        return self._gid

    def task(self):
        return self._obj

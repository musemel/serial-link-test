"""シリアル通信と、同一PC内のTCP通信を試験する。"""
import argparse
import ctypes
import logging
import math
import os
import socket
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# シリアル通信条件。接続する両端で同じ設定にする。
BAUD = 9600
DATA_BITS = 8  # ASCIIを使うため7または8
PARITY = "N"  # N=なし、E=偶数、O=奇数、M=マーク、S=スペース
STOP_BITS = 1
TIMEOUT_MARGIN_SECONDS = 30
PROGRESS_SECONDS = 5
HEADER_BYTES = 8
MIN_SIZE = HEADER_BYTES + 1  # 16進の巡番号と改行を含む
POLL_SECONDS = 0.2
PATTERN = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
XON = 0x11
XOFF = 0x13


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("serial", "tcp"), default="serial")
    parser.add_argument("--com", type=int, help="COMポート番号")
    parser.add_argument("--tcp-port", type=int, default=8765, help="localhostのポート")
    parser.add_argument("--size", type=int, help=f"1巡の送信バイト数（{MIN_SIZE}以上）")
    parser.add_argument("--count", type=int, help="巡数（0=無制限）")
    parser.add_argument("--wait", type=float, help="巡間のウェイト秒数")
    parser.add_argument("--mode", choices=("send", "receive", "mutual"))
    parser.add_argument("--first", choices=("yes", "no"), help="交互モードで先に送るか")
    parser.add_argument("--xonxoff", choices=("yes", "no", "software"), help="OS制御 / 無効 / アプリで受信制御")
    parser.add_argument("--timeout", type=float, help="受信と送信のタイムアウト秒数")
    parser.add_argument("--dump-rx", action="store_true", help="受信した全バイトをログに記録")
    parser.add_argument("--display", choices=("progress", "round", "full"), default="round",
                        help="進捗のみ、巡ごとの結果、巡ごとの結果と全受信バイト")
    args = parser.parse_args()

    def fill(name, prompt, parse, valid):
        value = getattr(args, name)
        if value is not None:
            if not valid(value):
                parser.error(f"--{name} の指定値が範囲外です")
            return value
        while True:
            try:
                value = parse(input(prompt + ": ").strip().lower())
                if valid(value):
                    return value
            except ValueError:
                pass
            except EOFError:
                parser.error(f"--{name} を指定してください")
            print("入力値を確認してください。")

    if args.transport == "serial":
        args.com = fill("com", "COMポート番号", int, lambda v: v > 0)
        args.xonxoff = fill("xonxoff", "XON/XOFF [yes/no/software]", str, ("yes", "no", "software").__contains__)
    elif not 1 <= args.tcp_port <= 65535:
        parser.error("--tcp-port は1～65535を指定してください")
    args.size = fill("size", f"1巡のバイト数 ({MIN_SIZE}以上、改行込み)", int, lambda v: v >= MIN_SIZE)
    args.count = fill("count", "巡数 (0=無制限)", int, lambda v: v >= 0)
    args.wait = fill("wait", "巡間のウェイト (秒)", float, lambda v: math.isfinite(v) and v >= 0)
    args.mode = fill("mode", "モード [send/receive/mutual]",
                     lambda v: {"1": "send", "2": "receive", "3": "mutual"}.get(v, v),
                     ("send", "receive", "mutual").__contains__)
    if args.mode == "mutual":
        args.first = fill("first", "先に送りますか [yes/no]", str, ("yes", "no").__contains__) == "yes"
    else:
        args.first = args.mode == "send"
    if args.timeout is None:
        bits_per_byte = 1 + DATA_BITS + (PARITY != "N") + STOP_BITS
        transfer = args.size * bits_per_byte / BAUD if args.transport == "serial" else 0
        args.timeout = TIMEOUT_MARGIN_SECONDS + args.wait + transfer
    elif not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout は正の秒数を指定してください")
    return args


def frame(number, size):
    # 巡番号を明示し、受信側の途中参加とデータ欠落を区別する。
    header = f"{number:0{HEADER_BYTES}x}".encode("ascii")
    length = size - len(header) - 1
    # 巡ごとに開始位置をずらし、指定サイズまでA～Zを繰り返す。
    offset = (number - 1) % len(PATTERN)
    pattern = PATTERN[offset:] + PATTERN[:offset]
    repeats, tail = divmod(length, len(pattern))
    return header + pattern * repeats + pattern[:tail] + b"\n"


@contextmanager
def monitor_flow(port, enabled):
    """Windowsドライバーが報告した送信保留を、書き込み待ち中も監視する。"""
    handle = getattr(port, "_port_handle", None)
    if not enabled or handle is None or os.name != "nt":
        yield
        return
    from serial import win32

    stopped = threading.Event()

    def observe():
        flags = win32.DWORD()
        status = win32.COMSTAT()
        held = False
        while not stopped.is_set():
            if not win32.ClearCommError(handle, ctypes.byref(flags), ctypes.byref(status)):
                logging.warning("FLOW Windowsドライバーの状態取得に失敗")
                return
            current = bool(status.fXoffHold)
            if current != held:
                held = current
                logging.info("FLOW %s", "XOFFによる送信保留を検出" if held else "送信保留解除を検出")
            stopped.wait(0.02)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    try:
        yield
    finally:
        stopped.set()
        observer.join()



def frame_number(data):
    header = data[:HEADER_BYTES]
    if len(header) == HEADER_BYTES and all(b in b"0123456789abcdef" for b in header):
        return int(header, 16)
    return None


@contextmanager
def connect(args):
    if args.transport == "serial":
        try:
            import serial
        except ImportError:
            raise SystemExit("pyserial が必要です。python -m pip install pyserial")
        with serial.Serial(
            f"COM{args.com}", BAUD, bytesize=DATA_BITS, parity=PARITY, stopbits=STOP_BITS,
            timeout=min(POLL_SECONDS, args.timeout), write_timeout=args.timeout,
            xonxoff=args.xonxoff == "yes",
        ) as port:
            with monitor_flow(port, args.xonxoff == "yes"):
                yield port
        return

    address = ("127.0.0.1", args.tcp_port)
    if args.first:
        # 接続ができるまで試験データを送らないので、どちらから起動してもよい。
        logging.info("TCP 接続待ち %s:%d", *address)
        while True:
            try:
                port = socket.create_connection(address, timeout=POLL_SECONDS)
                break
            except (ConnectionRefusedError, TimeoutError):
                time.sleep(POLL_SECONDS)
    else:
        with socket.socket() as listener:
            listener.bind(address)
            listener.listen(1)
            listener.settimeout(POLL_SECONDS)
            logging.info("TCP 受け付け待ち %s:%d", *address)
            while True:
                try:
                    port, _ = listener.accept()
                    break
                except TimeoutError:
                    continue
    with port:
        port.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        logging.info("TCP 接続済み")
        yield port


class Link:
    """TCPとシリアルのバイト列を、共通の改行区切りフレームとして扱う。"""

    def __init__(self, port, args):
        self.port = port
        self.tcp = args.transport == "tcp"
        self.timeout = args.timeout
        self.pending = bytearray()
        self.controls = Counter()
        self.dump_rx = args.dump_rx
        self.software_flow = not self.tcp and args.xonxoff == "software"
        self.paused = False

    def serial_data(self, chunk):
        # 制御文字を受信順に処理し、試験データから分離する。
        for byte, label in ((XON, "XON"), (XOFF, "XOFF")):
            count = chunk.count(bytes((byte,)))
            if count:
                if not self.controls[label]:
                    logging.info("FLOW %sを読み取りで観測（データ集計から除外）", label)
                self.controls[label] += count
        if self.dump_rx and (XON in chunk or XOFF in chunk):
            logging.debug("FLOW 制御文字を含む読み取り全量 %r", chunk)
        if self.software_flow:
            for byte in chunk:
                if byte in (XON, XOFF) and self.paused != (byte == XOFF):
                    self.paused = byte == XOFF
                    logging.info("FLOW アプリ %s", "XOFFで送信保留" if self.paused else "XONで送信再開")
        return chunk.translate(None, bytes((XON, XOFF)))

    def send(self, data):
        if self.tcp:
            self.port.settimeout(self.timeout)
            self.port.sendall(data)
            return
        deadline = time.monotonic() + self.timeout
        # アプリ制御時は小分けに送信し、次の書き込み前に制御文字を処理する。
        if self.software_flow:
            for start in range(0, len(data), 64):
                while True:
                    available = self.port.in_waiting
                    if available:
                        self.pending.extend(self.serial_data(self.port.read(min(available, 65536))))
                    if time.monotonic() >= deadline:
                        raise TimeoutError("XON待ちがタイムアウト" if self.paused else "送信がタイムアウト")
                    if not self.paused and not self.port.out_waiting:
                        break
                    time.sleep(0.005)
                self.write_serial(data[start:start + 64], deadline)
            return
        self.write_serial(data, deadline)

    def write_serial(self, data, deadline):
        written = self.port.write(data)
        if written != len(data):
            raise OSError(f"部分書き込み {written}/{len(data)} bytes")
        while self.port.out_waiting:
            if time.monotonic() >= deadline:
                raise TimeoutError("送信バッファの排出待ちがタイムアウト")
            time.sleep(0.01)

    def receive(self, size):
        deadline = time.monotonic() + self.timeout
        closed = False
        # 一度に複数巡届くことがあるので、改行の後ろは次の呼び出しに残す。
        while True:
            end = self.pending.find(b"\n")
            if end >= 0:
                data = bytes(self.pending[:end + 1])
                del self.pending[:end + 1]
                return data, False
            remaining = deadline - time.monotonic()
            if closed or remaining <= 0 or len(self.pending) > max(size * 2, 65536):
                data = bytes(self.pending)
                self.pending.clear()
                return data, closed
            poll = min(POLL_SECONDS, remaining)
            if self.tcp:
                self.port.settimeout(poll)
                try:
                    chunk = self.port.recv(65536)
                except TimeoutError:
                    continue
                closed = not chunk
            else:
                chunk = self.port.read(min(self.port.in_waiting or 1, 65536))
                chunk = self.serial_data(chunk)
            self.pending.extend(chunk)


def verdict(data, expected, closed):
    if data == expected:
        return "OK", "欠落なし"
    if closed:
        return "DISCONNECTED", "相手切断"
    if not data:
        return "RX_TIMEOUT", "受信期限まで0バイト"
    if not data.endswith(b"\n"):
        return "SYNC_ERROR", "終端未受信"
    if len(data) < len(expected):
        return "SHORT", f"不足={len(expected) - len(data)} bytes"
    if len(data) > len(expected):
        return "LONG", "受信長超過"
    index = next(i for i, (actual, wanted) in enumerate(zip(data, expected)) if actual != wanted)
    return "MISMATCH", f"先頭不一致位置={index + 1} 期待=0x{expected[index]:02X} 実際=0x{data[index]:02X}"


def run(link, args, totals):
    number = 1
    aligned = False
    next_progress = time.monotonic() + PROGRESS_SECONDS
    directions = ("TX", "RX") if args.first else ("RX", "TX")
    if args.mode != "mutual":
        directions = directions[:1]
    while (not args.count or number <= args.count) and number < 16 ** HEADER_BYTES:
        expected = frame(number, args.size)
        row = {"TX": "-", "RX": "-"}
        data = None
        stop = False
        waiting = False
        for direction in directions:
            started = time.monotonic()
            logging.debug("巡=%d %s 開始", number, direction)
            try:
                if direction == "TX":
                    link.send(expected)
                    count, status, detail = len(expected), "OK", "欠落は受信側で判定"
                else:
                    data, closed = link.receive(args.size)
                    count = len(data)
                    received = frame_number(data)
                    # 正常な後続フレームから巡番号を復元する。同期後の飛びは異常として集計。
                    if (received is not None and received > number
                            and (not args.count or received <= args.count)
                            and data == frame(received, args.size)):
                        missing = "GAP" if aligned or args.first else "SKIP"
                        totals["RX", missing] += received - number
                        logging.log(logging.WARNING if missing == "GAP" else logging.INFO,
                                    "巡=%d～%d RX %s", number, received - 1, missing)
                        number = received
                        expected = frame(number, args.size)
                    if not aligned and not data and not closed:
                        status, detail = "SKIP", "初回受信待ち、判定省略"
                        waiting = not args.first
                    else:
                        status, detail = verdict(data, expected, closed)
                        aligned |= status == "OK"
                    if args.dump_rx:
                        logging.debug("巡=%d RX全量 %r", number, data)
            except OSError as exc:
                totals[direction, "ERROR"] += 1
                logging.error("巡=%d %s ERROR %.3fs %s", number, direction,
                              time.monotonic() - started, exc)
                raise
            if not waiting:
                totals[direction, status] += 1
            elapsed = time.monotonic() - started
            row[direction] = f"{status} {count}B {elapsed:.3f}s"
            logging.debug("巡=%d %s 結果=%s bytes=%d/%d 経過=%.3fs %s",
                          number, direction, status, count, args.size, elapsed, detail)
            if status not in ("OK", "SKIP") and totals[direction, status] == 1:
                logging.warning("巡=%d %s 初回異常=%s %s", number, direction, status, detail)
            stop = status == "DISCONNECTED"
            # 応答側は要求の境界が不明な巡に返答せず、次の巡を受け付ける。
            if stop or status in ("SKIP", "RX_TIMEOUT", "SYNC_ERROR"):
                break

        # 巡ごとの固定幅の行。全量表示では同じ巡番号の行に受信データを続ける。
        if args.display != "progress":
            logging.info("巡=%08d  TX %-24s  RX %-24s", number, row["TX"], row["RX"])
            if args.display == "full" and data is not None:
                print(f"巡={number:08d}  RX全量 {data!r}", file=sys.stderr, flush=True)
        elif time.monotonic() >= next_progress:
            logging.info("進捗 巡=%d TX成功=%d RX正常=%d RXスキップ=%d",
                         number, totals["TX", "OK"], totals["RX", "OK"], totals["RX", "SKIP"])
            next_progress = time.monotonic() + PROGRESS_SECONDS
        if stop:
            break
        if not waiting:
            number += 1
        if args.count and number > args.count:
            break
        if not waiting:
            time.sleep(args.wait)


def main():
    try:
        args = options()
    except KeyboardInterrupt:
        return 130

    # マイクロ秒とPIDで、同じフォルダーから同時起動してもログを分離する。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = Path.cwd() / f"serial_test_{stamp}_{os.getpid()}.log"
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(path, mode="x", encoding="utf-8"), console],
    )
    logging.info("ログ=%s", path)
    logging.info("開始 方式=%s モード=%s 送信先行=%s bytes=%d 巡数=%s wait=%gs timeout=%gs",
                 args.transport, args.mode, args.first, args.size, args.count or "無制限",
                 args.wait, args.timeout)
    if args.transport == "serial":
        logging.info("通信条件 COM%d %d baud %d%s%g XON/XOFF=%s", args.com, BAUD,
                     DATA_BITS, PARITY, STOP_BITS, args.xonxoff)

    totals = Counter()
    exit_code = 0
    try:
        with connect(args) as port:
            link = Link(port, args)
            try:
                run(link, args, totals)
            finally:
                if args.transport == "serial":
                    logging.info("FLOW アプリで観測した制御文字 XON=%d XOFF=%d",
                                 link.controls["XON"], link.controls["XOFF"])
        exit_code = int(any(count for (_, status), count in totals.items()
                            if status not in ("OK", "SKIP")))
    except KeyboardInterrupt:
        logging.info("Ctrl+Cで中断")
        exit_code = 130
    except (OSError, ValueError) as exc:
        logging.error("通信終了 %s", exc)
        exit_code = 1
    finally:
        for direction in ("TX", "RX"):
            statuses = ("OK", "SKIP", "SHORT", "LONG", "MISMATCH", "GAP", "RX_TIMEOUT", "SYNC_ERROR", "DISCONNECTED", "ERROR")
            counts = " ".join(f"{status}={totals[direction, status]}" for status in statuses)
            logging.info("集計 %s %s", direction, counts)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

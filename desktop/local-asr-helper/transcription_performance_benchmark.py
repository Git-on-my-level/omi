#!/usr/bin/env python3
import argparse
import base64
import json
import os
import resource
import select
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode


SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS


def load_env(repo_root):
    for path in [
        repo_root / "desktop" / "Backend-Rust" / ".env",
        repo_root / "desktop" / ".env",
        repo_root / ".env",
    ]:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(
            f"command failed: {' '.join(map(str, command))}\n{result.stderr.strip()}"
        )
    return result


def normalize_audio(input_path, output_path):
    if not input_path.exists():
        raise SystemExit(f"audio file does not exist: {input_path}")
    if shutil_which("ffmpeg"):
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                str(output_path),
            ]
        )
        return
    if shutil_which("afconvert"):
        wav_path = output_path.with_suffix(".wav")
        run(
            [
                "afconvert",
                str(input_path),
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{SAMPLE_RATE}",
                "-c",
                "1",
                str(wav_path),
            ]
        )
        extract_wav_pcm(wav_path, output_path)
        return
    raise SystemExit("audio normalization requires ffmpeg or afconvert")


def shutil_which(name):
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def extract_wav_pcm(wav_path, pcm_path):
    import wave

    with wave.open(str(wav_path), "rb") as wav:
        if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{wav_path} is not {SAMPLE_RATE} Hz mono signed 16-bit PCM")
        pcm_path.write_bytes(wav.readframes(wav.getnframes()))


def word_count(text):
    return len([part for part in text.replace("\n", " ").split(" ") if part.strip()])


def process_rows():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pcpu=,rss="],
        text=True,
        capture_output=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])))
        except ValueError:
            pass
    return rows


def expand_process_tree(root_pids):
    root_pids = {pid for pid in root_pids if pid}
    rows = process_rows()
    children_by_parent = {}
    metrics = {}
    for pid, ppid, cpu, rss in rows:
        children_by_parent.setdefault(ppid, []).append(pid)
        metrics[pid] = (cpu, rss)
    seen = set(root_pids)
    stack = list(root_pids)
    while stack:
        pid = stack.pop()
        for child in children_by_parent.get(pid, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen, metrics


class Sampler:
    def __init__(self, mode, pid_provider, jsonl_path, interval):
        self.mode = mode
        self.pid_provider = pid_provider
        self.jsonl_path = jsonl_path
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            timestamp = time.time()
            pids, metrics = expand_process_tree(self.pid_provider())
            cpu_percent = sum(metrics.get(pid, (0.0, 0))[0] for pid in pids)
            rss_kb = sum(metrics.get(pid, (0.0, 0))[1] for pid in pids)
            sample = {
                "timestamp": timestamp,
                "mode": self.mode,
                "pids": sorted(pids),
                "process_cpu_percent": cpu_percent,
                "rss_bytes": rss_kb * 1024,
            }
            self.samples.append(sample)
            with self.jsonl_path.open("a") as handle:
                handle.write(json.dumps({"type": "sample", **sample}, sort_keys=True) + "\n")
            self._stop.wait(self.interval)


class DeepgramStreamingClient:
    def __init__(self, api_key, language, on_segment):
        self.api_key = api_key
        self.language = language
        self.on_segment = on_segment
        self.sock = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.receiver = None
        self.bytes_sent = 0
        self.bytes_received = 0

    def connect(self):
        query = {
            "model": "nova-3",
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "channels": "1",
            "interim_results": "false",
            "smart_format": "true",
            "punctuate": "true",
        }
        if self.language != "multi":
            query["language"] = self.language
        path = "/v1/listen?" + urlencode(query)
        raw = socket.create_connection(("api.deepgram.com", 443), timeout=15)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname="api.deepgram.com")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            "Host: api.deepgram.com\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Token {self.api_key}\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self.sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(response.decode("utf-8", errors="replace"))
        self.receiver = threading.Thread(target=self._receive_loop, daemon=True)
        self.receiver.start()

    def send_audio(self, data):
        self._send_frame(0x2, data)
        self.bytes_sent += len(data)

    def finalize(self):
        self._send_frame(0x1, b'{"type":"Finalize"}')
        time.sleep(2)
        self._send_frame(0x8, b"")
        self.stop_event.set()
        if self.receiver:
            self.receiver.join(timeout=3)
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_frame(self, opcode, payload):
        payload = bytes(payload)
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + mask + masked)

    def _receive_loop(self):
        while not self.stop_event.is_set():
            try:
                readable, _, _ = select.select([self.sock], [], [], 0.25)
                if not readable:
                    continue
                opcode, payload = self._read_frame()
                self.bytes_received += len(payload)
                if opcode == 0x1:
                    self._handle_text(payload)
                elif opcode == 0x8:
                    return
                elif opcode == 0x9:
                    self._send_frame(0xA, payload)
            except Exception as error:
                if not self.stop_event.is_set():
                    raise RuntimeError(f"Deepgram receive failed: {error}") from error
                return

    def _read_exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise EOFError("socket closed")
            data += chunk
        return data

    def _read_frame(self):
        first, second = struct.unpack("!BB", self._read_exact(2))
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _handle_text(self, payload):
        message = json.loads(payload.decode("utf-8"))
        if message.get("type") != "Results":
            return
        if not (message.get("is_final") or message.get("speech_final")):
            return
        transcript = (
            message.get("channel", {})
            .get("alternatives", [{}])[0]
            .get("transcript", "")
            .strip()
        )
        if not transcript:
            return
        start = float(message.get("start") or 0.0)
        duration = float(message.get("duration") or 0.0)
        self.on_segment(
            {
                "id": f"deepgram-{len(transcript)}-{start:.2f}",
                "text": transcript,
                "start": start,
                "end": start + duration,
            }
        )


def resource_cpu_seconds(who):
    usage = resource.getrusage(who)
    return usage.ru_utime + usage.ru_stime


def summarize(mode, audio_duration, wall_seconds, cpu_seconds, samples, segments, extra):
    rss_values = [sample["rss_bytes"] for sample in samples]
    transcript = " ".join(segment.get("text", "") for segment in segments).strip()
    latencies = [segment.get("latency_seconds") for segment in segments if segment.get("latency_seconds") is not None]
    return {
        "mode": mode,
        "audio_duration_seconds": audio_duration,
        "wall_duration_seconds": wall_seconds,
        "realtime_factor": wall_seconds / audio_duration if audio_duration else None,
        "cpu_seconds": cpu_seconds,
        "cpu_seconds_per_audio_minute": cpu_seconds / (audio_duration / 60.0) if audio_duration else None,
        "average_rss_bytes": sum(rss_values) / len(rss_values) if rss_values else 0,
        "peak_rss_bytes": max(rss_values) if rss_values else 0,
        "produced_segment_count": len(segments),
        "produced_word_count": word_count(transcript),
        "p50_latency_seconds": percentile(latencies, 50),
        "p95_latency_seconds": percentile(latencies, 95),
        "dropped_chunks": 0,
        "pending_local_asr_chunks": 0,
        **extra,
    }


def percentile(values, percent):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((percent / 100) * (len(values) - 1))))
    return values[index]


def run_baseline(pcm, args, jsonl_path):
    active = [os.getpid()]
    sampler = Sampler("baseline", lambda: active, jsonl_path, args.sample_interval)
    start_cpu = resource_cpu_seconds(resource.RUSAGE_SELF)
    started = time.time()
    sampler.start()
    if args.realtime:
        time.sleep(audio_duration(pcm))
    else:
        _ = pcm.read_bytes()
    sampler.stop()
    wall = time.time() - started
    cpu = resource_cpu_seconds(resource.RUSAGE_SELF) - start_cpu
    return summarize("baseline", audio_duration(pcm), wall, cpu, sampler.samples, [], {})


def run_local_whisper(pcm, args, jsonl_path):
    helper = resolve_helper(args)
    active_child = {"pid": None}
    sampler = Sampler(
        "local-whisper",
        lambda: [os.getpid()] + ([active_child["pid"]] if active_child["pid"] else []),
        jsonl_path,
        args.sample_interval,
    )
    segments = []
    started = time.time()
    child_cpu_start = resource_cpu_seconds(resource.RUSAGE_CHILDREN)
    sampler.start()
    chunks = split_pcm(pcm.read_bytes(), args.local_chunk_seconds)
    duration = audio_duration(pcm)
    for index, chunk in enumerate(chunks):
        chunk_path = pcm.with_name(f"chunk-{index:04d}.pcm")
        chunk_path.write_bytes(chunk)
        request = {
            "request_id": f"benchmark-{index}",
            "audio_path": str(chunk_path),
            "language": args.language,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "engine": args.engine,
            "model": args.model,
        }
        chunk_started = time.time()
        process = subprocess.Popen(
            [str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        active_child["pid"] = process.pid
        stdout, stderr = process.communicate(json.dumps(request), timeout=args.local_timeout)
        active_child["pid"] = None
        chunk_path.unlink(missing_ok=True)
        if process.returncode != 0:
            raise SystemExit(f"local ASR helper failed for chunk {index}: {stderr.strip()}")
        response = json.loads(stdout)
        offset = index * args.local_chunk_seconds
        for segment in response.get("segments", []):
            segment["start"] = float(segment.get("start", 0.0)) + offset
            segment["end"] = float(segment.get("end", segment["start"])) + offset
            segment["latency_seconds"] = time.time() - chunk_started
            segments.append(segment)
        if args.realtime:
            target_elapsed = min(duration, (index + 1) * args.local_chunk_seconds)
            sleep_seconds = started + target_elapsed - time.time()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    sampler.stop()
    wall = time.time() - started
    cpu = resource_cpu_seconds(resource.RUSAGE_CHILDREN) - child_cpu_start
    return summarize("local-whisper", audio_duration(pcm), wall, cpu, sampler.samples, segments, {})


def run_deepgram(pcm, args, jsonl_path):
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise SystemExit("DEEPGRAM_API_KEY is required for deepgram mode")
    segments = []
    sampler = Sampler("deepgram", lambda: [os.getpid()], jsonl_path, args.sample_interval)
    cpu_start = resource_cpu_seconds(resource.RUSAGE_SELF)
    started = time.time()
    client = DeepgramStreamingClient(api_key, args.language, segments.append)
    sampler.start()
    client.connect()
    data = pcm.read_bytes()
    frame_bytes = max(BYTES_PER_SAMPLE, int(args.deepgram_frame_seconds * BYTES_PER_SECOND))
    for offset in range(0, len(data), frame_bytes):
        client.send_audio(data[offset : offset + frame_bytes])
        if args.realtime:
            time.sleep(args.deepgram_frame_seconds)
    client.finalize()
    sampler.stop()
    wall = time.time() - started
    cpu = resource_cpu_seconds(resource.RUSAGE_SELF) - cpu_start
    network_mb_per_minute = (
        ((client.bytes_sent + client.bytes_received) / 1_000_000) / (audio_duration(pcm) / 60.0)
        if audio_duration(pcm)
        else None
    )
    return summarize(
        "deepgram",
        audio_duration(pcm),
        wall,
        cpu,
        sampler.samples,
        segments,
        {
            "deepgram_network_bytes_sent": client.bytes_sent,
            "deepgram_network_bytes_received": client.bytes_received,
            "deepgram_network_mb_per_audio_minute": network_mb_per_minute,
        },
    )


def split_pcm(data, chunk_seconds):
    chunk_size = int(chunk_seconds * BYTES_PER_SECOND)
    return [data[index : index + chunk_size] for index in range(0, len(data), chunk_size)]


def audio_duration(pcm):
    return pcm.stat().st_size / BYTES_PER_SECOND


def resolve_helper(args):
    if args.helper_path:
        helper = Path(args.helper_path)
    else:
        repo_root = Path(__file__).resolve().parents[2]
        helper = repo_root / "desktop" / "local-asr-helper" / "target" / "debug" / "local-asr-helper"
    if not helper.exists():
        raise SystemExit(
            f"local ASR helper does not exist: {helper}\n"
            "Build it with: cargo build --manifest-path desktop/local-asr-helper/Cargo.toml"
        )
    return helper


def write_summary(jsonl_path, summary):
    with jsonl_path.open("a") as handle:
        handle.write(json.dumps({"type": "summary", **summary}, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark local Whisper versus cloud Deepgram on the same prerecorded audio."
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--mode", choices=["baseline", "local-whisper", "deepgram", "all"], default="all")
    parser.add_argument("--output-jsonl", type=Path, default=Path("/tmp/omi-transcription-benchmark.jsonl"))
    parser.add_argument("--summary-json", type=Path, default=Path("/tmp/omi-transcription-benchmark-summary.json"))
    parser.add_argument("--language", default="en")
    parser.add_argument("--engine", choices=["mlx-whisper", "faster-whisper"], default="mlx-whisper")
    parser.add_argument("--model", choices=["tiny", "base", "small", "medium", "large_v3_turbo"], default="base")
    parser.add_argument("--helper-path")
    parser.add_argument("--local-chunk-seconds", type=float, default=15.0)
    parser.add_argument("--local-timeout", type=float, default=180.0)
    parser.add_argument("--deepgram-frame-seconds", type=float, default=0.1)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--realtime", action="store_true", help="Replay audio at real-time speed.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    load_env(repo_root)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text("")

    with tempfile.TemporaryDirectory(prefix="omi-transcription-benchmark-") as temp:
        pcm = Path(temp) / "input.s16le.pcm"
        normalize_audio(args.audio.resolve(), pcm)
        modes = ["baseline", "local-whisper", "deepgram"] if args.mode == "all" else [args.mode]
        summaries = []
        for mode in modes:
            if mode == "baseline":
                summary = run_baseline(pcm, args, args.output_jsonl)
            elif mode == "local-whisper":
                summary = run_local_whisper(pcm, args, args.output_jsonl)
            elif mode == "deepgram":
                summary = run_deepgram(pcm, args, args.output_jsonl)
            else:
                raise AssertionError(mode)
            write_summary(args.output_jsonl, summary)
            summaries.append(summary)

    result = {
        "audio": str(args.audio.resolve()),
        "created_at": time.time(),
        "modes": summaries,
    }
    args.summary_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

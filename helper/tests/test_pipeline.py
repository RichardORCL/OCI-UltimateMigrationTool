"""PipelinedDecoder: same result as the plain decoder, errors surface on the caller, no hangs."""

import threading
import time

import pytest

from helper_app.disk import vmdk_stream as vs
from helper_app.disk.pipeline import PipelinedDecoder

from .test_vmdk_stream import MemSink, make_raw


@pytest.mark.parametrize("chunk,depth", [(4096, 1), (65536 + 13, 2), (1024 * 1024, 8)])
def test_pipeline_matches_direct_decode(chunk, depth):
    raw = make_raw(3 * 1024 * 1024, seed=chunk)
    encoded = vs.encode_raw_bytes(raw)
    sink = MemSink(len(raw))
    dec = vs.StreamOptimizedDecoder(sink.write_at, expected_capacity_bytes=len(raw))
    pipe = PipelinedDecoder(dec, depth=depth)
    for i in range(0, len(encoded), chunk):
        pipe.feed(encoded[i : i + chunk])
    stats = pipe.finish()
    assert bytes(sink.buf) == raw
    assert stats.finished and stats.bytes_consumed == len(encoded)
    assert stats.grains_written == sink.writes
    pipe.abort()  # idempotent after finish
    assert pipe.finish() is stats


def test_decoder_error_surfaces_on_caller_with_original_type():
    raw = make_raw(1024 * 1024)
    encoded = bytearray(vs.encode_raw_bytes(raw))
    encoded[0:4] = b"XXXX"  # bad magic -> VmdkFormatError in the worker on the first chunk
    sink = MemSink(len(raw))
    pipe = PipelinedDecoder(vs.StreamOptimizedDecoder(sink.write_at), depth=1)
    with pytest.raises(vs.VmdkFormatError, match="magic"):
        # the producer keeps feeding (and would block on the full queue) until it sees the failure
        for i in range(0, len(encoded), 512):
            pipe.feed(bytes(encoded[i : i + 512]))
        pipe.finish()
    pipe.abort()


def test_writer_error_surfaces_from_finish():
    raw = make_raw(1024 * 1024)
    encoded = vs.encode_raw_bytes(raw)

    def failing_write(offset, data):
        raise OSError("device gone")

    pipe = PipelinedDecoder(vs.StreamOptimizedDecoder(failing_write), depth=64)
    pipe.feed(encoded)  # fits the queue; the worker fails asynchronously
    with pytest.raises(OSError, match="device gone"):
        pipe.finish()


def test_abort_stops_a_slow_worker_and_unblocks_the_producer():
    raw = make_raw(1024 * 1024)
    encoded = vs.encode_raw_bytes(raw)
    gate = threading.Event()

    def slow_write(offset, data):
        gate.wait(timeout=5)

    pipe = PipelinedDecoder(vs.StreamOptimizedDecoder(slow_write), depth=1)
    # fill the queue and the worker: the worker blocks in slow_write on the first grain
    produced = []
    outcome = []

    def produce():
        try:
            for i in range(0, len(encoded), 512):
                pipe.feed(encoded[i : i + 512])
                produced.append(i)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    t = threading.Thread(target=produce, daemon=True)
    t.start()
    time.sleep(0.2)
    assert t.is_alive() and len(produced) < len(encoded) // 512  # back-pressured
    gate.set()
    pipe.abort()
    t.join(timeout=5)
    assert not t.is_alive()
    assert outcome and "aborted" in str(outcome[0])  # the blocked producer was released with an error


def test_rejects_bad_depth():
    with pytest.raises(ValueError):
        PipelinedDecoder(vs.StreamOptimizedDecoder(lambda o, d: None), depth=0)

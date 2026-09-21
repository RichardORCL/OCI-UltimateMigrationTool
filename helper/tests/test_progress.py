from helper_app.jobs.progress import RateMeter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_meter_uses_elapsed_time_until_window_is_full():
    clock = Clock()
    m = RateMeter(window_s=60, clock=clock)
    assert m.rate() == 0.0
    clock.t += 10
    m.add(10 * 1024 * 1024)
    assert m.rate() == 1024 * 1024  # 10 MiB over the 10 s the meter has existed


def test_rate_meter_slides_over_the_last_minute_and_reflects_stalls():
    clock = Clock()
    m = RateMeter(window_s=60, clock=clock)
    for _ in range(120):  # 2 minutes at 1 MiB/s
        clock.t += 1
        m.add(1024 * 1024)
    assert abs(m.rate() - 1024 * 1024) < 1
    clock.t += 30  # nothing received for 30 s: only 30 MiB left in the window
    assert abs(m.rate() - 512 * 1024) < 1
    clock.t += 31  # window empty
    assert m.rate() == 0.0

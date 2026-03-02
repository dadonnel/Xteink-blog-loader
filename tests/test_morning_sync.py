import morning_sync


def test_offline_retry_seconds_backoff_and_cap():
    assert morning_sync.offline_retry_seconds(30, 300, 1) == 60
    assert morning_sync.offline_retry_seconds(30, 300, 2) == 120
    assert morning_sync.offline_retry_seconds(30, 300, 5) == 300


def test_host_reachable_uses_tcp_probe(monkeypatch):
    monkeypatch.setattr(morning_sync, "tcp_probe_host", lambda host, port, timeout: (host, port, timeout) == ("h", 23, 0.5))
    assert morning_sync.host_reachable("h", "tcp", 23, 0.5)


def test_host_reachable_uses_ping(monkeypatch):
    monkeypatch.setattr(morning_sync, "ping_host", lambda host: host == "h")
    assert morning_sync.host_reachable("h", "ping", 22, 1.0)

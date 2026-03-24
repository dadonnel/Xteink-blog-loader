from pathlib import Path


def test_legacy_entrypoint_binds_to_loopback_by_default():
    source = Path("web/app.py").read_text(encoding="utf-8")

    assert 'run(host="127.0.0.1", port=5002)' in source

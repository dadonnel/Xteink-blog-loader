import importlib

blog_loader = importlib.import_module("3dayblogs")


def test_main_returns_error_when_ebooklib_missing(monkeypatch, capsys):
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name == "ebooklib":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(blog_loader.importlib.util, "find_spec", fake_find_spec)

    assert blog_loader.main() == 1
    captured = capsys.readouterr()
    assert "Missing dependency: ebooklib" in captured.out
    assert "pip install ebooklib" in captured.out

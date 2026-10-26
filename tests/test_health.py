from genesis.core.health import ModelHealth


def test_recently_overloaded_models_go_last(tmp_path):
    h = ModelHealth(tmp_path / "gemini-health.json")
    assert h.order(["a", "b", "c"]) == ["a", "b", "c"]
    h.mark("a", True)
    assert h.busy("a") and h.order(["a", "b", "c"]) == ["b", "c", "a"]
    h.mark("a", False)
    assert h.order(["a", "b", "c"]) == ["a", "b", "c"]


def test_unreadable_health_file_is_ignored(tmp_path):
    p = tmp_path / "gemini-health.json"
    p.write_text("not json")
    assert ModelHealth(p).order(["x", "y"]) == ["x", "y"]

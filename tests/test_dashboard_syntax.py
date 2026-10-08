from pathlib import Path


def test_web_dashboard_source_compiles():
    dashboard = Path(__file__).resolve().parents[1] / "web_dashboard.py"
    compile(dashboard.read_text(encoding="utf-8"), str(dashboard), "exec")

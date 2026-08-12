import ast
from pathlib import Path

def test_application_never_mounts_legacy_debug_send_router() -> None:
    tree = ast.parse(Path("app/main.py").read_text(encoding="utf-8"))
    mounted = {
        ast.unparse(call.args[0])
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "include_router"
        and call.args
    }

    assert "debug.router" not in mounted


def test_production_application_exposes_no_legacy_write_side_doors() -> None:
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    performance_tree = ast.parse(
        Path("app/api/performance.py").read_text(encoding="utf-8")
    )
    performance_paths = {
        arg.value
        for node in ast.walk(performance_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
        for arg in decorator.args[:1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }

    assert "debug.router" not in main_source
    assert "/manual" not in performance_paths

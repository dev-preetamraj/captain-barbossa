"""The package's internal imports form a layered graph: no module reaches itself again."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "captain_barbossa"


def imports():
    """Each module's package imports, local ones included, keyed by module name."""
    modules = {path.stem for path in PACKAGE.glob("*.py")}
    graph = {}
    for path in PACKAGE.glob("*.py"):
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
        graph[path.stem] = (found & modules) - {path.stem}
    return graph


def cycle(graph):
    """One import cycle as a module path, or None."""
    state, trail = {}, []

    def visit(module):
        state[module] = "open"
        trail.append(module)
        for target in sorted(graph[module]):
            if state.get(target) == "open":
                return trail[trail.index(target) :] + [target]
            if target not in state and (found := visit(target)):
                return found
        state[module] = "done"
        trail.pop()
        return None

    for module in sorted(graph):
        if module not in state and (found := visit(module)):
            return found
    return None


class ModuleImportCycleTests(unittest.TestCase):
    def test_no_module_imports_itself_through_another(self):
        found = cycle(imports())
        self.assertIsNone(found, " -> ".join(found or ()))

    def test_the_check_finds_a_planted_cycle(self):
        self.assertEqual(cycle({"a": {"b"}, "b": {"a"}}), ["a", "b", "a"])


if __name__ == "__main__":
    unittest.main()

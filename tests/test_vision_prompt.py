"""Guards the Vision analysis prompt (``_ANALYSIS_INSTRUCTIONS``).

Extracts the prompt string from ``vision.py`` via ``ast`` so the module's HA-importing
top level never runs. Fix 1 removed the scenic few-shot examples the model was copying
verbatim; this test fails if any of those concrete residential nouns creep back in.
"""

import ast
import pathlib
import re

_VISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "teds_dashboard_system"
    / "vision.py"
)


def _analysis_instructions() -> str:
    tree = ast.parse(_VISION.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ANALYSIS_INSTRUCTIONS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("_ANALYSIS_INSTRUCTIONS not found in vision.py")


def test_prompt_has_no_scenic_examples():
    text = _analysis_instructions().lower()
    for word in ("driveway", "garage", "porch", "suv", "van"):
        assert not re.search(rf"\b{word}\b", text), f"scenic literal {word!r} leaked into the prompt"


def test_prompt_has_object_context_placeholder():
    # Fix 2a threads Frigate's tracked object/zones in here.
    assert "{object_context}" in _analysis_instructions()

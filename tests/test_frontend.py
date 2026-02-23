"""Run the dependency-free JavaScript regression cases as part of the Python suite."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from agentic_trading.service.shapes import (
    Health,
    MicroLive,
    Orders,
    Portfolio,
    ProposalDetail,
    ProposalSummary,
)

UI_ASSETS = Path(__file__).resolve().parents[1] / "ui" / "assets"

# Each payload the console renders with ?demo=1, and the type its endpoint is declared to return.
DEMO_SHAPES = {
    "health": Health,                         # GET /health
    "portfolio": Portfolio,                   # GET /portfolio
    "microLive": MicroLive,                   # GET /micro-live
    "orders": Orders,                         # GET /orders
    "proposals": list[ProposalSummary],       # GET /proposals
    "proposalDetail": ProposalDetail,         # GET /proposals/{proposal_id}
}


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend regression cases")
    return node


def test_frontend_regressions():
    node = _node()
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("frontend.test.cjs"))],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _demo_fixtures(node: str) -> dict:
    script = (
        "const fs = require('fs'), vm = require('vm');"
        "const window = {location: {search: '?demo=1'}};"
        "vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window, URLSearchParams});"
        "process.stdout.write(JSON.stringify(window.CT));"
    )
    result = subprocess.run([node, "-e", script, str(UI_ASSETS / "data.js")],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_demo_fixtures_are_the_shapes_the_api_returns():
    """``ui/assets/data.js`` is documented as the API's own shapes, and nothing else holds it to that.

    The demo proposal list went without ``version`` while the detail of the same proposal carried
    one, so the pair the approval screen pins a proposal to did not match in the demo.
    """
    fixtures = _demo_fixtures(_node())
    failures = {}
    for key, shape in DEMO_SHAPES.items():
        try:
            TypeAdapter(shape).validate_python(fixtures[key])
        except ValidationError as error:
            failures[key] = error.errors(include_url=False)
    assert not failures, json.dumps(failures, indent=2, default=str)

    detail = fixtures["proposalDetail"]
    listed = [p for p in fixtures["proposals"] if p["proposal_id"] == detail["proposal_id"]]
    assert [p.get("version") for p in listed] == [detail["version"]]

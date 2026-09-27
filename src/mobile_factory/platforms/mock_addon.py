"""mitmproxy addon behind `factory android mock`: answers matching requests from the run's mocks.json.

Loaded by `mitmdump -s`; never imported by the factory itself. Rules are re-read per response, so a rule added
mid-session applies at once. A rule is {"pattern": <regex on the URL path>, "file": <json>} or {..., "jq": <filter>}.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from mitmproxy import ctx, http


class FactoryMocks:
    def load(self, loader: Any) -> None:
        loader.add_option("factory_mocks", str, "", "the run's mocks.json")

    def response(self, flow: http.HTTPFlow) -> None:
        rules = Path(ctx.options.factory_mocks)
        for rule in json.loads(rules.read_text()) if rules.is_file() else []:
            if not re.search(rule["pattern"], flow.request.path) or flow.response is None:
                continue
            if "file" in rule:
                body = Path(rule["file"]).read_text()
            else:
                real = flow.response.get_text() or "null"
                body = subprocess.run(
                    ["jq", "-c", rule["jq"]], input=real, capture_output=True, text=True, check=False
                ).stdout
            flow.response.status_code = int(rule.get("status", 200))
            flow.response.headers["content-type"] = "application/json"
            flow.response.set_text(body)
            return


addons = [FactoryMocks()]

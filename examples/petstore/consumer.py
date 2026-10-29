"""A consumer written against Petstore v1, kept working on v2 by a repair plan.

Run `./demo.sh` first so the plan exists, then:

    python consumer.py
"""

import json
from pathlib import Path

from apimend import Healer

HERE = Path(__file__).parent
PLAN = HERE / ".apimend/plans/petstore/list-pets.json"


def summarize(pets: list[dict]) -> list[str]:
    # v1 code: expects `name`, integer `age`, `owner.email`, and `tag`
    return [f"{p['name']} ({p['tag']}, {p['age'] + 0} yrs) -> {p['owner']['email']}" for p in pets]


def main() -> None:
    v2_response = json.loads((HERE / "samples/v2/list-pets/page-1.json").read_text())

    try:
        summarize(v2_response)
    except (KeyError, TypeError) as e:
        print(f"Without healing, v1 code fails on v2 data: {type(e).__name__}: {e}")

    heal = Healer.from_file(PLAN)
    for line in summarize(heal(v2_response)):
        print("healed:", line)


if __name__ == "__main__":
    main()

"""Print the raw upstream vehicle payload and the normalised result.

    python -m windsor_bridge.dump

Use this once after configuring the homeassistant or saic provider to confirm
the field mapping (SoC scale, lock state, tyre units, charging status codes).
Note: with VEHICLE_PROVIDER=saic this makes one real cloud request.
"""

import asyncio
import json

from .config import Settings
from .vehicle import make_provider


async def main():
    p = make_provider(Settings.from_env())
    try:
        print("=== raw ===")
        print(json.dumps(await p.raw(), indent=2, default=str))
        print("=== normalised ===")
        print(json.dumps((await p.fetch()).to_dict(), indent=2, default=str))
    finally:
        await p.aclose()


if __name__ == "__main__":
    asyncio.run(main())

"""Vehicle telemetry providers.

* ``mock``          — synthetic data for development and the Phase 4 test checklist.
* ``homeassistant`` — reads entity states from Home Assistant's REST API
                      (e.g. populated by an MG/SAIC custom integration). Recommended.
* ``saic``          — talks to the SAIC iSMART cloud directly via
                      ``saic-ismart-client-ng``. EXPERIMENTAL for JSW MG India:
                      the endpoint/region/tenant values must be supplied and the
                      field mapping verified with ``python -m windsor_bridge.dump``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

BAR_TO_PSI = 14.5038
KPA_TO_PSI = 0.145038


@dataclass
class EVState:
    soc: int | None = None
    range_km: int | None = None
    charging: bool = False
    plugged_in: bool = False
    charge_time_min: int = 0
    locked: bool | None = None
    tire_psi: list = field(default_factory=lambda: [None, None, None, None])  # FL, FR, RL, RR
    cabin_temp_c: int | None = None
    polled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["polled_at"] = self.polled_at.isoformat()
        return d


class VehicleProvider:
    name = "base"
    #: True if a fetch reaches the car's TCU (subject to the 12V safeguard intervals)
    touches_vehicle = True

    async def fetch(self) -> EVState:  # pragma: no cover - interface
        raise NotImplementedError

    async def raw(self) -> Any:
        """Raw upstream payload, for debugging field mappings."""
        return (await self.fetch()).to_dict()

    async def aclose(self) -> None:
        pass


# --------------------------------------------------------------------------- mock
class MockProvider(VehicleProvider):
    name = "mock"
    touches_vehicle = False

    def __init__(self, s: Settings):
        self.scenario = s.mock_scenario

    async def fetch(self) -> EVState:
        now = time.time()
        sc = self.scenario
        if sc == "auto":
            # 3-hour cycle: 60 min charging, then parked & slowly draining
            phase = (now % 10800) / 10800
            sc = "charging" if phase < 1 / 3 else "parked"
        st = EVState(
            soc=78, range_km=342, charging=False, plugged_in=False, charge_time_min=0,
            locked=True, tire_psi=[33, 33, 32, 33], cabin_temp_c=27,
        )
        if sc == "charging":
            st.soc = 40 + int((now / 60) % 50)
            st.range_km = int(st.soc * 4.4)
            st.charging = st.plugged_in = True
            st.charge_time_min = max(5, (90 - st.soc) * 2)
        elif sc == "unlocked":
            st.locked = False
        elif sc == "lowbatt":
            st.soc, st.range_km = 14, 58
        elif sc == "lowtyre":
            st.tire_psi = [33, 28, 32, 33]
        else:
            st.cabin_temp_c = 24 + int(3 * math.sin(now / 3600))
        return st


# ------------------------------------------------------------------ home assistant
def _num(st: dict | None) -> float | None:
    if not st:
        return None
    try:
        return float(st["state"])
    except (TypeError, ValueError, KeyError):
        return None


def _unit(st: dict | None) -> str:
    return ((st or {}).get("attributes") or {}).get("unit_of_measurement", "") or ""


def _truthy(st: dict | None) -> bool | None:
    if not st:
        return None
    v = str(st.get("state", "")).strip().lower()
    if v in ("unknown", "unavailable", ""):
        return None
    if v in ("on", "true", "yes", "1", "charging", "connected", "plugged", "plugged_in"):
        return True
    if "not" in v or "dis" in v or v in ("off", "false", "no", "0", "idle", "unplugged"):
        return False
    return "charg" in v  # e.g. "fast_charging"


def _locked(st: dict | None) -> bool | None:
    if not st:
        return None
    eid = st.get("entity_id", "")
    v = str(st.get("state", "")).lower()
    if eid.startswith("lock."):
        return {"locked": True, "unlocked": False, "open": False}.get(v)
    if eid.startswith("binary_sensor."):
        # HA 'lock' device class: on == unlocked
        return {"off": True, "on": False}.get(v)
    if v in ("locked", "true", "1", "on"):
        return True
    if v in ("unlocked", "false", "0", "off"):
        return False
    return None


def _psi(st: dict | None) -> int | None:
    v = _num(st)
    if v is None:
        return None
    u = _unit(st).lower()
    if u == "bar":
        v *= BAR_TO_PSI
    elif u == "kpa":
        v *= KPA_TO_PSI
    return round(v)


def _ts(st: dict | None) -> datetime | None:
    if not st:
        return None
    raw = st.get("last_reported") or st.get("last_updated")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


class HomeAssistantProvider(VehicleProvider):
    name = "homeassistant"
    touches_vehicle = False  # HA's integration owns the cloud polling cadence

    def __init__(self, s: Settings):
        if not s.ha_token:
            raise ValueError("HA_TOKEN is required for VEHICLE_PROVIDER=homeassistant")
        if not s.ha_entities.get("soc"):
            raise ValueError("HA_ENTITY_SOC is required for VEHICLE_PROVIDER=homeassistant")
        self.entities = {k: v for k, v in s.ha_entities.items() if v}
        self.client = httpx.AsyncClient(
            base_url=s.ha_url,
            headers={"Authorization": f"Bearer {s.ha_token}"},
            timeout=10,
        )

    async def _get(self, eid: str) -> dict | None:
        r = await self.client.get(f"/api/states/{eid}")
        if r.status_code == 404:
            log.warning("HA entity not found: %s", eid)
            return None
        r.raise_for_status()
        return r.json()

    async def raw(self) -> dict:
        res = await asyncio.gather(*(self._get(e) for e in self.entities.values()))
        return dict(zip(self.entities.keys(), res))

    async def fetch(self) -> EVState:
        m = await self.raw()
        if m.get("soc") is None or _num(m.get("soc")) is None:
            raise RuntimeError("SoC entity unavailable in Home Assistant")
        rng = _num(m.get("range"))
        if rng is not None and _unit(m.get("range")).lower() in ("mi", "miles"):
            rng *= 1.60934
        temp = _num(m.get("cabin_temp"))
        if temp is not None and "f" in _unit(m.get("cabin_temp")).lower():
            temp = (temp - 32) * 5 / 9
        charging = bool(_truthy(m.get("charging")))
        plugged = _truthy(m.get("plugged_in"))
        ct = _num(m.get("charge_time"))
        if ct is not None and _unit(m.get("charge_time")).lower() in ("h", "hr", "hours"):
            ct *= 60
        return EVState(
            soc=round(_num(m["soc"])),
            range_km=round(rng) if rng is not None else None,
            charging=charging,
            plugged_in=charging if plugged is None else plugged,
            charge_time_min=int(ct) if (ct and charging) else 0,
            locked=_locked(m.get("lock")),
            tire_psi=[_psi(m.get(k)) for k in ("tyre_fl", "tyre_fr", "tyre_rl", "tyre_rr")],
            cabin_temp_c=round(temp) if temp is not None else None,
            polled_at=_ts(m.get("soc")) or datetime.now(timezone.utc),
        )

    async def aclose(self) -> None:
        await self.client.aclose()


# ------------------------------------------------------------------------- saic
def _g(obj: Any, *path: str) -> Any:
    for p in path:
        if obj is None:
            return None
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return obj


def _valid(v: Any, bad: tuple = (-128, 255, 1023, 2047, 65535)) -> Any:
    return None if v is None or v in bad else v


class SaicProvider(VehicleProvider):
    """Direct SAIC iSMART cloud access (EXPERIMENTAL for India)."""

    name = "saic"
    touches_vehicle = True

    def __init__(self, s: Settings):
        try:
            from saic_ismart_client_ng import SaicApi  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install saic-ismart-client-ng to use VEHICLE_PROVIDER=saic") from e
        if not (s.mg_user and s.mg_pass):
            raise ValueError("MG_USER and MG_PASS are required for VEHICLE_PROVIDER=saic")
        self.s = s
        self.api = None
        self.vin = s.mg_vin or None

    async def _login(self):
        from saic_ismart_client_ng import SaicApi
        from saic_ismart_client_ng.model import SaicApiConfiguration

        kw: dict[str, Any] = dict(
            username=self.s.mg_user,
            password=self.s.mg_pass,
            username_is_email="@" in self.s.mg_user,
        )
        if "@" not in self.s.mg_user:
            kw["phone_country_code"] = self.s.saic_phone_country_code
        if self.s.saic_base_uri:
            kw["base_uri"] = self.s.saic_base_uri
        if self.s.saic_region:
            kw["region"] = self.s.saic_region
        if self.s.saic_tenant_id:
            kw["tenant_id"] = self.s.saic_tenant_id
        api = SaicApi(configuration=SaicApiConfiguration(**kw))
        await api.login()
        if not self.vin:
            vl = await api.vehicle_list()
            vins = [v.vin for v in (getattr(vl, "vinList", None) or [])]
            if not vins:
                raise RuntimeError("No vehicles visible to this MG account — was access shared?")
            self.vin = vins[0]
        self.api = api
        log.info("SAIC login OK, VIN …%s", self.vin[-4:])

    async def _call(self, fn_name: str):
        for attempt in (1, 2):
            if self.api is None:
                await self._login()
            try:
                return await getattr(self.api, fn_name)(self.vin)
            except Exception:
                if attempt == 2:
                    raise
                log.warning("SAIC %s failed, re-authenticating", fn_name, exc_info=True)
                self.api = None

    async def raw(self) -> dict:
        status = await self._call("get_vehicle_status")
        charge = await self._call("get_vehicle_charging_management_data")
        conv = lambda o: o if isinstance(o, (dict, list, int, float, str, type(None))) else getattr(o, "__dict__", repr(o))
        import json

        return json.loads(json.dumps({"status": status, "charge": charge}, default=conv))

    async def fetch(self) -> EVState:
        status = await self._call("get_vehicle_status")
        charge = await self._call("get_vehicle_charging_management_data")
        bvs = _g(status, "basicVehicleStatus")
        cm = _g(charge, "chrgMgmtData")
        rcs = _g(charge, "rvsChargeStatus")

        soc_raw = _valid(_g(cm, "bmsPackSOCDsp"))
        soc = round(soc_raw / 10) if soc_raw is not None else _valid(_g(bvs, "extendedData1"))
        rng_raw = _valid(_g(bvs, "fuelRangeElec"))
        bms = _g(cm, "bmsChrgSts")
        charging = bms in (1, 3, 10, 12)  # verify against your car with the dump tool
        gun = _g(rcs, "chargingGunState")
        rem = _valid(_g(cm, "chrgngRmnngTime"))
        tyres = []
        for k in ("frontLeftTyrePressure", "frontRightTyrePressure", "rearLeftTyrePressure", "rearRightTyrePressure"):
            v = _valid(_g(bvs, k))
            tyres.append(round(v * 0.04 * BAR_TO_PSI) if v else None)
        lock = _g(bvs, "lockStatus")
        temp = _valid(_g(bvs, "interiorTemperature"))
        return EVState(
            soc=soc,
            range_km=round(rng_raw / 10) if rng_raw is not None else None,
            charging=charging,
            plugged_in=bool(gun) or charging,
            charge_time_min=int(rem) if (charging and rem) else 0,
            locked=None if lock is None else lock == 1,
            tire_psi=tyres,
            cabin_temp_c=temp,
        )


def make_provider(s: Settings) -> VehicleProvider:
    p = s.vehicle_provider
    if p == "mock":
        return MockProvider(s)
    if p in ("homeassistant", "ha"):
        return HomeAssistantProvider(s)
    if p == "saic":
        return SaicProvider(s)
    raise ValueError(f"Unknown VEHICLE_PROVIDER={p!r}")

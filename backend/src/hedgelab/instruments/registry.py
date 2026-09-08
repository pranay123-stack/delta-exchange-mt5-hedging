"""Instrument and hedge-mapping registry.

Specifications are loaded from YAML at startup and may be overlaid by database
records (so the dashboard's Configuration page can edit them at runtime).  The
registry is the only component that knows where specs come from; every other
module receives an ``InstrumentSpec`` and asks it questions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..domain.enums import HedgeObjective
from ..domain.instrument import InstrumentSpec
from ..domain.numeric import dec
from ..logging_setup import get_logger

log = get_logger(__name__)


class InstrumentNotFound(KeyError):
    def __init__(self, key: str, available: Iterable[str] = ()) -> None:
        options = ", ".join(sorted(available))
        super().__init__(f"unknown instrument {key!r}" + (f"; known: {options}" if options else ""))
        self.key = key


class MappingNotFound(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class HedgeMapping:
    """A configured source -> hedge instrument pair."""

    name: str
    source_key: str
    hedge_key: str
    objective: HedgeObjective = HedgeObjective.QUOTE_PNL_NEUTRAL
    target_ratio: Decimal = Decimal(1)
    tolerance_bps: Decimal = Decimal(25)
    max_notional: Decimal | None = None
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.source_key}->{self.hedge_key}"


@dataclass
class InstrumentRegistry:
    """In-memory catalogue keyed by ``VENUE:SYMBOL``."""

    _instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    _mappings: dict[str, HedgeMapping] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    @classmethod
    def from_directory(cls, directory: Path) -> InstrumentRegistry:
        registry = cls()
        registry.load_directory(directory)
        return registry

    def load_directory(self, directory: Path) -> None:
        """Load every YAML file in ``directory``.

        Two passes: all instruments first, then all mappings.  Mappings
        reference instruments by key, and file order is alphabetical -- a
        single pass would fail whenever a mapping file sorts before the
        catalogue file that defines its instruments.
        """
        if not directory.exists():
            raise FileNotFoundError(f"instrument spec directory not found: {directory}")
        documents = [
            (path, yaml.safe_load(path.read_text()) or {})
            for path in sorted(directory.glob("*.yaml"))
        ]
        for path, raw in documents:
            for entry in raw.get("instruments", []) or []:
                self.register(self._parse_instrument(entry, source=str(path)))
        for path, raw in documents:
            for entry in raw.get("mappings", []) or []:
                self.register_mapping(self._parse_mapping(entry, source=str(path)))
        log.info(
            "instrument registry loaded",
            extra={
                "instrument_count": len(self._instruments),
                "mapping_count": len(self._mappings),
                "directory": str(directory),
            },
        )

    def load_file(self, path: Path) -> None:
        """Load one file.  Its mappings may only reference already-known
        instruments -- use :meth:`load_directory` for cross-file references."""
        raw = yaml.safe_load(path.read_text()) or {}
        for entry in raw.get("instruments", []) or []:
            self.register(self._parse_instrument(entry, source=str(path)))
        for entry in raw.get("mappings", []) or []:
            self.register_mapping(self._parse_mapping(entry, source=str(path)))

    @staticmethod
    def _parse_instrument(entry: dict[str, Any], source: str) -> InstrumentSpec:
        try:
            return InstrumentSpec.model_validate(entry)
        except Exception as exc:
            symbol = entry.get("symbol", "<unknown>")
            raise ValueError(f"invalid instrument {symbol!r} in {source}: {exc}") from exc

    @staticmethod
    def _parse_mapping(entry: dict[str, Any], source: str) -> HedgeMapping:
        try:
            max_notional = entry.get("max_notional")
            return HedgeMapping(
                name=entry["name"],
                source_key=entry["source"],
                hedge_key=entry["hedge"],
                objective=HedgeObjective(entry.get("objective", "QUOTE_PNL_NEUTRAL")),
                target_ratio=dec(entry.get("target_ratio", 1)),
                tolerance_bps=dec(entry.get("tolerance_bps", 25)),
                max_notional=dec(max_notional) if max_notional is not None else None,
                enabled=bool(entry.get("enabled", True)),
            )
        except Exception as exc:
            raise ValueError(f"invalid mapping in {source}: {exc}") from exc

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, spec: InstrumentSpec) -> InstrumentSpec:
        """Add or replace an instrument.  Replacement is how runtime edits work."""
        self._instruments[spec.key] = spec
        return spec

    def register_many(self, specs: Iterable[InstrumentSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def register_mapping(self, mapping: HedgeMapping) -> HedgeMapping:
        for key in (mapping.source_key, mapping.hedge_key):
            if key not in self._instruments:
                raise InstrumentNotFound(key, self._instruments)
        if self._instruments[mapping.source_key].venue == self._instruments[mapping.hedge_key].venue:
            raise ValueError(
                f"mapping {mapping.name!r}: source and hedge are on the same venue "
                f"({self._instruments[mapping.source_key].venue}); a hedge must cross venues"
            )
        if mapping.enabled:
            # Two enabled mappings sharing a leg would make position
            # attribution ambiguous: one venue position cannot belong to two
            # pairs, and every exposure/risk number would double-count it.
            for existing in self._mappings.values():
                if not existing.enabled or existing.key == mapping.key:
                    continue
                shared = {existing.source_key, existing.hedge_key} & {
                    mapping.source_key, mapping.hedge_key
                }
                if shared:
                    raise ValueError(
                        f"mapping {mapping.name!r} shares instrument(s) "
                        f"{sorted(shared)} with the enabled mapping {existing.name!r}; "
                        f"disable one or give it a distinct instrument"
                    )
        self._mappings[mapping.key] = mapping
        return mapping

    def remove(self, key: str) -> None:
        self._instruments.pop(key, None)

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------
    def get(self, key: str) -> InstrumentSpec:
        try:
            return self._instruments[key]
        except KeyError:
            raise InstrumentNotFound(key, self._instruments) from None

    def find(self, key: str) -> InstrumentSpec | None:
        return self._instruments.get(key)

    def get_by_venue_symbol(self, venue: str, symbol: str) -> InstrumentSpec:
        return self.get(f"{venue}:{symbol}")

    def all(self, *, active_only: bool = False) -> list[InstrumentSpec]:
        specs = list(self._instruments.values())
        if active_only:
            specs = [s for s in specs if s.active]
        return sorted(specs, key=lambda s: s.key)

    def by_venue(self, venue: str) -> list[InstrumentSpec]:
        return [s for s in self.all() if s.venue == venue]

    def venues(self) -> list[str]:
        return sorted({s.venue for s in self._instruments.values()})

    def by_underlying(self, underlying: str) -> list[InstrumentSpec]:
        return [s for s in self.all() if s.effective_underlying_key == underlying]

    def hedge_candidates(self, source_key: str) -> list[InstrumentSpec]:
        """Instruments on another venue that track the same underlying."""
        source = self.get(source_key)
        return [
            spec
            for spec in self.all(active_only=True)
            if spec.venue != source.venue
            and spec.effective_underlying_key == source.effective_underlying_key
        ]

    def mappings(self, *, enabled_only: bool = False) -> list[HedgeMapping]:
        items = list(self._mappings.values())
        if enabled_only:
            items = [m for m in items if m.enabled]
        return sorted(items, key=lambda m: m.name)

    def mapping(self, key: str) -> HedgeMapping:
        try:
            return self._mappings[key]
        except KeyError:
            raise MappingNotFound(f"unknown hedge mapping {key!r}") from None

    def mapping_by_name(self, name: str) -> HedgeMapping:
        for mapping in self._mappings.values():
            if mapping.name == name:
                return mapping
        raise MappingNotFound(f"unknown hedge mapping named {name!r}")

    def __len__(self) -> int:
        return len(self._instruments)

    def __contains__(self, key: object) -> bool:
        return key in self._instruments

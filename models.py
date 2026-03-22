from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


class POIType(Enum):
    ORDER_BLOCK = "OB"
    FAIR_VALUE_GAP = "FVG"
    LIQUIDITY_POOL = "LIQ"


class Direction(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class StructureType(Enum):
    BOS = "BOS"
    CHOCH = "CHoCH"


@dataclass
class SwingPoint:
    bar_index: int
    price: float
    is_high: bool
    is_broken: bool = False
    time: int = 0


@dataclass
class StructureBreak:
    bar_index: int
    price: float
    direction: Direction
    break_type: StructureType
    time: int = 0


@dataclass
class LiquidityPool:
    price: float
    first_bar: int
    last_bar: int
    touch_count: int = 1
    is_high_side: bool = True
    is_swept: bool = False
    sweep_bar: Optional[int] = None
    first_time: int = 0
    last_time: int = 0


@dataclass
class FairValueGap:
    bar_index: int
    top: float
    bottom: float
    is_bullish: bool
    is_active: bool = True
    post_sweep: bool = False
    retrace_triggered: bool = False
    time: int = 0
    fill_bar: Optional[int] = None  # bar where FVG was filled (for snapshot)


@dataclass
class OrderBlock:
    start_bar: int
    top: float
    bottom: float
    is_bullish: bool
    is_active: bool = True
    post_sweep: bool = False
    retrace_triggered: bool = False
    bos_bar: Optional[int] = None
    caused_choch: bool = False
    time: int = 0
    invalidation_bar: Optional[int] = None  # bar where OB was invalidated (for snapshot)


@dataclass
class POI:
    """A Point of Interest with its probability score."""
    poi_type: POIType
    direction: Direction
    top: float
    bottom: float
    bar_index: int
    post_sweep: bool = False
    hold_probability: float = 0.0
    break_probability: float = 0.0
    prior: float = 0.5
    posterior: float = 0.5
    fpt_probability: float = 0.5
    time: int = 0
    touch_count: int = 1
    caused_choch: bool = False
    retrace_triggered: bool = False


@dataclass
class Signal:
    direction: Direction
    poi: POI
    probability: float
    bar_index: int
    price: float
    time: int = 0

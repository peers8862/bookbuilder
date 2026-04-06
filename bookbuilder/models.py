from dataclasses import dataclass

@dataclass
class RawItem:
    id: int
    name: str
    value: float

@dataclass
class AnalyzedItem:
    id: int
    raw_item_id: int
    analysis_result: str

@dataclass
class Cluster:
    cluster_id: int
    items: list[AnalyzedItem]
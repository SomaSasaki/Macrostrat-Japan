# -*- coding: utf-8 -*-
"""
scripts.v2_200k.models — GSJ 200k v2_polygon_column データ契約および型定義 (WP0)

設計原則:
- Actual geometry: 実ポリゴンのUnion Footprintを保持
- Evidence-first: すべての層序・接触関係にEvidenceRecordを紐付け
- Partial-order DAG: 循環ゼロ・証拠付き有向非巡回グラフ
- Fail-closed: 根拠不足・競合時は REVIEW_REQUIRED / INVENTORY_ONLY で安全停止
- Human-auditable: レビュー判断を ReviewDecision として永続化
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Optional, Any, Set, Tuple
import json
import hashlib

class ColumnStatus(str, Enum):
    SOURCE_READY = "SOURCE_READY"
    INVENTORY_READY = "INVENTORY_READY"
    TOPOLOGY_CANDIDATE = "TOPOLOGY_CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INSUFFICIENT_SOURCE = "INSUFFICIENT_SOURCE"
    INVENTORY_ONLY = "INVENTORY_ONLY"
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    EXPORTABLE = "EXPORTABLE"
    ACCEPTED = "ACCEPTED"

class ColumnKind(str, Enum):
    SEDIMENTARY_SUCCESSION = "sedimentary_succession"
    VOLCANIC_ARC = "volcanic_arc"
    ACCRETIONARY_COMPLEX = "accretionary_complex"
    METAMORPHIC_BELT = "metamorphic_belt"
    PLUTONIC_COMPLEX = "plutonic_complex"
    QUATERNARY_COVER = "quaternary_cover"
    MARINE_SUCCESSION = "marine_succession"
    UNCLASSIFIED = "unclassified"

class RelationType(str, Enum):
    STRATIGRAPHIC_OVER = "stratigraphic_over"      # 下位から上位へ積み重なる関係
    INTRUSIVE_INTO = "intrusive_into"              # 貫入関係
    FAULT_CONTACT = "fault_contact"                # 断層接触
    COVER_OVER_BASEMENT = "cover_over_basement"    # 基盤と被覆
    LATERAL_INTERFINGERING = "lateral_interfingering" # 側方変化・指交
    UNKNOWN_CONTACT = "unknown_contact"            # 不明な接触

class BasalSurfaceType(str, Enum):
    CONFORMABLE = "conformable"
    UNCONFORMITY = "unconformity"
    ANGULAR_UNCONFORMITY = "angular unconformity"
    DISCONFORMITY = "disconformity"
    PARACONFORMITY = "paraconformity"
    NONCONFORMITY = "nonconformity"
    INTRUSIVE = "intrusive"
    FAULTED = "faulted"
    UNKNOWN = "unknown"

@dataclass
class EvidenceRecord:
    """
    判断の根拠（原典、ページ、図版、引用、信頼度）
    """
    evidence_id: str
    source_type: str                   # 'gsj_shapefile', 'map_sheet_pdf', 'cross_section', 'chart', 'paper'
    source_id: str                     # PDF URL, File SHA-256, Map sheet code
    page_number: Optional[int] = None
    figure_id: Optional[str] = None
    quote: Optional[str] = None
    bbox_in_page: Optional[List[float]] = None # [ymin, xmin, ymax, xmax] normalized
    confidence: float = 1.0            # 0.0 - 1.0
    extractor: str = "deterministic_rule" # 'deterministic_rule', 'ocr_text', 'human_expert'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class MapUnitEntity:
    """
    地質単元の実体台帳（凡例シンボルと正規化属性）
    """
    unit_id: str                       # e.g., 'm200k_NI_53_14_u001'
    symbol: str                        # GSJ seamless legend symbol (e.g., 'Q3_al')
    name_ja: str                       # e.g., '完新世 沖積層'
    name_en: str                       # e.g., 'Holocene Alluvium'
    b_int: str                         # Macrostrat interval name
    t_int: str                         # Macrostrat interval name
    b_age_ma: float                    # Bottom age in Ma
    t_age_ma: float                    # Top age in Ma
    lithology: str                     # Macrostrat primary lithology (vocab.json 214)
    minor_lith: Optional[str] = None   # Macrostrat minor lithology
    environment: str = "unknown"       # Macrostrat environment
    group_ja: str = ""
    group_en: str = ""
    description_ja: str = ""
    description_en: str = ""
    source_symbol_level: str = "basic" # 'basic', 'detailed'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class PolygonOccurrence:
    """
    図幅内における実ポリゴンの出現データ（幾何形状・面積・重心・ドメイン所属）
    """
    occurrence_id: str                 # e.g., 'NI_53_14_poly_1042'
    unit_id: str                       # MapUnitEntity.unit_id
    sheet_code: str                    # e.g., 'NI-53-14'
    geometry_wkt: str                  # WGS84 EPSG:4326 Polygon/MultiPolygon WKT
    area_sq_km: float                  # 平方キロメートル
    centroid: Tuple[float, float]      # (lat, lng)
    domain_id: Optional[str] = None    # GeologicDomain.domain_id
    is_major_occurrence: bool = True   # 微小ポリゴンノイズの判定

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class TopologyEdge:
    """
    Column内の地層間・地質体間の関係エッジ（DAGの有向辺）
    """
    edge_id: str                       # e.g., 'edge_u001_to_u002'
    from_unit_id: str                  # 下位・古い・貫入される側
    to_unit_id: str                    # 上位・新しい・貫入する側
    relation_type: RelationType
    basal_surface: BasalSurfaceType = BasalSurfaceType.UNKNOWN
    confidence: float = 1.0
    evidence_ids: List[str] = field(default_factory=list)
    conflict_state: str = "resolved"   # 'resolved', 'hard_conflict', 'soft_conflict'

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['relation_type'] = self.relation_type.value
        d['basal_surface'] = self.basal_surface.value
        return d

@dataclass
class GeologicDomain:
    """
    地質ドメイン（テクトニック単元・堆積盆・火山域・深成岩体・被覆層）
    """
    domain_id: str                     # e.g., 'NI_53_14_dom_accretionary_tamba'
    sheet_code: str                    # e.g., 'NI-53-14'
    domain_name: str                   # e.g., 'Tamba Accretionary Complex'
    column_kind: ColumnKind
    footprint_wkt: str                 # 所属実ポリゴンのUnion MultiPolygon WKT
    representative_point: Tuple[float, float] # (lat, lng)
    total_area_sq_km: float            # ドメイン総面積
    unit_ids: List[str] = field(default_factory=list)
    occurrence_ids: List[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['column_kind'] = self.column_kind.value
        return d

@dataclass
class ReviewDecision:
    """
    人手による修正・承認ログ（不変ログとして保持し、再生成時にも反映）
    """
    decision_id: str
    sheet_code: str
    target_type: str                   # 'column', 'unit', 'edge', 'domain'
    target_id: str
    action: str                        # 'override_order', 'split_domain', 'change_contact', 'approve'
    original_value: Any
    reviewed_value: Any
    reviewer: str                      # 'expert_soma', 'geologist'
    timestamp: str                     # ISO 8601
    rationale: str                     # 判断理由・根拠文献

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class ColumnGraph:
    """
    1本の地質Columnのトポロジーグラフ（実形状・単元群・DAG順序・根拠・ステータス）
    """
    col_id: str                        # e.g., 'col_1'
    col_group: str                     # e.g., 'GSJ_200K_NI_53_14_Tamba_Accretionary'
    col_name: str                      # e.g., 'Kyoto-Osaka (Tamba Accretionary Complex) Column'
    sheet_code: str                    # e.g., 'NI-53-14'
    domain_id: str                     # GeologicDomain.domain_id
    column_kind: ColumnKind
    footprint_wkt: str                 # 実ポリゴンUnionのWKT
    representative_point: Tuple[float, float]
    b_int: str
    t_int: str
    b_age_ma: float
    t_age_ma: float
    status: ColumnStatus = ColumnStatus.INVENTORY_READY
    units: List[MapUnitEntity] = field(default_factory=list)
    edges: List[TopologyEdge] = field(default_factory=list)
    evidence_records: List[EvidenceRecord] = field(default_factory=list)
    decision_logs: List[ReviewDecision] = field(default_factory=list)
    comments: str = ""

    def validate_dag(self) -> Tuple[bool, List[str]]:
        """
        DAGの非循環性（Cycle 0件）を検証
        """
        adj: Dict[str, List[str]] = {}
        for u in self.units:
            adj[u.unit_id] = []
        for e in self.edges:
            if e.from_unit_id in adj and e.to_unit_id in adj:
                adj[e.from_unit_id].append(e.to_unit_id)

        visited: Dict[str, int] = {u.unit_id: 0 for u in self.units} # 0: unvisited, 1: visiting, 2: visited
        cycles: List[str] = []

        def dfs(node: str, path: List[str]):
            visited[node] = 1
            path.append(node)
            for neighbor in adj.get(node, []):
                if visited[neighbor] == 1:
                    cycle_path = " -> ".join(path[path.index(neighbor):] + [neighbor])
                    cycles.append(f"Cycle detected: {cycle_path}")
                elif visited[neighbor] == 0:
                    dfs(neighbor, path)
            path.pop()
            visited[node] = 2

        for u in self.units:
            if visited[u.unit_id] == 0:
                dfs(u.unit_id, [])

        return len(cycles) == 0, cycles

    def to_dict(self) -> Dict[str, Any]:
        return {
            'col_id': self.col_id,
            'col_group': self.col_group,
            'col_name': self.col_name,
            'sheet_code': self.sheet_code,
            'domain_id': self.domain_id,
            'column_kind': self.column_kind.value,
            'footprint_wkt': self.footprint_wkt,
            'representative_point': list(self.representative_point),
            'b_int': self.b_int,
            't_int': self.t_int,
            'b_age_ma': self.b_age_ma,
            't_age_ma': self.t_age_ma,
            'status': self.status.value,
            'units': [u.to_dict() for u in self.units],
            'edges': [e.to_dict() for e in self.edges],
            'evidence_records': [ev.to_dict() for ev in self.evidence_records],
            'decision_logs': [d.to_dict() for d in self.decision_logs],
            'comments': self.comments,
        }

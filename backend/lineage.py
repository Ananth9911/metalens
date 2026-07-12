"""
lineage.py — the graph engine at the core of MetaLens.

This module is deliberately dependency-free (pure Python, standard library only).
Everything the catalog does — upstream/downstream lineage, impact analysis,
build ordering, cycle detection — is a graph algorithm over a directed graph
whose nodes are datasets (tables) and whose edges are "feeds" relationships
(source table -> derived table).

Interview talking points live here:
  - Adjacency-list representation, O(V + E) traversals.
  - BFS for shortest-hop lineage, DFS for full transitive closure.
  - Kahn's algorithm for topological (build) order + cycle detection.
  - Column-level lineage layered on top of table-level edges.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Column:
    name: str
    type: str
    description: str = ""
    # column-level lineage: which upstream (table.column) values feed this one
    derived_from: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    id: str                      # unique key, e.g. "analytics.daily_active_users"
    name: str                    # human name
    layer: str                   # raw | staging | mart | report  (governance layer)
    owner: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    # table-level edges: dataset ids this dataset is built FROM (its parents/sources)
    sources: list[str] = field(default_factory=list)


class LineageGraph:
    """
    Directed graph:  source_dataset  --feeds-->  derived_dataset

    We store both directions as adjacency lists so upstream and downstream
    traversals are each O(V + E) without rebuilding anything.
    """

    def __init__(self) -> None:
        self.datasets: dict[str, Dataset] = {}
        self._downstream: dict[str, set[str]] = defaultdict(set)  # id -> ids it feeds
        self._upstream: dict[str, set[str]] = defaultdict(set)    # id -> ids that feed it

    # ---- construction -------------------------------------------------------

    def add_dataset(self, ds: Dataset) -> None:
        self.datasets[ds.id] = ds

    def build_edges(self) -> None:
        """Materialise edges from each dataset's declared `sources`."""
        self._downstream.clear()
        self._upstream.clear()
        for ds in self.datasets.values():
            for src in ds.sources:
                if src not in self.datasets:
                    # dangling source — surface it rather than silently dropping
                    raise ValueError(
                        f"Dataset '{ds.id}' lists unknown source '{src}'"
                    )
                self._downstream[src].add(ds.id)
                self._upstream[ds.id].add(src)

    # ---- basic accessors ----------------------------------------------------

    def get(self, dataset_id: str) -> Optional[Dataset]:
        return self.datasets.get(dataset_id)

    def all_datasets(self) -> list[Dataset]:
        return list(self.datasets.values())

    def direct_sources(self, dataset_id: str) -> list[str]:
        return sorted(self._upstream.get(dataset_id, set()))

    def direct_targets(self, dataset_id: str) -> list[str]:
        return sorted(self._downstream.get(dataset_id, set()))

    # ---- lineage traversals -------------------------------------------------

    def upstream(self, dataset_id: str) -> list[str]:
        """
        Full transitive set of ancestors (everything this dataset ultimately
        depends on). DFS over the upstream adjacency list. O(V + E).
        """
        return self._traverse(dataset_id, self._upstream)

    def downstream(self, dataset_id: str) -> list[str]:
        """
        Full transitive set of descendants (everything that would be affected
        if this dataset changed). DFS over the downstream adjacency list.
        This is the core of impact analysis.
        """
        return self._traverse(dataset_id, self._downstream)

    def _traverse(self, start: str, adj: dict[str, set[str]]) -> list[str]:
        if start not in self.datasets:
            raise KeyError(f"Unknown dataset '{start}'")
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in adj.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        seen.discard(start)
        return sorted(seen)

    def shortest_path(self, src: str, dst: str) -> Optional[list[str]]:
        """
        Shortest lineage path (fewest hops) from src to dst, following the
        direction of data flow. BFS. Returns the node sequence or None.
        """
        if src not in self.datasets or dst not in self.datasets:
            raise KeyError("Unknown dataset in path query")
        if src == dst:
            return [src]
        prev: dict[str, str] = {src: src}
        q = deque([src])
        while q:
            node = q.popleft()
            for nxt in self._downstream.get(node, ()):
                if nxt not in prev:
                    prev[nxt] = node
                    if nxt == dst:
                        return self._reconstruct(prev, src, dst)
                    q.append(nxt)
        return None

    @staticmethod
    def _reconstruct(prev: dict[str, str], src: str, dst: str) -> list[str]:
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    # ---- impact analysis ----------------------------------------------------

    def impact_of(self, dataset_id: str) -> dict:
        """
        "What breaks if this changes?" — the headline feature.
        Returns downstream datasets grouped by governance layer, so an owner
        can see blast radius at a glance.
        """
        affected = self.downstream(dataset_id)
        by_layer: dict[str, list[str]] = defaultdict(list)
        for aid in affected:
            by_layer[self.datasets[aid].layer].append(aid)
        return {
            "dataset": dataset_id,
            "total_affected": len(affected),
            "affected": affected,
            "by_layer": {k: sorted(v) for k, v in by_layer.items()},
            "direct_targets": self.direct_targets(dataset_id),
        }

    # ---- build order + cycle detection --------------------------------------

    def topological_order(self) -> list[str]:
        """
        Kahn's algorithm. Returns a valid build order (sources before the
        datasets derived from them). Raises if the graph has a cycle — which,
        for a data pipeline, is a real defect worth surfacing loudly.
        """
        indeg: dict[str, int] = {ds_id: 0 for ds_id in self.datasets}
        for ds_id in self.datasets:
            for tgt in self._downstream.get(ds_id, ()):
                indeg[tgt] += 1

        q = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: list[str] = []
        while q:
            node = q.popleft()
            order.append(node)
            for tgt in sorted(self._downstream.get(node, ())):
                indeg[tgt] -= 1
                if indeg[tgt] == 0:
                    q.append(tgt)

        if len(order) != len(self.datasets):
            cycle = self.find_cycle()
            raise ValueError(f"Pipeline has a cycle: {' -> '.join(cycle)}")
        return order

    def find_cycle(self) -> list[str]:
        """DFS with a recursion stack to recover one concrete cycle for the
        error message. Returns [] if the graph is acyclic."""
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self.datasets}
        parent: dict[str, Optional[str]] = {n: None for n in self.datasets}

        def dfs(u: str) -> Optional[list[str]]:
            color[u] = GREY
            for v in self._downstream.get(u, ()):
                if color[v] == WHITE:
                    parent[v] = u
                    res = dfs(v)
                    if res:
                        return res
                elif color[v] == GREY:
                    # back edge -> reconstruct the cycle v ... u v
                    cyc = [v, u]
                    while cyc[-1] != v:
                        cyc.append(parent[cyc[-1]])
                    cyc.reverse()
                    return cyc
            color[u] = BLACK
            return None

        for node in self.datasets:
            if color[node] == WHITE:
                found = dfs(node)
                if found:
                    return found
        return []

    # ---- column-level lineage ----------------------------------------------

    def column_lineage(self, dataset_id: str, column_name: str) -> dict:
        """
        Trace a single column back to its ultimate raw sources by walking the
        `derived_from` links recorded on each column. This is what lets an
        analyst answer 'where does this number actually come from?'.
        """
        ds = self.datasets.get(dataset_id)
        if ds is None:
            raise KeyError(f"Unknown dataset '{dataset_id}'")
        col = next((c for c in ds.columns if c.name == column_name), None)
        if col is None:
            raise KeyError(f"Unknown column '{column_name}' on '{dataset_id}'")

        edges: list[dict] = []
        seen: set[str] = set()
        stack = [(f"{dataset_id}.{column_name}", col)]
        while stack:
            ref, current = stack.pop()
            for parent_ref in current.derived_from:
                edges.append({"from": parent_ref, "to": ref})
                if parent_ref in seen:
                    continue
                seen.add(parent_ref)
                p_ds_id, _, p_col_name = parent_ref.rpartition(".")
                p_ds = self.datasets.get(p_ds_id)
                if p_ds:
                    p_col = next(
                        (c for c in p_ds.columns if c.name == p_col_name), None
                    )
                    if p_col and p_col.derived_from:
                        stack.append((parent_ref, p_col))
        return {"column": f"{dataset_id}.{column_name}", "edges": edges}

    # ---- health / stats -----------------------------------------------------

    def stats(self) -> dict:
        edge_count = sum(len(v) for v in self._downstream.values())
        orphans = [
            ds_id for ds_id in self.datasets
            if not self._upstream.get(ds_id) and not self._downstream.get(ds_id)
        ]
        roots = [
            ds_id for ds_id in self.datasets if not self._upstream.get(ds_id)
        ]
        leaves = [
            ds_id for ds_id in self.datasets if not self._downstream.get(ds_id)
        ]
        return {
            "datasets": len(self.datasets),
            "edges": edge_count,
            "roots": sorted(roots),
            "leaves": sorted(leaves),
            "orphans": sorted(orphans),
            "has_cycle": bool(self.find_cycle()),
        }

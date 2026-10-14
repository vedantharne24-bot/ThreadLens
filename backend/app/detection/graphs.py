"""Graph algorithms used by the analysis engine.

Wait-for graph (WFG)
    Nodes are threads; edge A -> B means "A is blocked waiting for something
    that only B can provide" (a mutex B owns, or B's termination for a join).
    A blocked thread waits for exactly ONE thing, so every node has
    out-degree <= 1: the WFG is a *functional graph*. Finding the cycle reachable
    from a node is therefore a simple walk along successors — O(length of the
    chain) — instead of a full DFS over the graph. ``find_cycle_from`` does
    this walk; ``all_cycles`` (used for snapshots) runs the same idea from every
    node with a shared visited set, which is O(V) overall.

Lock-order graph (LOG)
    Nodes are locks; edge L1 -> L2 means "some thread acquired L2 (by a
    blocking lock call) while holding L1". This is the GoodLock / lockdep idea:
    a cycle L1 -> L2 -> ... -> L1 means the program acquires the same locks in
    conflicting orders and *may* deadlock under a different interleaving.
    When an edge is observed for the first time we look for a path back
    (BFS from the edge's target to its source), which yields a shortest cycle
    through the new edge in O(V + E).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Hashable, Iterable


def find_cycle_from(start: Hashable, successor: Callable[[Hashable], Hashable | None]) -> list | None:
    """Follow successor links from ``start``; return the cycle reached, if any.

    The returned list contains each node of the cycle once, in edge order.
    The start node itself need not be on the cycle (it may lead into one).
    """
    position: dict = {}
    path: list = []
    node = start
    while node is not None:
        if node in position:
            return path[position[node]:]
        position[node] = len(path)
        path.append(node)
        node = successor(node)
    return None


def all_cycles(nodes: Iterable[Hashable], successor: Callable[[Hashable], Hashable | None]) -> list[list]:
    """All cycles of a functional graph (each node has at most one successor)."""
    state: dict = {}  # node -> walk id when first visited
    cycles: list[list] = []
    for walk_id, start in enumerate(nodes):
        if start in state:
            continue
        path: list = []
        node = start
        while node is not None and node not in state:
            state[node] = walk_id
            path.append(node)
            node = successor(node)
        if node is not None and state.get(node) == walk_id:
            # the walk ran into a node visited during this same walk: cycle
            idx = path.index(node)
            cycles.append(path[idx:])
    return cycles


def canonical_cycle(cycle: list) -> tuple:
    """Rotate a cycle so that it starts at its smallest element (for dedup)."""
    if not cycle:
        return ()
    i = min(range(len(cycle)), key=lambda k: cycle[k])
    return tuple(cycle[i:] + cycle[:i])


@dataclass(slots=True)
class LockOrderEdge:
    src: int
    dst: int
    count: int = 0
    threads: set[int] = field(default_factory=set)
    first_thread: int = -1
    first_seq_src: int = -1      # when src was acquired (first observation)
    first_seq_dst: int = -1      # when dst was acquired while holding src
    first_ts: int = 0
    first_caller_src: int = 0
    first_caller_dst: int = 0
    last_seq: int = -1
    guards: frozenset[int] | None = None  # locks held during EVERY observation
    timed_only: bool = True      # every observation used a timed acquisition
    via_cond: bool = False       # observed when re-acquiring a cond-var mutex

    def as_dict(self) -> dict:
        return {
            "from": self.src, "to": self.dst, "count": self.count,
            "threads": sorted(self.threads), "first_thread": self.first_thread,
            "first_seq_src": self.first_seq_src, "first_seq": self.first_seq_dst,
            "first_ts": self.first_ts, "last_seq": self.last_seq,
            "guards": sorted(self.guards or ()), "timed_only": self.timed_only,
            "via_cond": self.via_cond,
        }


class LockOrderGraph:
    def __init__(self) -> None:
        self.edges: dict[tuple[int, int], LockOrderEdge] = {}
        self.adj: dict[int, set[int]] = {}

    def observe(self, src: int, dst: int, thread: int, seq_src: int, seq_dst: int, ts: int,
                caller_src: int, caller_dst: int, guards: frozenset[int], timed: bool,
                via_cond: bool = False) -> tuple[LockOrderEdge, bool]:
        """Record one observation of "dst acquired while holding src".

        Returns (edge, is_new_edge).
        """
        key = (src, dst)
        edge = self.edges.get(key)
        new = edge is None
        if new:
            edge = LockOrderEdge(src, dst, first_thread=thread, first_seq_src=seq_src,
                                 first_seq_dst=seq_dst, first_ts=ts, first_caller_src=caller_src,
                                 first_caller_dst=caller_dst, guards=guards, via_cond=via_cond)
            self.edges[key] = edge
            self.adj.setdefault(src, set()).add(dst)
        else:
            edge.guards = edge.guards & guards if edge.guards is not None else guards
        edge.count += 1
        edge.threads.add(thread)
        edge.last_seq = seq_dst
        if not timed:
            edge.timed_only = False
        return edge, new

    def path(self, start: int, goal: int) -> list[int] | None:
        """Shortest path start ~> goal (BFS), as a list of nodes."""
        if start == goal:
            return [start]
        prev: dict[int, int] = {start: start}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for nxt in self.adj.get(node, ()):
                if nxt in prev:
                    continue
                prev[nxt] = node
                if nxt == goal:
                    out = [goal]
                    while out[-1] != start:
                        out.append(prev[out[-1]])
                    return out[::-1]
                queue.append(nxt)
        return None

    def cycle_through(self, src: int, dst: int) -> list[int] | None:
        """Cycle that uses edge src -> dst: [src, dst, ..., (back to src)]."""
        back = self.path(dst, src)
        if back is None:
            return None
        return [src] + back[:-1]

    def cycle_edges(self, cycle: list[int]) -> list[LockOrderEdge]:
        return [self.edges[(cycle[i], cycle[(i + 1) % len(cycle)])] for i in range(len(cycle))]

    def all_cycles(self, limit: int = 50) -> list[list[int]]:
        """Distinct shortest cycles through each edge (bounded; for snapshots)."""
        seen: set[tuple] = set()
        out: list[list[int]] = []
        for (src, dst) in self.edges:
            cyc = self.cycle_through(src, dst)
            if cyc is None:
                continue
            key = canonical_cycle(cyc)
            if key in seen:
                continue
            seen.add(key)
            out.append(list(key))
            if len(out) >= limit:
                break
        return out


def tarjan_scc(nodes: Iterable[Hashable], neighbors: Callable[[Hashable], Iterable[Hashable]]) -> list[list]:
    """Strongly connected components (iterative Tarjan). Used for merged
    multi-session lock-order graphs, whose nodes can have many successors."""
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    result: list[list] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(neighbors(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(neighbors(nxt))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
    return result

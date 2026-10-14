import { Html, Line, OrbitControls, RoundedBox } from "@react-three/drei";
import { Canvas, type ThreeEvent, useFrame } from "@react-three/fiber";
import { type MutableRefObject, useMemo, useRef, useState } from "react";
import * as THREE from "three";

import { usePrefs, useReducedEffects } from "@/hooks/usePrefs";
import type { GEdge, GNode, GraphModel } from "@/utils/graph";

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0) / 4294967295;
}

/** Deterministic 3D force-directed layout (Fruchterman–Reingold style). */
function layout3D(nodes: GNode[], edges: GEdge[], prev: Map<string, THREE.Vector3>): Map<string, THREE.Vector3> {
  const pos = new Map<string, THREE.Vector3>();
  nodes.forEach((n, i) => {
    const p = prev.get(n.id);
    if (p) pos.set(n.id, p.clone());
    else {
      const a = hash(n.id) * Math.PI * 2;
      const b = Math.acos(2 * hash(n.id + "z") - 1);
      const r = 3 + (i % 3);
      pos.set(n.id, new THREE.Vector3(r * Math.sin(b) * Math.cos(a), r * Math.cos(b) * 0.6, r * Math.sin(b) * Math.sin(a)));
    }
  });
  const k = 2.4;
  let temp = 1.2;
  const disp = new Map<string, THREE.Vector3>();
  const tmp = new THREE.Vector3();
  for (let it = 0; it < 140; it++) {
    nodes.forEach((n) => disp.set(n.id, new THREE.Vector3()));
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos.get(nodes[i].id)!;
        const b = pos.get(nodes[j].id)!;
        tmp.subVectors(a, b);
        const d = Math.max(tmp.length(), 0.05);
        tmp.multiplyScalar((k * k) / (d * d));
        disp.get(nodes[i].id)!.add(tmp);
        disp.get(nodes[j].id)!.sub(tmp);
      }
    }
    for (const e of edges) {
      const a = pos.get(e.source);
      const b = pos.get(e.target);
      if (!a || !b) continue;
      tmp.subVectors(a, b);
      const d = Math.max(tmp.length(), 0.05);
      tmp.multiplyScalar((d / k) * 0.5); // attraction ~ d²/k along the edge
      disp.get(e.source)!.sub(tmp);
      disp.get(e.target)!.add(tmp);
    }
    nodes.forEach((n) => {
      const p = pos.get(n.id)!;
      const dv = disp.get(n.id)!;
      dv.add(tmp.copy(p).multiplyScalar(-0.06)); // gravity to the centre
      const len = dv.length();
      if (len > 0) p.add(dv.multiplyScalar(Math.min(len, temp) / len));
    });
    temp *= 0.97;
  }
  return pos;
}

interface Palette {
  cyan: string;
  violet: string;
  crit: string;
  warn: string;
  info: string;
  fg: string;
  fg3: string;
  bg: string;
}

function nodeColor(n: GNode, p: Palette): string {
  if (n.classes.includes("cycle")) return p.crit;
  if (n.classes.includes("potential")) return p.warn;
  if (n.kind === "lock") return p.violet;
  if (n.classes.includes("blocked_lock")) return p.warn;
  if (n.classes.includes("blocked_join") || n.classes.includes("barrier")) return p.info;
  if (n.classes.includes("waiting_cond")) return p.violet;
  if (n.classes.includes("exited")) return p.fg3;
  return p.cyan;
}

function edgeColor(e: GEdge, p: Palette): string {
  if (e.classes.includes("cycle")) return p.crit;
  if (e.classes.includes("potential")) return p.warn;
  switch (e.kind) {
    case "holds":
      return p.violet;
    case "waits":
      return p.warn;
    case "join":
      return p.info;
    case "reacquire":
      return p.violet;
    default:
      return p.fg3;
  }
}

function NodeMesh({
  node,
  target,
  positions,
  color,
  selected,
  onSelect,
  reduced,
}: {
  node: GNode;
  target: THREE.Vector3;
  positions: MutableRefObject<Map<string, THREE.Vector3>>;
  color: string;
  selected: boolean;
  onSelect?: (id: string) => void;
  reduced: boolean;
}) {
  const group = useRef<THREE.Group>(null);
  const mat = useRef<THREE.MeshStandardMaterial>(null);
  const [hover, setHover] = useState(false);
  const inCycle = node.classes.includes("cycle") || node.classes.includes("potential");
  useFrame((state, dt) => {
    const g = group.current;
    if (!g) return;
    const live = positions.current.get(node.id) ?? target.clone();
    live.lerp(target, reduced ? 1 : Math.min(1, dt * 4));
    positions.current.set(node.id, live);
    g.position.copy(live);
    const s = hover || selected ? 1.25 : 1;
    g.scale.lerp(new THREE.Vector3(s, s, s), reduced ? 1 : 0.2);
    if (mat.current) {
      mat.current.emissiveIntensity = inCycle && !reduced ? 0.9 + Math.sin(state.clock.elapsedTime * 4) * 0.5 : selected ? 1.1 : 0.55;
    }
    if (!reduced && node.kind === "lock") g.rotation.y += dt * 0.3;
  });
  const handlers = {
    onClick: (e: ThreeEvent<MouseEvent>) => {
      e.stopPropagation();
      onSelect?.(node.id);
    },
    onPointerOver: (e: ThreeEvent<PointerEvent>) => {
      e.stopPropagation();
      setHover(true);
      document.body.style.cursor = "pointer";
    },
    onPointerOut: () => {
      setHover(false);
      document.body.style.cursor = "";
    },
  };
  return (
    <group ref={group}>
      {node.kind === "thread" ? (
        <mesh {...handlers}>
          <icosahedronGeometry args={[0.42, 2]} />
          <meshStandardMaterial ref={mat} color={color} emissive={color} emissiveIntensity={0.6} roughness={0.35} metalness={0.4} />
        </mesh>
      ) : (
        <RoundedBox args={[0.95, 0.55, 0.55]} radius={0.12} smoothness={3} {...handlers}>
          <meshStandardMaterial ref={mat} color={color} emissive={color} emissiveIntensity={0.5} roughness={0.3} metalness={0.6} />
        </RoundedBox>
      )}
      {inCycle && (
        <mesh>
          <sphereGeometry args={[0.85, 24, 24]} />
          <meshBasicMaterial color={color} transparent opacity={0.1} depthWrite={false} />
        </mesh>
      )}
      <Html center position={[0, -0.85, 0]} distanceFactor={11} style={{ pointerEvents: "none" }} zIndexRange={[10, 0]}>
        <div
          className="mono whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px]"
          style={{ background: "color-mix(in oklab, var(--bg) 80%, transparent)", borderColor: "var(--line-strong)", color: "var(--fg)" }}
        >
          {node.label}
          <span className="ml-1 text-[9px]" style={{ color: "var(--fg-3)" }}>
            {node.kind === "thread" ? `T${node.ref}` : `L${node.ref}`}
          </span>
        </div>
      </Html>
    </group>
  );
}

function EdgeView({ edge, positions, color, reduced }: { edge: GEdge; positions: MutableRefObject<Map<string, THREE.Vector3>>; color: string; reduced: boolean }) {
  const line = useRef<THREE.Object3D & { geometry: { setPositions: (p: number[]) => void }; computeLineDistances: () => void }>(null);
  const arrow = useRef<THREE.Mesh>(null);
  const particle = useRef<THREE.Mesh>(null);
  const offset = useMemo(() => hash(edge.id), [edge.id]);
  const flows = edge.kind === "waits" || edge.kind === "join" || edge.kind === "reacquire";
  const dashed = edge.kind !== "holds" && edge.kind !== "order";
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const dir = new THREE.Vector3();
  const up = new THREE.Vector3(0, 1, 0);
  useFrame((state) => {
    const pa = positions.current.get(edge.source);
    const pb = positions.current.get(edge.target);
    if (!pa || !pb) return;
    dir.subVectors(pb, pa).normalize();
    a.copy(pa).addScaledVector(dir, 0.5);
    b.copy(pb).addScaledVector(dir, -0.6);
    if (line.current) {
      line.current.geometry.setPositions([a.x, a.y, a.z, b.x, b.y, b.z]);
      if (dashed) line.current.computeLineDistances();
    }
    if (arrow.current) {
      arrow.current.position.copy(b);
      arrow.current.quaternion.setFromUnitVectors(up, dir);
    }
    if (particle.current) {
      const t = reduced ? 0.5 : (state.clock.elapsedTime * 0.45 + offset) % 1;
      particle.current.position.lerpVectors(a, b, t);
      particle.current.visible = !reduced;
    }
  });
  const cycle = edge.classes.includes("cycle") || edge.classes.includes("potential");
  return (
    <group>
      <Line
        ref={line as never}
        points={[
          [0, 0, 0],
          [0, 0.001, 0],
        ]}
        color={color}
        lineWidth={cycle ? 3 : 1.6}
        dashed={dashed}
        dashSize={0.25}
        gapSize={0.15}
        transparent
        opacity={cycle ? 1 : 0.8}
      />
      <mesh ref={arrow}>
        <coneGeometry args={[0.1, 0.28, 12]} />
        <meshBasicMaterial color={color} />
      </mesh>
      {flows && (
        <mesh ref={particle}>
          <sphereGeometry args={[0.07, 10, 10]} />
          <meshBasicMaterial color={color} />
        </mesh>
      )}
    </group>
  );
}

export default function Graph3D({ model, onSelect, selected, height = 560 }: { model: GraphModel; onSelect?: (id: string | null) => void; selected?: string | null; height?: number | string }) {
  const reduced = useReducedEffects();
  const theme = usePrefs((p) => p.theme);
  const positions = useRef(new Map<string, THREE.Vector3>());
  const targets = useMemo(() => layout3D(model.nodes, model.edges, positions.current), [model]);
  const palette: Palette = useMemo(
    () => ({
      cyan: cssVar("--cyan", "#3ddcff"),
      violet: cssVar("--violet", "#9283ff"),
      crit: cssVar("--crit", "#ff4d6d"),
      warn: cssVar("--warn", "#ffb547"),
      info: cssVar("--info", "#6ea2ff"),
      fg: cssVar("--fg", "#e7ecf6"),
      fg3: cssVar("--fg-3", "#6f7a93"),
      bg: cssVar("--bg", "#06080f"),
    }),
    [theme],
  );
  const ids = new Set(model.nodes.map((n) => n.id));
  return (
    <div style={{ height }} className="relative overflow-hidden rounded-b-[14px]">
      <Canvas camera={{ position: [0, 2.5, 12], fov: 48 }} dpr={[1, 2]} onPointerMissed={() => onSelect?.(null)} gl={{ antialias: true, powerPreference: "high-performance" }}>
        <color attach="background" args={[palette.bg]} />
        <fog attach="fog" args={[palette.bg, 12, 30]} />
        <ambientLight intensity={0.35} />
        <pointLight position={[8, 8, 8]} intensity={80} color={palette.cyan} />
        <pointLight position={[-9, -5, -6]} intensity={60} color={palette.violet} />
        <gridHelper args={[30, 30, palette.fg3, palette.fg3]} position={[0, -4.5, 0]} material-opacity={0.08} material-transparent />
        {model.edges
          .filter((e) => ids.has(e.source) && ids.has(e.target))
          .map((e) => (
            <EdgeView key={e.id} edge={e} positions={positions} color={edgeColor(e, palette)} reduced={reduced} />
          ))}
        {model.nodes.map((n) => (
          <NodeMesh
            key={n.id}
            node={n}
            target={targets.get(n.id)!}
            positions={positions}
            color={nodeColor(n, palette)}
            selected={selected === n.id}
            onSelect={onSelect}
            reduced={reduced}
          />
        ))}
        <OrbitControls enableDamping dampingFactor={0.08} autoRotate={!reduced && !selected} autoRotateSpeed={0.35} minDistance={4} maxDistance={28} />
      </Canvas>
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border hairline bg-bg/70 px-2 py-1 text-[11px] text-fg-3 backdrop-blur">
        Drag to orbit · scroll to zoom · click a node for evidence
      </div>
    </div>
  );
}

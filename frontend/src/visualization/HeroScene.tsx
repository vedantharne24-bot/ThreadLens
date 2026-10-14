import { Center, Float, RoundedBox, Sparkles, Text3D } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Suspense, useMemo, useRef } from "react";
import * as THREE from "three";
import fontUrl from "three/examples/fonts/helvetiker_bold.typeface.json?url";

import { usePrefs } from "@/hooks/usePrefs";

function rand(seed: number) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

interface Net {
  threads: THREE.Vector3[];
  locks: THREE.Vector3[];
  edges: [THREE.Vector3, THREE.Vector3, "hold" | "wait" | "cycle"][];
  cycle: [number, number, number, number]; // thread a, lock 1, thread b, lock 2
}

function buildNetwork(): Net {
  const r = rand(7);
  const threads: THREE.Vector3[] = [];
  const locks: THREE.Vector3[] = [];
  for (let i = 0; i < 16; i++) {
    const a = (i / 16) * Math.PI * 2 + r() * 0.3;
    const rad = 5.2 + r() * 2.4;
    threads.push(new THREE.Vector3(Math.cos(a) * rad, (r() - 0.5) * 5, -2.5 - r() * 4 + Math.sin(a) * 1.5));
  }
  for (let i = 0; i < 7; i++) {
    const a = (i / 7) * Math.PI * 2 + 0.4;
    const rad = 3.8 + r() * 1.6;
    locks.push(new THREE.Vector3(Math.cos(a) * rad, (r() - 0.5) * 3.6, -3.5 - r() * 2));
  }
  const edges: Net["edges"] = [];
  threads.forEach((t, i) => {
    const l1 = locks[i % locks.length];
    edges.push([l1, t, "hold"]);
    if (i % 3 === 0) edges.push([t, locks[(i + 2) % locks.length], "wait"]);
  });
  // a highlighted circular wait: T0 holds L1 and waits for L2, T5 holds L2 and waits for L1
  const cycle: Net["cycle"] = [0, 1, 5, 2];
  edges.push([locks[1], threads[0], "cycle"], [threads[0], locks[2], "cycle"], [locks[2], threads[5], "cycle"], [threads[5], locks[1], "cycle"]);
  return { threads, locks, edges, cycle };
}

function Pulse({ a, b, color, speed, offset }: { a: THREE.Vector3; b: THREE.Vector3; color: string; speed: number; offset: number }) {
  const ref = useRef<THREE.Mesh>(null);
  useFrame((s) => {
    const t = (s.clock.elapsedTime * speed + offset) % 1;
    ref.current?.position.lerpVectors(a, b, t);
  });
  return (
    <mesh ref={ref}>
      <sphereGeometry args={[0.045, 8, 8]} />
      <meshBasicMaterial color={color} toneMapped={false} />
    </mesh>
  );
}

function Network({ palette }: { palette: Record<string, string> }) {
  const net = useMemo(buildNetwork, []);
  const group = useRef<THREE.Group>(null);
  const cycleMats = useRef<THREE.MeshStandardMaterial[]>([]);
  const lines = useMemo(() => {
    const geo = (kind: "hold" | "wait" | "cycle") => {
      const pts: number[] = [];
      net.edges.filter((e) => e[2] === kind).forEach(([a, b]) => pts.push(a.x, a.y, a.z, b.x, b.y, b.z));
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
      return g;
    };
    return { hold: geo("hold"), wait: geo("wait"), cycle: geo("cycle") };
  }, [net]);

  useFrame((s, dt) => {
    if (group.current) group.current.rotation.y += dt * 0.025;
    const k = 0.9 + Math.sin(s.clock.elapsedTime * 3.2) * 0.6;
    cycleMats.current.forEach((m) => m && (m.emissiveIntensity = k));
  });

  const inCycleThread = (i: number) => i === net.cycle[0] || i === net.cycle[2];
  const inCycleLock = (i: number) => i === net.cycle[1] || i === net.cycle[3];
  return (
    <group ref={group}>
      <lineSegments geometry={lines.hold}>
        <lineBasicMaterial color={palette.violet} transparent opacity={0.28} />
      </lineSegments>
      <lineSegments geometry={lines.wait}>
        <lineBasicMaterial color={palette.cyan} transparent opacity={0.22} />
      </lineSegments>
      <lineSegments geometry={lines.cycle}>
        <lineBasicMaterial color={palette.crit} transparent opacity={0.85} />
      </lineSegments>
      {net.threads.map((p, i) => (
        <Float key={`t${i}`} speed={1 + (i % 4) * 0.3} rotationIntensity={0} floatIntensity={0.6}>
          <mesh position={p}>
            <icosahedronGeometry args={[inCycleThread(i) ? 0.22 : 0.15, 1]} />
            <meshStandardMaterial
              ref={(m) => {
                if (m && inCycleThread(i)) cycleMats.current[i] = m;
              }}
              color={inCycleThread(i) ? palette.crit : palette.cyan}
              emissive={inCycleThread(i) ? palette.crit : palette.cyan}
              emissiveIntensity={0.9}
              toneMapped={false}
            />
          </mesh>
        </Float>
      ))}
      {net.locks.map((p, i) => (
        <RoundedBox key={`l${i}`} args={[0.42, 0.26, 0.26]} radius={0.06} position={p}>
          <meshStandardMaterial
            ref={(m) => {
              if (m && inCycleLock(i)) cycleMats.current[100 + i] = m;
            }}
            color={inCycleLock(i) ? palette.crit : palette.violet}
            emissive={inCycleLock(i) ? palette.crit : palette.violet}
            emissiveIntensity={0.6}
            metalness={0.5}
            roughness={0.3}
          />
        </RoundedBox>
      ))}
      {net.edges
        .filter((e) => e[2] !== "hold")
        .map(([a, b, kind], i) => (
          <Pulse key={i} a={a} b={b} color={kind === "cycle" ? palette.crit : palette.cyan} speed={kind === "cycle" ? 0.5 : 0.25 + (i % 5) * 0.05} offset={i * 0.37} />
        ))}
    </group>
  );
}

function Title({ palette }: { palette: Record<string, string> }) {
  // keep the title in the upper part of the view and inside its width,
  // so it never sits behind the headline text on short or narrow screens
  const { viewport } = useThree();
  const scale = Math.min(1, viewport.width / 10.5);
  const y = viewport.height * 0.2;
  return (
    <Float speed={1.4} rotationIntensity={0.15} floatIntensity={0.35}>
      <Center position={[0, y, 0]} scale={scale}>
        <Text3D font={fontUrl} size={1.05} height={0.3} curveSegments={10} bevelEnabled bevelSize={0.022} bevelThickness={0.045} bevelSegments={4} letterSpacing={0.01}>
          ThreadLens
          <meshPhysicalMaterial color={palette.title} metalness={0.85} roughness={0.2} clearcoat={1} clearcoatRoughness={0.15} emissive={palette.cyan} emissiveIntensity={0.06} />
        </Text3D>
      </Center>
    </Float>
  );
}

function CameraRig({ progress }: { progress: React.MutableRefObject<number> }) {
  useFrame((state) => {
    const { camera, pointer } = state;
    const p = progress.current;
    camera.position.x = THREE.MathUtils.lerp(camera.position.x, pointer.x * 1.1, 0.04);
    camera.position.y = THREE.MathUtils.lerp(camera.position.y, pointer.y * 0.6 + p * 1.5, 0.04);
    camera.position.z = THREE.MathUtils.lerp(camera.position.z, 9 + p * 5, 0.06);
    camera.lookAt(0, p * 0.8, -1);
  });
  return null;
}

export default function HeroScene({ progress, active }: { progress: React.MutableRefObject<number>; active: boolean }) {
  const theme = usePrefs((p) => p.theme);
  const palette = useMemo(() => {
    const css = getComputedStyle(document.documentElement);
    const v = (n: string, f: string) => css.getPropertyValue(n).trim() || f;
    return {
      cyan: v("--cyan", "#3ddcff"),
      violet: v("--violet", "#9283ff"),
      crit: v("--crit", "#ff4d6d"),
      bg: v("--bg", "#06080f"),
      title: theme === "light" ? "#1b2a44" : "#e8f1ff",
    };
  }, [theme]);
  return (
    <Canvas camera={{ position: [0, 0, 9], fov: 45 }} dpr={[1, 1.75]} frameloop={active ? "always" : "never"} gl={{ antialias: true, alpha: true }}>
      <fog attach="fog" args={[palette.bg, 9, 22]} />
      <ambientLight intensity={0.4} />
      <directionalLight position={[4, 6, 6]} intensity={2.2} color="#ffffff" />
      <pointLight position={[-6, -2, 4]} intensity={50} color={palette.violet} />
      <pointLight position={[6, 3, 2]} intensity={45} color={palette.cyan} />
      <Suspense fallback={null}>
        <Title palette={palette} />
      </Suspense>
      <Network palette={palette} />
      <Sparkles count={120} scale={[18, 10, 10]} size={1.6} speed={0.25} opacity={0.5} color={palette.cyan} />
      <CameraRig progress={progress} />
    </Canvas>
  );
}

import { useEffect, useRef, useState } from 'react';

interface Particle {
  id: number;
  angle: number;
  radius: number;
  speed: number;
  layer: 1 | 2 | 3;
  resolved: boolean;
  terminal: 'MATCHED' | 'REVIEW' | 'EXCEPTION' | null;
  opacity: number;
  pulsePhase: number;
}

const LAYER_RADII = [0, 70, 130, 195];
const CENTER = 250;
const PARTICLE_COUNT = 14;

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function ReconciliationOrbit() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const particlesRef = useRef<Particle[]>([]);
  const rafRef = useRef<number>(0);
  const [reduced] = useState(prefersReducedMotion);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = 500 * dpr;
    canvas.height = 500 * dpr;
    canvas.style.width = '500px';
    canvas.style.height = '500px';
    ctx.scale(dpr, dpr);

    // Initialize particles
    if (particlesRef.current.length === 0) {
      particlesRef.current = Array.from({ length: PARTICLE_COUNT }, (_, i) => {
        const rand = Math.random();
        let layer: 1 | 2 | 3 = 1;
        let terminal: 'MATCHED' | 'REVIEW' | 'EXCEPTION' | null = null;

        if (rand < 0.65) {
          layer = 1;
          terminal = 'MATCHED';
        } else if (rand < 0.85) {
          layer = 2;
          terminal = Math.random() < 0.6 ? 'MATCHED' : 'REVIEW';
        } else {
          layer = 3;
          terminal = Math.random() < 0.5 ? 'REVIEW' : 'EXCEPTION';
        }

        return {
          id: i,
          angle: (i / PARTICLE_COUNT) * Math.PI * 2 + Math.random() * 0.5,
          radius: 15,
          speed: 0.003 + Math.random() * 0.004,
          layer,
          resolved: false,
          terminal,
          opacity: 0.3 + Math.random() * 0.4,
          pulsePhase: Math.random() * Math.PI * 2,
        };
      });
    }

    if (reduced) {
      drawStatic(ctx, particlesRef.current);
      return;
    }

    let lastTime = performance.now();

    const animate = (now: number) => {
      const dt = Math.min(now - lastTime, 50);
      lastTime = now;

      ctx.clearRect(0, 0, 500, 500);

      // Draw rings
      drawRings(ctx);

      // Update and draw particles
      for (const p of particlesRef.current) {
        p.angle += p.speed * dt;
        p.pulsePhase += 0.02 * dt / 16;

        // Move outward based on layer
        const targetRadius = LAYER_RADII[p.layer];
        p.radius += (targetRadius - p.radius) * 0.02;

        const x = CENTER + Math.cos(p.angle) * p.radius;
        const y = CENTER + Math.sin(p.angle) * p.radius;

        // Color based on layer/terminal
        let color = '#5FB98B'; // green
        if (p.layer === 2) color = '#E8B85B'; // amber
        if (p.layer === 3) {
          color = p.terminal === 'EXCEPTION' ? '#D9614E' : '#E8B85B';
        }

        const pulse = p.layer === 2 ? 0.7 + Math.sin(p.pulsePhase) * 0.3 : 1;

        // Draw particle
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.globalAlpha = p.opacity * pulse;
        ctx.fill();

        // Glow
        ctx.beginPath();
        ctx.arc(x, y, 6, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.globalAlpha = p.opacity * pulse * 0.15;
        ctx.fill();

        ctx.globalAlpha = 1;
      }

      // Draw center node
      drawCenter(ctx);

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(rafRef.current);
  }, [reduced]);

  return (
    <div className="relative flex items-center justify-center" style={{ width: 500, height: 500 }}>
      <canvas ref={canvasRef} className="absolute inset-0" />
      {/* Ring labels */}
      <div className="absolute inset-0 pointer-events-none">
        <RingLabel radius={70} angle={-Math.PI / 2.5} text="LAYER 1 · RULES" />
        <RingLabel radius={130} angle={-Math.PI / 2.5} text="LAYER 2 · AI RESIDUAL" />
        <RingLabel radius={195} angle={-Math.PI / 2.5} text="LAYER 3 · GUARDRAIL" />
        <TerminalLabel angle={Math.PI / 4} radius={230} text="MATCHED" color="#5FB98B" />
        <TerminalLabel angle={Math.PI / 1.8} radius={230} text="REVIEW" color="#E8B85B" />
        <TerminalLabel angle={Math.PI / 0.9} radius={230} text="EXCEPTION" color="#D9614E" />
      </div>
    </div>
  );
}

function RingLabel({ radius, angle, text }: { radius: number; angle: number; text: string }) {
  const x = 250 + Math.cos(angle) * radius;
  const y = 250 + Math.sin(angle) * radius;
  return (
    <div
      className="absolute mono text-[9px] text-cream-500/60 tracking-widest whitespace-nowrap"
      style={{ left: x, top: y, transform: 'translate(-50%, -50%)' }}
    >
      {text}
    </div>
  );
}

function TerminalLabel({ angle, radius, text, color }: { angle: number; radius: number; text: string; color: string }) {
  const x = 250 + Math.cos(angle) * radius;
  const y = 250 + Math.sin(angle) * radius;
  return (
    <div
      className="absolute mono text-[9px] tracking-widest whitespace-nowrap font-medium"
      style={{ left: x, top: y, transform: 'translate(-50%, -50%)', color }}
    >
      {text}
    </div>
  );
}

function drawRings(ctx: CanvasRenderingContext2D) {
  const radii = [70, 130, 195];
  const colors = ['rgba(95,185,139,0.12)', 'rgba(232,184,91,0.10)', 'rgba(217,97,78,0.08)'];

  radii.forEach((r, i) => {
    ctx.beginPath();
    ctx.arc(CENTER, CENTER, r, 0, Math.PI * 2);
    ctx.strokeStyle = colors[i];
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 6]);
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

function drawCenter(ctx: CanvasRenderingContext2D) {
  // Outer glow
  const gradient = ctx.createRadialGradient(CENTER, CENTER, 0, CENTER, CENTER, 30);
  gradient.addColorStop(0, 'rgba(232,184,91,0.3)');
  gradient.addColorStop(1, 'rgba(232,184,91,0)');
  ctx.beginPath();
  ctx.arc(CENTER, CENTER, 30, 0, Math.PI * 2);
  ctx.fillStyle = gradient;
  ctx.fill();

  // Core
  ctx.beginPath();
  ctx.arc(CENTER, CENTER, 12, 0, Math.PI * 2);
  ctx.fillStyle = '#1D1B14';
  ctx.fill();
  ctx.strokeStyle = '#E8B85B';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Label
  ctx.font = '8px "JetBrains Mono", monospace';
  ctx.fillStyle = '#E8B85B';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('RECORD', CENTER, CENTER);
}

function drawStatic(ctx: CanvasRenderingContext2D, particles: Particle[]) {
  drawRings(ctx);
  drawCenter(ctx);

  // Draw static particles at their target positions
  for (const p of particles) {
    const targetRadius = LAYER_RADII[p.layer];
    const x = CENTER + Math.cos(p.angle) * targetRadius;
    const y = CENTER + Math.sin(p.angle) * targetRadius;

    let color = '#5FB98B';
    if (p.layer === 2) color = '#E8B85B';
    if (p.layer === 3) color = p.terminal === 'EXCEPTION' ? '#D9614E' : '#E8B85B';

    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.globalAlpha = p.opacity;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
}

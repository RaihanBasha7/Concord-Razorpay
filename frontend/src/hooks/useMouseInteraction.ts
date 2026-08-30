import { useEffect, useRef, useCallback, useState } from 'react';

/**
 * Tracks mouse position relative to an element and returns normalized offsets (-1 to 1).
 * Uses requestAnimationFrame for smooth, throttled updates. Respects reduced motion.
 */
export function useMousePosition<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [position, setPosition] = useState({ x: 0, y: 0, active: false });
  const rafRef = useRef<number>(0);
  const pendingRef = useRef<{ x: number; y: number } | null>(null);

  const handleMove = useCallback((e: MouseEvent) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    pendingRef.current = {
      x: ((e.clientX - rect.left) / rect.width - 0.5) * 2,
      y: ((e.clientY - rect.top) / rect.height - 0.5) * 2,
    };
    if (!rafRef.current) {
      rafRef.current = requestAnimationFrame(() => {
        if (pendingRef.current) {
          setPosition({ ...pendingRef.current, active: true });
        }
        rafRef.current = 0;
      });
    }
  }, []);

  const handleLeave = useCallback(() => {
    setPosition({ x: 0, y: 0, active: false });
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReduced) return;

    el.addEventListener('mousemove', handleMove);
    el.addEventListener('mouseleave', handleLeave);
    return () => {
      el.removeEventListener('mousemove', handleMove);
      el.removeEventListener('mouseleave', handleLeave);
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [handleMove, handleLeave]);

  return { ref, ...position };
}

/**
 * Subtle 3D tilt effect for cards. Returns ref + transform style.
 * Max tilt is very small (3deg) to stay professional.
 */
export function useTilt<T extends HTMLElement>(maxTilt = 3) {
  const { ref, x, y, active } = useMousePosition<T>();

  const transform = active
    ? `perspective(800px) rotateX(${-y * maxTilt}deg) rotateY(${x * maxTilt}deg) scale(1.005)`
    : 'perspective(800px) rotateX(0deg) rotateY(0deg) scale(1)';

  const transition = 'transform 0.2s cubic-bezier(0.22, 1, 0.36, 1)';

  return { ref, transform, transition, active };
}

/**
 * Ambient glow that follows the cursor within a section.
 * Returns a ref to attach and a style object for a glow div.
 */
export function useAmbientGlow<T extends HTMLElement>(enabled = true) {
  const ref = useRef<T>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number>(0);

  useEffect(() => {
    if (!enabled) return;
    const el = ref.current;
    const glow = glowRef.current;
    if (!el || !glow) return;
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReduced) return;

    let pending: { x: number; y: number } | null = null;

    const onMove = (e: MouseEvent) => {
      const rect = el.getBoundingClientRect();
      pending = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      if (!rafRef.current) {
        rafRef.current = requestAnimationFrame(() => {
          if (pending && glow) {
            glow.style.setProperty('--glow-x', `${pending.x}px`);
            glow.style.setProperty('--glow-y', `${pending.y}px`);
            glow.style.opacity = '1';
          }
          rafRef.current = 0;
        });
      }
    };

    const onLeave = () => {
      if (glow) glow.style.opacity = '0';
    };

    el.addEventListener('mousemove', onMove);
    el.addEventListener('mouseleave', onLeave);
    return () => {
      el.removeEventListener('mousemove', onMove);
      el.removeEventListener('mouseleave', onLeave);
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [enabled]);

  return { ref, glowRef };
}
